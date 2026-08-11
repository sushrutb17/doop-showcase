"""One-time backfill of RR interval slots 3-4 from stored raw packets (D54).

An audited full-dataset re-scan proved the realtime payload carries up to
FOUR RR intervals (bytes 14-17 are slots 3-4), not two — the collector's
parser dropped slots 3-4 for every packet captured before the fix. Because
raw payloads are always persisted (`raw_packets`), the missed values can be
re-derived exactly: this script re-parses every stored data-channel frame and
inserts only the RR values beyond what `rr_intervals` already holds for that
packet's timestamp.

Safe to re-run: a packet whose row count already matches its RR count is
skipped. A timestamp shared by more than one RR-bearing packet would make
the per-timestamp accounting ambiguous, so such packets are skipped and
reported instead of guessed at (none existed in the retained validation set).

Usage:
    python -m whoop_local.capture.backfill_rr [--db-path PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3

from whoop_local.capture.proprietary_protocol import (
    CHAR_DATA_FROM_STRAP,
    parse_frame,
    parse_realtime_rr,
)
from whoop_local.config import DEFAULT_DB_PATH
from whoop_local.db.schema import init_db


def backfill_rr_slots(conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    """Insert RR values the pre-D54 parser dropped; returns a summary dict."""
    stats = {
        "packets_scanned": 0,
        "packets_with_missing_rr": 0,
        "rr_inserted": 0,
        "ambiguous_skipped": 0,
    }

    # Map ts_utc -> list of (rr_values, source_host) for every RR-bearing
    # realtime packet, so a shared timestamp can be detected up front.
    packets_by_ts: dict[int, list[tuple[list[float], str]]] = {}
    for ts_utc, source_host, payload_hex in conn.execute(
        "SELECT ts_utc, source_host, payload_hex FROM raw_packets WHERE source_uuid = ?",
        (CHAR_DATA_FROM_STRAP,),
    ):
        try:
            parsed = parse_frame(bytes.fromhex(payload_hex))
        except ValueError:
            continue
        if not parsed.payload_crc_valid:
            continue
        rr_values = parse_realtime_rr(parsed.payload)
        stats["packets_scanned"] += 1
        if rr_values:
            packets_by_ts.setdefault(ts_utc, []).append((rr_values, source_host))

    for ts_utc, packets in packets_by_ts.items():
        if len(packets) > 1:
            stats["ambiguous_skipped"] += len(packets)
            continue
        rr_values, source_host = packets[0]
        existing = conn.execute(
            "SELECT COUNT(*) FROM rr_intervals WHERE ts_utc = ? AND source_host = ? AND source = 'whoop'",
            (ts_utc, source_host),
        ).fetchone()[0]
        missing = rr_values[existing:]
        if not missing:
            continue
        stats["packets_with_missing_rr"] += 1
        for rr_ms in missing:
            stats["rr_inserted"] += 1
            if not dry_run:
                conn.execute(
                    "INSERT INTO rr_intervals (ts_utc, rr_ms, source_host, source) VALUES (?, ?, ?, 'whoop')",
                    (ts_utc, rr_ms, source_host),
                )
    if not dry_run:
        conn.commit()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be inserted, write nothing"
    )
    args = parser.parse_args()

    conn = init_db(args.db_path)
    stats = backfill_rr_slots(conn, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}scanned {stats['packets_scanned']} realtime packets; "
        f"{stats['packets_with_missing_rr']} had undropped RR slots; "
        f"inserted {stats['rr_inserted']} RR values; "
        f"{stats['ambiguous_skipped']} ambiguous packets skipped"
    )


if __name__ == "__main__":
    main()
