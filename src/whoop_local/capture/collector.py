"""Live-capture collector (Phase 1 + RR decode).

Connects to the WHOOP band by its saved peripheral identifier, subscribes to
the proprietary data/event channels, sends the toggle_realtime_hr command,
and writes every raw notification plus decoded HR/RR values to SQLite.
Reconnects with backoff on disconnect and records the outage in
`capture_gaps`.

Standard BLE Heart Rate (0x180D/0x2A37) was tried first (D8) and confirmed
dead on this firmware (D14) — it subscribes without error but never sends a
notification. Real HR/RR only flow through the proprietary channel after
sending toggle_realtime_hr (D15, D16, D19); see ``CASE_STUDY.md`` and
``proprietary_protocol.py`` for the empirically decoded packet layout.

Includes a data-freshness watchdog (D24): macOS's Bluetooth stack does not
reliably fire bleak's disconnected_callback when the *system* sleeps (as
opposed to the peripheral going out of range), so a session can go silently
stale with no error, no gap logged, and no reconnect attempt. Separately,
`toggle_realtime_hr` is a stateful toggle, not an explicit "turn on" — if a
prior session died uncleanly (streaming never explicitly stopped), the next
connection's toggle can flip the stream back *off*. The watchdog forces a
full reconnect (which resets device streaming state) whenever no HR
notification has arrived for `WATCHDOG_TIMEOUT_S`, fixing both failure modes
with one mechanism.

Run `scan.py --save` first to populate the device table for this host.
"""

import argparse
import asyncio
import contextlib
import logging
import sqlite3
import time

from bleak import BleakClient
from bleak.exc import BleakError

from whoop_local.capture.history_sync import HistoryOffloader
from whoop_local.capture.proprietary_protocol import (
    ALL_NOTIFY_UUIDS,
    CHAR_COMMAND_TO_STRAP,
    CHAR_DATA_FROM_STRAP,
    CHAR_EVENTS_FROM_STRAP,
    COMMAND_TOGGLE_REALTIME_HR,
    build_command,
    parse_battery_raw,
    parse_frame,
    parse_realtime_hr,
    parse_realtime_rr,
)
from whoop_local.config import DEFAULT_DB_PATH, current_host
from whoop_local.db.battery_store import record_battery_sample
from whoop_local.db.device_store import latest_identifier
from whoop_local.db.schema import init_db

log = logging.getLogger("whoop_local.collector")

RECONNECT_BACKOFF_S = [2, 5, 10, 30, 60]  # caps out and repeats the last value
WATCHDOG_TIMEOUT_S = 60.0  # no HR notification this long -> assume dead, force reconnect
WATCHDOG_POLL_INTERVAL_S = 5.0
# CoreBluetooth never times out a pending connectPeripheral on its own — a band
# drifting out of range mid-attempt leaves the await hanging *forever* with no
# exception and no disconnected_callback (observed as an unbounded silent
# stall). The D24 watchdog only guards the streaming phase,
# so the connect/setup phase needs its own deadline.
CONNECT_SETUP_TIMEOUT_S = 45.0
DISCONNECT_TIMEOUT_S = 10.0  # disconnect can hang the same way; abandon it rather than stall
# A normal reconnect should absorb an extended disconnection in one durable
# pass. The standalone CLI remains deliberately shorter by default; the
# always-on collector has an explicit, bounded catch-up window.
AUTO_HISTORY_TIMEOUT_S = 900.0


def is_stale(
    last_notification_monotonic: float, now_monotonic: float, timeout_s: float = WATCHDOG_TIMEOUT_S
) -> bool:
    """Pure staleness check, kept separate from the async loop for testability."""
    return (now_monotonic - last_notification_monotonic) > timeout_s


class Collector:
    def __init__(
        self,
        conn: sqlite3.Connection,
        identifier: str,
        source_host: str,
        *,
        history_sync: bool = False,
    ):
        self.conn = conn
        self.identifier = identifier
        self.source_host = source_host
        self._gap_open_id: int | None = None
        self._command_sequence = 0
        self._last_hr_monotonic: float = 0.0
        self.history_sync = history_sync

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _on_notification(self, characteristic, data: bytearray) -> None:
        ts_utc = self._now_ms()
        payload = bytes(data)
        char_uuid = characteristic.uuid

        self.conn.execute(
            "INSERT INTO raw_packets (ts_utc, source_uuid, source_host, origin, payload_hex) "
            "VALUES (?, ?, ?, 'live', ?)",
            (ts_utc, char_uuid, self.source_host, payload.hex()),
        )
        self.conn.commit()

        if char_uuid.lower() == CHAR_EVENTS_FROM_STRAP.lower():
            try:
                parsed = parse_frame(payload)
            except ValueError:
                return
            if parsed.payload_crc_valid:
                battery_raw = parse_battery_raw(parsed.payload)
                if battery_raw is not None:
                    record_battery_sample(self.conn, ts_utc, battery_raw, self.source_host)
                    self.conn.commit()
            return

        if char_uuid.lower() != CHAR_DATA_FROM_STRAP.lower():
            return  # responses/memfault are stored raw but not parsed for metrics yet

        try:
            parsed = parse_frame(payload)
        except ValueError:
            log.debug("Non-frame payload on data channel: %s", payload.hex())
            return

        if not parsed.payload_crc_valid:
            log.warning("Payload CRC invalid, dropping: %s", payload.hex())
            return

        hr_bpm = parse_realtime_hr(parsed.payload)
        if hr_bpm is None:
            return

        self._last_hr_monotonic = time.monotonic()
        self.conn.execute(
            "INSERT OR IGNORE INTO hr_samples (ts_utc, hr_bpm, source_host, source) "
            "VALUES (?, ?, ?, 'whoop')",
            (ts_utc, hr_bpm, self.source_host),
        )
        rr_values_ms = parse_realtime_rr(parsed.payload)
        for rr_ms in rr_values_ms:
            self.conn.execute(
                "INSERT INTO rr_intervals (ts_utc, rr_ms, source_host, source) "
                "VALUES (?, ?, ?, 'whoop')",
                (ts_utc, rr_ms, self.source_host),
            )
        self.conn.commit()
        log.debug("hr=%d rr=%s", hr_bpm, rr_values_ms)

    def _open_gap(self) -> None:
        if self._gap_open_id is not None:
            return
        cur = self.conn.execute(
            "INSERT INTO capture_gaps (source_host, gap_start_utc, reason) VALUES (?, ?, ?)",
            (self.source_host, self._now_ms(), "disconnected"),
        )
        self.conn.commit()
        self._gap_open_id = cur.lastrowid
        log.warning("Gap opened (disconnect) id=%s", self._gap_open_id)

    def _close_gap(self) -> None:
        if self._gap_open_id is None:
            return
        self.conn.execute(
            "UPDATE capture_gaps SET gap_end_utc = ? WHERE id = ?",
            (self._now_ms(), self._gap_open_id),
        )
        self.conn.commit()
        log.info("Gap closed id=%s", self._gap_open_id)
        self._gap_open_id = None

    def _reconcile_open_gaps(self) -> None:
        """Close orphaned open gaps left behind by a previous process death (D31 follow-up).

        A gap only gets closed on successful reconnect, so a collector that dies
        or hangs while disconnected leaves its gap open forever — and an open gap
        reads as "ongoing through now" to coverage_fraction(), zeroing out the
        coverage of every day after it. Close each stale gap at the first capture
        that followed it; if nothing has been captured since, adopt it as this
        run's open gap so the next successful connect closes it.
        """
        rows = self.conn.execute(
            "SELECT id, gap_start_utc FROM capture_gaps "
            "WHERE source_host = ? AND gap_end_utc IS NULL ORDER BY gap_start_utc",
            (self.source_host,),
        ).fetchall()
        for gap_id, gap_start_utc in rows:
            resumed = self.conn.execute(
                "SELECT MIN(ts_utc) FROM hr_samples WHERE source_host = ? AND ts_utc > ?",
                (self.source_host, gap_start_utc),
            ).fetchone()[0]
            if resumed is not None:
                self.conn.execute(
                    "UPDATE capture_gaps SET gap_end_utc = ? WHERE id = ?", (resumed, gap_id)
                )
                log.warning(
                    "Closed orphaned gap id=%s at the first capture after it (%s)", gap_id, resumed
                )
            elif self._gap_open_id is None:
                self._gap_open_id = gap_id
                log.info("Adopted still-open gap id=%s from a previous run", gap_id)
            else:
                # A second still-open gap is redundant inside the adopted one's span.
                self.conn.execute(
                    "UPDATE capture_gaps SET gap_end_utc = gap_start_utc WHERE id = ?", (gap_id,)
                )
                log.warning(
                    "Zeroed redundant open gap id=%s (already inside an adopted gap)", gap_id
                )
        self.conn.commit()

    async def run_session(self) -> None:
        """Connect, stream until disconnected, then return (caller reconnects)."""
        disconnected = asyncio.Event()

        def _on_disconnect(_client: BleakClient) -> None:
            disconnected.set()

        client = BleakClient(self.identifier, disconnected_callback=_on_disconnect)
        try:
            # CoreBluetooth's pending connect needs its own hard deadline. The
            # optional history drain has a separate bounded timeout because a
            # real multi-day backlog can legitimately exceed setup's 45 seconds.
            async with asyncio.timeout(CONNECT_SETUP_TIMEOUT_S):
                await client.connect()
            self._close_gap()
            log.info("Connected to %s", self.identifier)

            if self.history_sync:
                try:
                    report = await HistoryOffloader(
                        self.conn,
                        self.source_host,
                        timeout_s=AUTO_HISTORY_TIMEOUT_S,
                    ).run(client)
                    log.info("History sync report: %s", report)
                except Exception as exc:
                    # History is additive gap filling. A failed attempt must not
                    # suppress the already-proven live stream; raw data remains
                    # on the band unless/until a durable chunk ACK succeeds.
                    log.exception("History sync failed; continuing with live capture: %s", exc)

            # Bound notify setup + live toggle separately after history releases
            # its subscriptions.
            async with asyncio.timeout(CONNECT_SETUP_TIMEOUT_S):
                for uuid in ALL_NOTIFY_UUIDS:
                    await client.start_notify(uuid, self._on_notification)

                self._command_sequence = (self._command_sequence + 1) % 256
                frame = build_command(
                    sequence=self._command_sequence, command=COMMAND_TOGGLE_REALTIME_HR
                )
                await client.write_gatt_char(CHAR_COMMAND_TO_STRAP, frame, response=True)
                log.info("Sent toggle_realtime_hr, listening...")
                self._last_hr_monotonic = time.monotonic()

            while not disconnected.is_set():
                try:
                    await asyncio.wait_for(disconnected.wait(), timeout=WATCHDOG_POLL_INTERVAL_S)
                except TimeoutError:
                    pass  # just a poll tick, not an error
                if is_stale(self._last_hr_monotonic, time.monotonic()):
                    log.warning(
                        "No HR notification in over %.0fs — forcing reconnect (watchdog). "
                        "Likely a silent system-sleep disconnect or a toggled-off stream (D24).",
                        WATCHDOG_TIMEOUT_S,
                    )
                    break

            if disconnected.is_set():
                log.warning("Disconnected from %s", self.identifier)
        finally:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                async with asyncio.timeout(DISCONNECT_TIMEOUT_S):
                    await client.disconnect()
            self._open_gap()

    async def run_forever(self) -> None:
        self._reconcile_open_gaps()
        backoff_idx = 0
        while True:
            try:
                await self.run_session()
                backoff_idx = 0  # clean session ran; reset backoff
            except (TimeoutError, BleakError, OSError) as exc:
                self._open_gap()
                log.error("Connection error: %s", exc)

            delay = RECONNECT_BACKOFF_S[min(backoff_idx, len(RECONNECT_BACKOFF_S) - 1)]
            backoff_idx += 1
            log.info("Reconnecting in %ss...", delay)
            await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="path to the SQLite database")
    parser.add_argument(
        "--host",
        default=current_host(),
        help="validated per-host source label (defaults to mac, linux, or pi)",
    )
    parser.add_argument(
        "--identifier",
        default=None,
        help="peripheral UUID/address; defaults to the last one saved by scan.py",
    )
    parser.add_argument(
        "--history-sync",
        action="store_true",
        help="opt in to SET_CLOCK + durable onboard-history offload before live capture",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = init_db(args.db)
    identifier = args.identifier or latest_identifier(conn, args.host)
    if not identifier:
        raise SystemExit(
            f"No saved device identifier for host={args.host!r}. "
            f"Run `python -m whoop_local.capture.scan --save` first."
        )

    log.info("Starting collector: host=%s identifier=%s db=%s", args.host, identifier, args.db)
    collector = Collector(
        conn,
        identifier,
        args.host,
        history_sync=args.history_sync,
    )
    try:
        asyncio.run(collector.run_forever())
    except KeyboardInterrupt:
        log.info("Stopped by user.")
        collector._close_gap()


if __name__ == "__main__":
    main()
