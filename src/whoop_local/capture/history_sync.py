"""Durable WHOOP 4 history offload.

The band sends stored type-47 records in chunks delimited by type-49 metadata.
Each HISTORY_END carries an eight-byte continuation token.  Doop stores the
chunk's raw transport bytes and decoded records in one SQLite transaction,
commits it, and only then echoes that token in command 23.  If the process dies
before the ACK, the band safely re-sends the chunk and database deduplication
makes the replay harmless.

Physical execution is deliberately opt-in.  Use ``--execute`` here, or start
the normal collector with ``--history-sync`` after stopping any competing BLE
client.  The deployed launchd collector is not changed automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sqlite3
import time
from dataclasses import dataclass

from bleak import BleakClient

from whoop_local.capture.proprietary_protocol import (
    ALL_NOTIFY_UUIDS,
    CHAR_COMMAND_TO_STRAP,
    CHAR_DATA_FROM_STRAP,
    COMMAND_ABORT_HISTORICAL_TRANSMITS,
    COMMAND_GET_CLOCK,
    HISTORY_COMPLETE,
    HISTORY_END,
    HISTORY_START,
    PACKET_TYPE_COMMAND_RESPONSE,
    PACKET_TYPE_HISTORICAL_DATA,
    FrameReassembler,
    build_history_ack,
    build_history_init_commands,
    build_padded_command,
    build_set_clock,
    parse_frame,
    parse_historical_record,
    parse_history_metadata,
)
from whoop_local.config import DEFAULT_DB_PATH, current_host
from whoop_local.db.device_store import latest_identifier
from whoop_local.db.schema import init_db

log = logging.getLogger("whoop_local.history_sync")

COMMAND_SPACING_S = 0.12
DEFAULT_TIMEOUT_S = 180.0
MIN_PLAUSIBLE_HISTORY_MS = 1_577_836_800_000  # 2020-01-01 UTC
MAX_FUTURE_SKEW_MS = 86_400_000


@dataclass(frozen=True)
class HistorySyncReport:
    complete: bool
    records_received: int
    records_inserted: int
    hr_inserted: int
    rr_inserted: int
    batches_acked: int
    clock_verified: bool
    error: str | None = None


@dataclass(frozen=True)
class _TransportChunk:
    received_at: int
    source_uuid: str
    data: bytes


@dataclass(frozen=True)
class _HistoryFrame:
    received_at: int
    raw_frame: bytes
    payload: bytes


class HistoryStore:
    """Atomic raw-first persistence for one history chunk."""

    def __init__(self, conn: sqlite3.Connection, source_host: str):
        self.conn = conn
        self.source_host = source_host

    def persist_chunk(
        self,
        transport: list[_TransportChunk],
        records: list[_HistoryFrame],
    ) -> tuple[int, int, int]:
        inserted_records = 0
        inserted_hr = 0
        inserted_rr = 0
        with self.conn:
            # Raw transport notifications are durable before any derived insert.
            self.conn.executemany(
                "INSERT INTO raw_packets "
                "(ts_utc, source_uuid, source_host, origin, payload_hex, notes) "
                "VALUES (?, ?, ?, 'history', ?, 'phase4_history_offload')",
                [
                    (item.received_at, item.source_uuid, self.source_host, item.data.hex())
                    for item in transport
                ],
            )
            for item in records:
                decoded = parse_historical_record(item.payload)
                version = item.payload[1] if len(item.payload) > 1 else None
                counter = (
                    int.from_bytes(item.payload[3:7], "little") if len(item.payload) >= 7 else None
                )
                plausible = bool(
                    decoded is not None
                    and MIN_PLAUSIBLE_HISTORY_MS
                    <= decoded.device_ts_utc
                    <= item.received_at + MAX_FUTURE_SKEW_MS
                    and 25 <= decoded.hr_bpm <= 230
                )
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO historical_records "
                    "(received_at, device_ts_utc, record_counter, record_version, "
                    " source_host, payload_hex, decoded) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.received_at,
                        decoded.device_ts_utc if decoded else None,
                        decoded.counter if decoded else counter,
                        decoded.version if decoded else version,
                        self.source_host,
                        item.raw_frame.hex(),
                        int(plausible),
                    ),
                )
                if cursor.rowcount == 0:
                    continue
                inserted_records += 1
                if not plausible or decoded is None:
                    continue
                hr_cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO hr_samples "
                    "(ts_utc, device_ts_utc, hr_bpm, source_host, source) "
                    "VALUES (?, ?, ?, ?, 'whoop')",
                    (
                        decoded.device_ts_utc,
                        decoded.device_ts_utc,
                        decoded.hr_bpm,
                        self.source_host,
                    ),
                )
                inserted_hr += max(hr_cursor.rowcount, 0)
                for rr_index, rr_ms in enumerate(decoded.rr_values_ms):
                    rr_cursor = self.conn.execute(
                        "INSERT OR IGNORE INTO rr_intervals "
                        "(ts_utc, device_ts_utc, rr_ms, source_host, source, rr_index) "
                        "VALUES (?, ?, ?, ?, 'whoop', ?)",
                        (
                            decoded.device_ts_utc,
                            decoded.device_ts_utc,
                            rr_ms,
                            self.source_host,
                            rr_index,
                        ),
                    )
                    inserted_rr += max(rr_cursor.rowcount, 0)
        return inserted_records, inserted_hr, inserted_rr


class HistoryOffloader:
    def __init__(
        self,
        conn: sqlite3.Connection,
        source_host: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self.store = HistoryStore(conn, source_host)
        self.source_host = source_host
        self.timeout_s = timeout_s
        self._queue: asyncio.Queue[_TransportChunk | None] = asyncio.Queue()
        self._assemblers = {uuid.lower(): FrameReassembler() for uuid in ALL_NOTIFY_UUIDS}
        self._complete = asyncio.Event()
        self._failed = asyncio.Event()
        self._active = False
        self._transport: list[_TransportChunk] = []
        self._records: list[_HistoryFrame] = []
        self._ack_sequence = 5
        self._client: BleakClient | None = None
        self._error: str | None = None
        self.records_received = 0
        self.records_inserted = 0
        self.hr_inserted = 0
        self.rr_inserted = 0
        self.batches_acked = 0
        self.clock_verified = False

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _callback(self, source_uuid: str):
        def on_notify(_sender, data: bytearray) -> None:
            self._queue.put_nowait(_TransportChunk(self._now_ms(), source_uuid, bytes(data)))

        return on_notify

    def _persist_immediate(self, item: _TransportChunk) -> None:
        with self.store.conn:
            self.store.conn.execute(
                "INSERT INTO raw_packets "
                "(ts_utc, source_uuid, source_host, origin, payload_hex, notes) "
                "VALUES (?, ?, ?, 'history', ?, 'phase4_history_control')",
                (
                    item.received_at,
                    item.source_uuid,
                    self.source_host,
                    item.data.hex(),
                ),
            )

    def _flush_chunk(self) -> None:
        inserted, hr, rr = self.store.persist_chunk(self._transport, self._records)
        self.records_inserted += inserted
        self.hr_inserted += hr
        self.rr_inserted += rr
        self._transport.clear()
        self._records.clear()

    def _observe_clock_response(self, payload: bytes, received_at: int) -> None:
        if (
            len(payload) < 4
            or payload[0] != PACKET_TYPE_COMMAND_RESPONSE
            or payload[2] != COMMAND_GET_CLOCK
        ):
            return
        wall_seconds = received_at // 1000
        for offset in range(3, len(payload) - 3):
            candidate = int.from_bytes(payload[offset : offset + 4], "little")
            if abs(candidate - wall_seconds) <= 60:
                self.clock_verified = True
                return

    async def _process_notification(self, item: _TransportChunk) -> None:
        frames = self._assemblers[item.source_uuid.lower()].feed(item.data)
        data_channel = item.source_uuid.lower() == CHAR_DATA_FROM_STRAP.lower()
        if not frames:
            if self._active and data_channel:
                self._transport.append(item)
            else:
                self._persist_immediate(item)
            return

        if not data_channel:
            self._persist_immediate(item)
        if self._active and data_channel:
            self._transport.append(item)

        history_protocol_seen = False
        for raw_frame in frames:
            try:
                parsed = parse_frame(raw_frame)
            except ValueError:
                continue
            if not (parsed.header_crc_valid and parsed.payload_crc_valid):
                raise ValueError("invalid CRC in history transfer")
            self._observe_clock_response(parsed.payload, item.received_at)

            if parsed.packet_type == PACKET_TYPE_HISTORICAL_DATA:
                history_protocol_seen = True
                if not self._active:
                    self._active = True
                    self._transport = [item]
                self.records_received += 1
                self._records.append(_HistoryFrame(item.received_at, raw_frame, parsed.payload))
                continue

            marker = parse_history_metadata(parsed.payload)
            if marker is None:
                continue
            history_protocol_seen = True
            if marker.subtype == HISTORY_START:
                self._active = True
                self._transport = [item]
                self._records.clear()
                continue
            if marker.subtype == HISTORY_END:
                if marker.token is None:
                    raise ValueError("HISTORY_END did not contain an 8-byte continuation token")
                if not self._active:
                    self._active = True
                    self._transport = [item]
                elif data_channel and (not self._transport or self._transport[-1] is not item):
                    self._transport.append(item)
                # Commit raw + decoded data before advancing the strap cursor.
                self._flush_chunk()
                ack = build_history_ack(self._ack_sequence, marker.token)
                self._ack_sequence = (self._ack_sequence + 1) & 0xFF
                if self._client is None:
                    raise RuntimeError("history client is unavailable while acknowledging a chunk")
                await self._client.write_gatt_char(CHAR_COMMAND_TO_STRAP, ack, response=True)
                self.batches_acked += 1
                if self.batches_acked % 25 == 0:
                    log.info(
                        "History progress: batches_acked=%d records_received=%d "
                        "records_inserted=%d",
                        self.batches_acked,
                        self.records_received,
                        self.records_inserted,
                    )
                # END closes this chunk. The next one must begin with START;
                # unrelated data arriving between chunks is stored separately.
                self._active = False
                continue
            if marker.subtype == HISTORY_COMPLETE:
                if self._transport or self._records:
                    self._flush_chunk()
                elif data_channel:
                    self._persist_immediate(item)
                self._active = False
                self._complete.set()

        if data_channel and not self._active and not history_protocol_seen:
            self._persist_immediate(item)

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            try:
                await self._process_notification(item)
            except Exception as exc:
                self._error = str(exc)
                log.exception("History transfer failed while processing a notification")
                self._failed.set()
                return

    async def run(self, client: BleakClient) -> HistorySyncReport:
        self._client = client
        worker = asyncio.create_task(self._worker())
        subscribed: list[str] = []
        try:
            for uuid in ALL_NOTIFY_UUIDS:
                await client.start_notify(uuid, self._callback(uuid))
                subscribed.append(uuid)

            # Repair/verify the RTC before requesting flash records.  The old
            # band logs proved that an invalid RTC can stop flash writes.
            await client.write_gatt_char(
                CHAR_COMMAND_TO_STRAP,
                build_set_clock(0xA0, self._now_ms()),
                response=True,
            )
            await asyncio.sleep(COMMAND_SPACING_S)
            await client.write_gatt_char(
                CHAR_COMMAND_TO_STRAP,
                build_padded_command(0xA1, COMMAND_GET_CLOCK, b""),
                response=True,
            )
            await asyncio.sleep(COMMAND_SPACING_S)

            for command in build_history_init_commands():
                await client.write_gatt_char(CHAR_COMMAND_TO_STRAP, command, response=True)
                await asyncio.sleep(COMMAND_SPACING_S)

            complete_wait = asyncio.create_task(self._complete.wait())
            failed_wait = asyncio.create_task(self._failed.wait())
            done, pending = await asyncio.wait(
                (complete_wait, failed_wait),
                timeout=self.timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if not done:
                self._error = f"history transfer timed out after {self.timeout_s:.0f}s"
            elif failed_wait in done:
                self._error = self._error or "history transfer worker failed"

            # A wall-clock timeout can still follow steady cursor progress on a
            # large flash backlog. Disconnect without ABORT in that case so the
            # next session resumes from the already-ACKed chunks. ABORT is
            # reserved for a malformed/failed transfer; OpenStrap field evidence
            # shows aborting a merely long offload can stall cursor progress.
            if self._error and not self._error.startswith("history transfer timed out"):
                with contextlib.suppress(Exception):
                    await client.write_gatt_char(
                        CHAR_COMMAND_TO_STRAP,
                        build_padded_command(0xAF, COMMAND_ABORT_HISTORICAL_TRANSMITS, b"\x00"),
                        response=True,
                    )
        finally:
            for uuid in subscribed:
                with contextlib.suppress(Exception):
                    await client.stop_notify(uuid)
            if self._error:
                worker.cancel()
            else:
                await self._queue.put(None)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await worker

        return HistorySyncReport(
            complete=self._complete.is_set() and self._error is None,
            records_received=self.records_received,
            records_inserted=self.records_inserted,
            hr_inserted=self.hr_inserted,
            rr_inserted=self.rr_inserted,
            batches_acked=self.batches_acked,
            clock_verified=self.clock_verified,
            error=self._error,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--host", default=current_host())
    parser.add_argument("--identifier")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="required acknowledgement that this will set the strap clock and offload history",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        raise SystemExit("Refusing to touch the band without --execute")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    conn = init_db(args.db)
    identifier = args.identifier or latest_identifier(conn, args.host)
    if not identifier:
        raise SystemExit(f"No saved device for host={args.host!r}; run scan.py --save first")

    async def execute() -> HistorySyncReport:
        async with BleakClient(identifier) as client:
            return await HistoryOffloader(conn, args.host, timeout_s=args.timeout).run(client)

    report = asyncio.run(execute())
    print(report)
    if not report.complete:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
