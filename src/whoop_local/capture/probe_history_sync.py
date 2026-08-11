"""Phase 4 diagnostic probe — NOT the real history-sync implementation.

Same staged, cautious methodology that de-risked the realtime-HR work
(D15/D16/D19): connect, listen passively first, then GetDataRange (ReadOnly),
then optionally SendHistoricalData (UserVisibleStateChange — triggers an
actual bulk transfer) with a payload derived from GetDataRange's own decoded
fields (D34) rather than guessed blind.

GetDataRange's 72-byte response is now partially decoded (D34, from an earlier
controlled probe, cross-checked against
its own console-log text): payload[10:14] (u32 LE) is a "valid pages"
counter matching the device's own logged "Flash: Valid Pages: N" almost
exactly, and payload[18:22] (u32 LE) is a constant matching that counter's
earlier value -- the working hypothesis is [start_page, end_page) at those
two offsets. This probe's default SendHistoricalData payload is
struct.pack("<II", start_page, end_page) built from a *fresh* GetDataRange
response, with a --historical-data-payload-hex override for testing other
hypotheses.

See ``CASE_STUDY.md`` for why this was deliberately deferred until a dedicated,
staged session.
"""

import argparse
import asyncio
import logging
import struct
import time

from bleak import BleakClient

from whoop_local.capture.proprietary_protocol import (
    ALL_NOTIFY_UUIDS,
    CHAR_COMMAND_TO_STRAP,
    COMMAND_ABORT_HISTORICAL_TRANSMITS,
    COMMAND_GET_DATA_RANGE,
    COMMAND_SEND_HISTORICAL_DATA,
    PACKET_TYPE_HISTORICAL_DATA,
    build_command,
    parse_frame,
)
from whoop_local.config import DEFAULT_DB_PATH, current_host
from whoop_local.db.device_store import latest_identifier
from whoop_local.db.schema import init_db

log = logging.getLogger("whoop_local.probe_history")

# D34: offsets into GetDataRange's *payload* (post packet_type/seq/cmd header).
GET_DATA_RANGE_END_PAGE_OFFSET = 10
GET_DATA_RANGE_START_PAGE_OFFSET = 18


def derive_historical_data_payload(get_data_range_payload: bytes) -> bytes | None:
    """Builds a candidate SendHistoricalData request payload from a real
    GetDataRange response, per the D34 field hypothesis. Returns None if the
    payload is too short to contain both fields."""
    if len(get_data_range_payload) < GET_DATA_RANGE_START_PAGE_OFFSET + 4:
        return None
    end_page = int.from_bytes(
        get_data_range_payload[GET_DATA_RANGE_END_PAGE_OFFSET : GET_DATA_RANGE_END_PAGE_OFFSET + 4],
        "little",
    )
    start_page = int.from_bytes(
        get_data_range_payload[
            GET_DATA_RANGE_START_PAGE_OFFSET : GET_DATA_RANGE_START_PAGE_OFFSET + 4
        ],
        "little",
    )
    return struct.pack("<II", start_page, end_page)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--host", default=current_host())
    parser.add_argument("--listen-seconds", type=float, default=10.0)
    parser.add_argument("--after-command-seconds", type=float, default=20.0)
    parser.add_argument(
        "--send-historical-data",
        action="store_true",
        help="also try SendHistoricalData (22) after GetDataRange — "
        "UserVisibleStateChange risk, triggers an actual transfer",
    )
    parser.add_argument(
        "--historical-data-payload-hex",
        default=None,
        help="override the SendHistoricalData request payload (hex). "
        "Default: derive [start_page,end_page) from this session's own "
        "fresh GetDataRange response (D34).",
    )
    parser.add_argument("--transfer-listen-seconds", type=float, default=60.0)
    parser.add_argument(
        "--abort-after",
        action="store_true",
        help="send AbortHistoricalTransmits (20) at the end to cleanly stop any transfer",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = init_db(args.db)
    identifier = latest_identifier(conn, args.host)
    if not identifier:
        raise SystemExit(f"No saved device for host={args.host!r}. Run scan.py --save first.")

    received: list[tuple[str, bytes]] = []

    def on_notify(char_uuid: str):
        def _handler(_sender, data: bytearray) -> None:
            payload = bytes(data)
            ts_utc = int(time.time() * 1000)
            conn.execute(
                "INSERT INTO raw_packets (ts_utc, source_uuid, source_host, origin, payload_hex, notes) "
                "VALUES (?, ?, ?, 'live', ?, 'phase4_probe')",
                (ts_utc, char_uuid, args.host, payload.hex()),
            )
            conn.commit()
            received.append((char_uuid, payload))
            log.info("NOTIFY %s [%d bytes]: %s", char_uuid, len(payload), payload.hex())
            try:
                parsed = parse_frame(payload)
                log.info(
                    "  parsed: packet_type=%s seq=%s cmd/event=%s header_crc=%s payload_crc=%s payload=%s",
                    parsed.packet_type,
                    parsed.sequence,
                    parsed.command_or_event,
                    parsed.header_crc_valid,
                    parsed.payload_crc_valid,
                    parsed.payload.hex(),
                )
            except ValueError as exc:
                log.info("  (not a parseable Gen4 frame: %s)", exc)

        return _handler

    async def run() -> None:
        async with BleakClient(identifier) as client:
            log.info("Connected to %s", identifier)
            for uuid in ALL_NOTIFY_UUIDS:
                await client.start_notify(uuid, on_notify(uuid))
                log.info("Subscribed: %s", uuid)

            log.info("Listening passively for %.0fs...", args.listen_seconds)
            await asyncio.sleep(args.listen_seconds)
            log.info("Passive phase done: %d notification(s) received.", len(received))

            log.info(
                "Sending GetDataRange (command %d, ReadOnly per source) to %s...",
                COMMAND_GET_DATA_RANGE,
                CHAR_COMMAND_TO_STRAP,
            )
            get_data_range_count_before = len(received)
            frame = build_command(sequence=1, command=COMMAND_GET_DATA_RANGE)
            log.info("Command frame: %s", frame.hex())
            try:
                await client.write_gatt_char(CHAR_COMMAND_TO_STRAP, frame, response=True)
                log.info("Write acknowledged.")
            except Exception as exc:
                log.error("Write failed: %s", exc)
                return

            log.info("Listening for %.0fs after command...", args.after_command_seconds)
            await asyncio.sleep(args.after_command_seconds)
            log.info("Notifications after GetDataRange: %d total", len(received))

            if args.send_historical_data:
                if args.historical_data_payload_hex is not None:
                    hist_payload = bytes.fromhex(args.historical_data_payload_hex)
                    log.info(
                        "Using explicit --historical-data-payload-hex override: %s",
                        hist_payload.hex(),
                    )
                else:
                    get_data_range_response = None
                    for char_uuid, payload in received[get_data_range_count_before:]:
                        try:
                            parsed = parse_frame(payload)
                        except ValueError:
                            continue
                        if (
                            parsed.command_or_event == COMMAND_GET_DATA_RANGE
                            and parsed.payload_crc_valid
                        ):
                            get_data_range_response = parsed.payload
                    if get_data_range_response is None:
                        log.error(
                            "No valid GetDataRange response seen this session — can't derive a payload."
                        )
                        log.error("PHASE4_RESULT: send_historical_data_no_get_data_range_response")
                        return
                    candidate_payload = derive_historical_data_payload(get_data_range_response)
                    if candidate_payload is None:
                        log.error(
                            "GetDataRange response too short to derive [start_page,end_page)."
                        )
                        log.error("PHASE4_RESULT: send_historical_data_undecodable_get_data_range")
                        return
                    hist_payload = candidate_payload
                    log.info(
                        "Derived SendHistoricalData payload from this session's fresh GetDataRange (D34): %s",
                        hist_payload.hex(),
                    )

                before_count = len(received)
                log.info(
                    "Sending SendHistoricalData (command %d, UserVisibleStateChange per source) to %s with payload %s...",
                    COMMAND_SEND_HISTORICAL_DATA,
                    CHAR_COMMAND_TO_STRAP,
                    hist_payload.hex(),
                )
                frame = build_command(
                    sequence=2, command=COMMAND_SEND_HISTORICAL_DATA, data=hist_payload
                )
                log.info("Command frame: %s", frame.hex())
                try:
                    await client.write_gatt_char(CHAR_COMMAND_TO_STRAP, frame, response=True)
                    log.info("Write acknowledged.")
                except Exception as exc:
                    log.error("SendHistoricalData write FAILED: %s", exc)
                    log.error("PHASE4_RESULT: send_historical_data_write_failed")
                    return

                log.info(
                    "Listening for %.0fs for transfer activity...", args.transfer_listen_seconds
                )
                await asyncio.sleep(args.transfer_listen_seconds)
                new_notifications = received[before_count:]
                new_count = len(new_notifications)
                historical_data_count = 0
                for _char_uuid, payload in new_notifications:
                    try:
                        parsed = parse_frame(payload)
                    except ValueError:
                        continue
                    if parsed.packet_type == PACKET_TYPE_HISTORICAL_DATA:
                        historical_data_count += 1

                if new_count == 0:
                    log.warning(
                        "PHASE4_RESULT: send_historical_data_no_response — zero notifications after the command"
                    )
                elif historical_data_count > 0:
                    log.info(
                        "PHASE4_RESULT: send_historical_data_got_real_sensor_history — "
                        "%d HISTORICAL_DATA packet(s) of %d total notifications",
                        historical_data_count,
                        new_count,
                    )
                else:
                    log.info(
                        "PHASE4_RESULT: send_historical_data_got_%d_notifications_no_historical_data — "
                        "check packet_type breakdown above (likely console logs again, per D27)",
                        new_count,
                    )

                if args.abort_after:
                    log.info(
                        "Sending AbortHistoricalTransmits (command %d) to clean up...",
                        COMMAND_ABORT_HISTORICAL_TRANSMITS,
                    )
                    abort_frame = build_command(
                        sequence=3, command=COMMAND_ABORT_HISTORICAL_TRANSMITS
                    )
                    try:
                        await client.write_gatt_char(
                            CHAR_COMMAND_TO_STRAP, abort_frame, response=True
                        )
                        log.info("Abort acknowledged.")
                    except Exception as exc:
                        log.error("Abort write failed (non-fatal): %s", exc)
                    await asyncio.sleep(3.0)

            log.info("Total notifications received: %d", len(received))

    asyncio.run(run())


if __name__ == "__main__":
    main()
