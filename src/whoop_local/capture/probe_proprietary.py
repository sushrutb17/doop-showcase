"""Phase 1b diagnostic probe — NOT the final collector.

Connects to the saved band, subscribes to all four proprietary notify
characteristics, and listens passively. Only if nothing arrives does it try
writing a single low-risk command (toggle_realtime_hr, command 3) and listen
again. Every raw notification is logged and persisted to `raw_packets` so
nothing is lost even if our parsing/command-number guesses are wrong.

See ``CASE_STUDY.md`` for why this is a staged, cautious probe rather than a
confident implementation: the UUIDs were confirmed against owned hardware
before the low-risk command hypothesis was exercised.
"""

import argparse
import asyncio
import logging
import time

from bleak import BleakClient

from whoop_local.capture.proprietary_protocol import (
    ALL_NOTIFY_UUIDS,
    CHAR_COMMAND_TO_STRAP,
    CHAR_DATA_FROM_STRAP,
    COMMAND_TOGGLE_REALTIME_HR,
    build_command,
    parse_frame,
)
from whoop_local.config import DEFAULT_DB_PATH, current_host
from whoop_local.db.device_store import latest_identifier
from whoop_local.db.schema import init_db

# Only the data channel is what we actually need (realtime HR/sensor packets).
# CHAR_MEMFAULT (diagnostics) and CHAR_EVENTS_FROM_STRAP (battery/wrist/etc.
# events) are both confirmed-working and validate our protocol implementation,
# but neither is the HR feed itself — don't let them gate the command attempt.
RELEVANT_UUIDS = {CHAR_DATA_FROM_STRAP}

log = logging.getLogger("whoop_local.probe")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--host", default=current_host())
    parser.add_argument(
        "--listen-seconds",
        type=float,
        default=15.0,
        help="passive listen window before trying a command",
    )
    parser.add_argument(
        "--after-command-seconds",
        type=float,
        default=25.0,
        help="listen window after sending toggle_realtime_hr",
    )
    parser.add_argument(
        "--skip-command",
        action="store_true",
        help="passive listen only, never write anything to the device",
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
                "VALUES (?, ?, ?, 'live', ?, 'phase1b_probe')",
                (ts_utc, char_uuid, args.host, payload.hex()),
            )
            conn.commit()
            received.append((char_uuid, payload))
            log.info("NOTIFY %s [%d bytes]: %s", char_uuid, len(payload), payload.hex())
            try:
                parsed = parse_frame(payload)
                log.info(
                    "  parsed: packet_type=%s seq=%s cmd/event=%s header_crc=%s payload_crc=%s",
                    parsed.packet_type,
                    parsed.sequence,
                    parsed.command_or_event,
                    parsed.header_crc_valid,
                    parsed.payload_crc_valid,
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

            relevant = [r for r in received if r[0] in RELEVANT_UUIDS]
            if relevant:
                log.info(
                    "Received %d relevant (event/data) notification(s) passively — command not needed.",
                    len(relevant),
                )
                return
            if received:
                log.info(
                    "Received %d notification(s), but none on the event/data channels (likely just memfault chatter).",
                    len(received),
                )

            if args.skip_command:
                log.info("Nothing received passively. --skip-command set, stopping here.")
                return

            log.info(
                "Nothing received passively. Sending toggle_realtime_hr (command %d) to %s...",
                COMMAND_TOGGLE_REALTIME_HR,
                CHAR_COMMAND_TO_STRAP,
            )
            frame = build_command(sequence=1, command=COMMAND_TOGGLE_REALTIME_HR)
            log.info("Command frame: %s", frame.hex())
            try:
                await client.write_gatt_char(CHAR_COMMAND_TO_STRAP, frame, response=True)
                log.info("Write acknowledged.")
            except Exception as exc:
                log.error("Write failed: %s", exc)
                return

            log.info("Listening for %.0fs after command...", args.after_command_seconds)
            await asyncio.sleep(args.after_command_seconds)
            log.info("Total notifications received: %d", len(received))

    asyncio.run(run())


if __name__ == "__main__":
    main()
