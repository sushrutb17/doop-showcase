"""Battery raw-value storage + self-calibration (Phase 3b, D28).

The raw fuel-gauge counter decoded in `proprietary_protocol.parse_battery_raw`
isn't on a documented 0-100% scale, so there's no fixed "full" constant to
divide by. Instead we track the highest raw value ever observed in
`meta['battery_raw_max']` and express percentage relative to that — it
starts conservative and gets more accurate as more full-charge cycles are
seen. The raw-value evidence and parser behavior are documented in
``capture/proprietary_protocol.py``.
"""

from __future__ import annotations

import argparse
import sqlite3

from whoop_local.capture.proprietary_protocol import (
    PACKET_TYPE_EVENT,
    parse_battery_raw,
    parse_frame,
)
from whoop_local.config import DEFAULT_DB_PATH
from whoop_local.db.schema import init_db

BATTERY_RAW_MAX_META_KEY = "battery_raw_max"


def record_battery_sample(
    conn: sqlite3.Connection, ts_utc: int, raw_value: int, source_host: str
) -> bool:
    """Insert a battery sample (deduped on ts_utc+source_host); returns True if
    a new row was actually inserted."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO battery_samples (ts_utc, raw_value, source_host) VALUES (?, ?, ?)",
        (ts_utc, raw_value, source_host),
    )
    current_max = battery_raw_max(conn)
    if current_max is None or raw_value > current_max:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (BATTERY_RAW_MAX_META_KEY, str(raw_value)),
        )
    return cur.rowcount > 0


def latest_battery(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """Returns (ts_utc, raw_value) of the most recent battery sample, or None."""
    return conn.execute(
        "SELECT ts_utc, raw_value FROM battery_samples ORDER BY ts_utc DESC LIMIT 1"
    ).fetchone()


def battery_raw_max(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (BATTERY_RAW_MAX_META_KEY,)
    ).fetchone()
    return int(row[0]) if row else None


def backfill_from_raw_packets(conn: sqlite3.Connection) -> int:
    """One-time migration: decode battery events already sitting in
    `raw_packets` from before this feature existed. Safe to re-run (dedup via
    battery_samples' UNIQUE(ts_utc, source_host))."""
    rows = conn.execute(
        "SELECT ts_utc, source_uuid, source_host, payload_hex FROM raw_packets "
        "WHERE source_uuid LIKE '61080004%' ORDER BY ts_utc"
    ).fetchall()

    inserted = 0
    for ts_utc, _uuid, source_host, payload_hex in rows:
        try:
            parsed = parse_frame(bytes.fromhex(payload_hex))
        except ValueError:
            continue
        if parsed.packet_type != PACKET_TYPE_EVENT or not parsed.payload_crc_valid:
            continue
        raw_value = parse_battery_raw(parsed.payload)
        if raw_value is None:
            continue
        if record_battery_sample(conn, ts_utc, raw_value, source_host):
            inserted += 1
    conn.commit()
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="decode battery events already stored in raw_packets",
    )
    args = parser.parse_args()

    conn = init_db(args.db)
    if args.backfill:
        count = backfill_from_raw_packets(conn)
        print(f"Backfilled {count} battery sample(s). Raw max observed: {battery_raw_max(conn)}")


if __name__ == "__main__":
    main()
