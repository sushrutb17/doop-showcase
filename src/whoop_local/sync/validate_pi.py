"""Fail-closed acceptance checks for a real Raspberry Pi capture deployment."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from whoop_local.db.schema import SCHEMA_VERSION
from whoop_local.sync.import_snapshot import CAPTURE_TABLES

MAC_OWNED_TABLES = (
    "sessions",
    "metrics_daily",
    "sync_ingest_rows",
    "imported_daily_metrics",
    "imported_sleep",
    "imported_workouts",
    "imported_hr_samples",
    "exercise_catalog",
    "external_exercises",
    "external_exercise_reviews",
    "workout_sessions",
    "workout_exercises",
    "workout_sets",
    "training_plans",
    "plan_workouts",
    "plan_exercises",
    "manual_workout_drafts",
    "coaching_proposals",
)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _scalar(connection: sqlite3.Connection, sql: str, parameters: tuple = ()):
    row = connection.execute(sql, parameters).fetchone()
    return row[0] if row else None


def _inspect_database(path: Path) -> tuple[dict, list[str]]:
    report: dict = {"path": str(path), "exists": path.is_file()}
    errors: list[str] = []
    if not path.is_file():
        return report, [f"database does not exist: {path}"]

    try:
        connection = _connect_read_only(path)
    except sqlite3.Error as exc:
        return report, [f"database cannot be opened read-only: {path}: {exc}"]
    try:
        integrity = _scalar(connection, "PRAGMA integrity_check")
        report["integrity"] = integrity
        if integrity != "ok":
            errors.append(f"database integrity check failed for {path}: {integrity}")
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        report["foreign_key_violations"] = len(foreign_keys)
        if foreign_keys:
            errors.append(f"database has foreign-key violations: {path}")

        schema_version = _scalar(connection, "SELECT value FROM meta WHERE key = 'schema_version'")
        dataset_id = _scalar(connection, "SELECT value FROM meta WHERE key = 'dataset_id'")
        report["schema_version"] = schema_version
        report["dataset_id"] = dataset_id
        if schema_version != SCHEMA_VERSION:
            errors.append(
                f"database schema must be {SCHEMA_VERSION}; got {schema_version or 'missing'}: {path}"
            )
        if not dataset_id:
            errors.append(f"database has no stable dataset_id: {path}")

        latest_capture_ms = max(
            value or 0
            for value in (
                _scalar(connection, "SELECT MAX(ts_utc) FROM raw_packets"),
                _scalar(connection, "SELECT MAX(ts_utc) FROM hr_samples"),
                _scalar(connection, "SELECT MAX(ts_utc) FROM rr_intervals"),
            )
        )
        report["latest_capture_ms"] = latest_capture_ms or None
        report["raw_packets"] = _scalar(connection, "SELECT COUNT(*) FROM raw_packets")
        report["hr_samples"] = _scalar(connection, "SELECT COUNT(*) FROM hr_samples")

        unexpected_hosts: dict[str, list[str]] = {}
        for table in CAPTURE_TABLES:
            values = [
                str(row[0])
                for row in connection.execute(
                    f"SELECT DISTINCT {table.host_column} FROM {table.name} "
                    f"WHERE {table.host_column} != 'pi'"
                )
            ]
            if values:
                unexpected_hosts[table.name] = values
        report["unexpected_capture_hosts"] = unexpected_hosts
        if unexpected_hosts:
            errors.append(f"capture rows are not exclusively tagged 'pi': {unexpected_hosts}")

        pi_devices = _scalar(
            connection,
            "SELECT COUNT(*) FROM device "
            "WHERE host = 'pi' AND identifier IS NOT NULL AND TRIM(identifier) != ''",
        )
        report["saved_pi_devices"] = pi_devices
        if not pi_devices:
            errors.append("no saved Pi/BlueZ device identifier")

        orphaned_open_gaps = 0
        if latest_capture_ms:
            orphaned_open_gaps = _scalar(
                connection,
                "SELECT COUNT(*) FROM capture_gaps "
                "WHERE source_host = 'pi' AND gap_end_utc IS NULL AND gap_start_utc < ?",
                (latest_capture_ms,),
            )
        report["orphaned_open_gaps"] = orphaned_open_gaps
        if orphaned_open_gaps:
            errors.append("Pi database contains an open gap older than its latest capture")

        mac_owned_counts = {
            table: _scalar(connection, f"SELECT COUNT(*) FROM {table}")
            for table in MAC_OWNED_TABLES
        }
        report["mac_owned_rows"] = sum(mac_owned_counts.values())
        nonempty = {table: count for table, count in mac_owned_counts.items() if count}
        if nonempty:
            errors.append(f"Pi database contains Mac-owned/private rows: {nonempty}")
    except sqlite3.Error as exc:
        errors.append(f"database inspection failed for {path}: {exc}")
    finally:
        connection.close()
    return report, errors


def validate_pi_deployment(
    live_db: Path | str,
    snapshot_db: Path | str,
    *,
    now_ms: int | None = None,
    max_capture_age_minutes: int = 20,
    max_snapshot_age_minutes: int = 20,
    require_capture_after_ms: int | None = None,
) -> dict:
    """Return a secret-free acceptance report for live + snapshot Pi databases."""
    live_path = Path(live_db)
    snapshot_path = Path(snapshot_db)
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    live, live_errors = _inspect_database(live_path)
    snapshot, snapshot_errors = _inspect_database(snapshot_path)
    errors = live_errors + snapshot_errors

    max_capture_age_ms = max_capture_age_minutes * 60_000
    latest_live = live.get("latest_capture_ms")
    if not latest_live:
        errors.append("live Pi database contains no capture rows")
    else:
        live["capture_age_ms"] = current_ms - latest_live
        if current_ms - latest_live > max_capture_age_ms:
            errors.append("live Pi capture is stale")
        if require_capture_after_ms is not None and latest_live <= require_capture_after_ms:
            errors.append("no Pi capture exists after the required restart/cutover timestamp")

    if snapshot_path.is_file():
        snapshot_age_ms = current_ms - int(snapshot_path.stat().st_mtime * 1000)
        snapshot["file_age_ms"] = snapshot_age_ms
        if snapshot_age_ms > max_snapshot_age_minutes * 60_000:
            errors.append("Pi snapshot file is stale")

    latest_snapshot = snapshot.get("latest_capture_ms")
    if not latest_snapshot:
        errors.append("Pi snapshot contains no capture rows")
    elif latest_live and latest_snapshot > latest_live:
        errors.append("Pi snapshot contains capture time newer than the live database")
    elif current_ms - latest_snapshot > max_capture_age_ms + max_snapshot_age_minutes * 60_000:
        errors.append("Pi snapshot capture content is stale")

    live_dataset_id = live.get("dataset_id")
    snapshot_dataset_id = snapshot.get("dataset_id")
    if live_dataset_id and snapshot_dataset_id and live_dataset_id != snapshot_dataset_id:
        errors.append("live Pi database and snapshot have different dataset IDs")

    return {
        "passed": not errors,
        "errors": errors,
        "live": live,
        "snapshot": snapshot,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("/var/lib/doop/whoop.db"))
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=Path("/var/lib/doop/snapshots/pi-latest.db"),
    )
    parser.add_argument("--max-capture-age-minutes", type=int, default=20)
    parser.add_argument("--max-snapshot-age-minutes", type=int, default=20)
    parser.add_argument("--require-capture-after-ms", type=int)
    args = parser.parse_args()
    if args.max_capture_age_minutes <= 0 or args.max_snapshot_age_minutes <= 0:
        parser.error("age limits must be positive")
    report = validate_pi_deployment(
        args.db,
        args.snapshot,
        max_capture_age_minutes=args.max_capture_age_minutes,
        max_snapshot_age_minutes=args.max_snapshot_age_minutes,
        require_capture_after_ms=args.require_capture_after_ms,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
