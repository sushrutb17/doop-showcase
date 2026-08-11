"""Import capture-only rows from a Pi snapshot into the Mac-canonical DB."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from whoop_local.db.schema import SCHEMA_VERSION, init_db


@dataclass(frozen=True)
class CaptureTable:
    name: str
    columns: tuple[str, ...]
    host_column: str = "source_host"
    closed_only: bool = False


CAPTURE_TABLES = (
    CaptureTable(
        "raw_packets",
        ("ts_utc", "source_uuid", "source_host", "origin", "payload_hex", "notes"),
    ),
    CaptureTable(
        "historical_records",
        (
            "received_at",
            "device_ts_utc",
            "record_counter",
            "record_version",
            "source_host",
            "payload_hex",
            "decoded",
        ),
    ),
    CaptureTable(
        "hr_samples",
        ("ts_utc", "device_ts_utc", "hr_bpm", "source_host", "source"),
    ),
    CaptureTable(
        "rr_intervals",
        ("ts_utc", "device_ts_utc", "rr_ms", "source_host", "source", "rr_index"),
    ),
    CaptureTable(
        "spo2_samples",
        ("ts_utc", "device_ts_utc", "spo2_pct", "source_host"),
    ),
    CaptureTable(
        "temp_samples",
        ("ts_utc", "device_ts_utc", "skin_temp_c", "source_host"),
    ),
    CaptureTable("battery_samples", ("ts_utc", "raw_value", "source_host")),
    CaptureTable(
        "capture_gaps",
        ("source_host", "gap_start_utc", "gap_end_utc", "reason"),
        closed_only=True,
    ),
)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def _validate_source(source: sqlite3.Connection, target: sqlite3.Connection) -> str:
    source_version = _meta(source, "schema_version")
    if source_version != SCHEMA_VERSION:
        raise ValueError(
            f"source schema version must be {SCHEMA_VERSION}; got {source_version or 'missing'}"
        )
    dataset_id = _meta(source, "dataset_id")
    if not dataset_id:
        raise ValueError("source database is missing meta.dataset_id")
    if dataset_id == _meta(target, "dataset_id"):
        raise ValueError("refusing to import a database into itself")
    return dataset_id


def _validate_host(source: sqlite3.Connection, expected_host: str) -> None:
    unexpected: dict[str, list[str]] = {}
    for table in CAPTURE_TABLES:
        values = [
            str(row[0])
            for row in source.execute(
                f"SELECT DISTINCT {table.host_column} FROM {table.name} "
                f"WHERE {table.host_column} != ?",
                (expected_host,),
            )
        ]
        if values:
            unexpected[table.name] = values
    if unexpected:
        raise ValueError(f"source contains capture rows not tagged {expected_host!r}: {unexpected}")


def import_snapshot(
    source_path: Path | str,
    target_path: Path | str,
    *,
    expected_host: str = "pi",
    imported_at: int | None = None,
) -> dict[str, int]:
    """Copy unseen raw/capture rows; repeated imports are no-ops.

    Open capture gaps are deferred until they have an end time, so a transient
    snapshot can never freeze an incomplete gap in the canonical database.
    """
    source_file = Path(source_path)
    target_file = Path(target_path)
    if not source_file.is_file():
        raise FileNotFoundError(source_file)
    if source_file.resolve() == target_file.resolve():
        raise ValueError("source snapshot and target database must differ")

    target = init_db(target_file)
    source = _connect_read_only(source_file)
    counts = {table.name: 0 for table in CAPTURE_TABLES}
    now_ms = imported_at if imported_at is not None else int(time.time() * 1000)
    try:
        dataset_id = _validate_source(source, target)
        _validate_host(source, expected_host)
        with target:
            for table in CAPTURE_TABLES:
                columns_sql = ", ".join(table.columns)
                closed_sql = " AND source_row.gap_end_utc IS NOT NULL" if table.closed_only else ""
                imported_ids = {
                    int(row[0])
                    for row in target.execute(
                        "SELECT source_row_id FROM sync_ingest_rows "
                        "WHERE source_dataset_id = ? AND source_table = ?",
                        (dataset_id, table.name),
                    )
                }
                rows = source.execute(
                    f"SELECT source_row.id, {columns_sql} FROM {table.name} AS source_row "
                    f"WHERE 1 = 1{closed_sql} ORDER BY source_row.id"
                )
                placeholders = ", ".join("?" for _ in table.columns)
                for row in rows:
                    if row["id"] in imported_ids:
                        continue
                    cursor = target.execute(
                        f"INSERT OR IGNORE INTO {table.name} ({columns_sql}) "
                        f"VALUES ({placeholders})",
                        tuple(row[column] for column in table.columns),
                    )
                    canonical_id = cursor.lastrowid if cursor.rowcount else None
                    target.execute(
                        "INSERT INTO sync_ingest_rows "
                        "(source_dataset_id, source_table, source_row_id, canonical_row_id, imported_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (dataset_id, table.name, row["id"], canonical_id, now_ms),
                    )
                    counts[table.name] += 1
    finally:
        source.close()
        target.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="transferred Pi snapshot")
    parser.add_argument("--target", required=True, help="Mac-canonical SQLite database")
    parser.add_argument("--expected-host", default="pi")
    args = parser.parse_args()
    print(
        json.dumps(
            import_snapshot(args.source, args.target, expected_host=args.expected_host),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
