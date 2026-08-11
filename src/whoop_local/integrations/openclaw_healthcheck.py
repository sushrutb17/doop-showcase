"""Read-only readiness probe for OpenClaw's primary Doop MCP process."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from whoop_local.config import DEFAULT_DB_PATH
from whoop_local.db.schema import SCHEMA_VERSION
from whoop_local.mcp_server.queries import get_status

REQUIRED_TABLES = frozenset(
    {
        "meta",
        "raw_packets",
        "hr_samples",
        "rr_intervals",
        "metrics_daily",
        "device",
        "capture_gaps",
        "battery_samples",
        "imported_sleep",
        "imported_workouts",
        "workout_sessions",
        "workout_exercises",
        "workout_sets",
        "training_plans",
        "plan_workouts",
        "plan_exercises",
    }
)


def check_readiness(db_path: Path | str) -> list[str]:
    """Return secret-free failures without mutating or creating the database."""
    path = Path(db_path)
    if not path.is_file():
        return ["database is missing"]
    try:
        connection = sqlite3.connect(
            f"file:{quote(str(path.resolve()))}?mode=ro", uri=True, timeout=1
        )
    except sqlite3.Error:
        return ["database cannot be opened read-only"]

    errors: list[str] = []
    try:
        version_row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        version = str(version_row[0]) if version_row else None
        if version != SCHEMA_VERSION:
            errors.append(f"schema version must be {SCHEMA_VERSION}")

        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            errors.append("required tables are missing: " + ", ".join(missing))
        if not errors:
            get_status(connection, str(path))
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        errors.append("primary Doop query preflight failed")
    finally:
        connection.close()
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    errors = check_readiness(args.db)
    print(json.dumps({"ready": not errors, "errors": errors}, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
