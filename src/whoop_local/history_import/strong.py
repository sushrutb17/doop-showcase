"""Import detailed Strong workout history into the canonical logbook (D40).

Strong CSV exports contain one row per set. The importer groups rows into
sessions and ordered exercise blocks, preserves the original values, and is
idempotent: re-importing replaces the Strong-owned children of each matching
session without touching manual or agent-created workouts.

Strong does not include a weight unit in its CSV. Callers must choose ``kg``,
``lb``, or ``unknown`` explicitly; silently guessing would corrupt every
weighted set in the import.

Usage:
    python -m whoop_local.history_import.strong \
        --csv /path/to/strong_workouts.csv --weight-unit kg
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from whoop_local.config import DEFAULT_DB_PATH, local_timezone
from whoop_local.db.schema import init_db

log = logging.getLogger("whoop_local.history_import.strong")

SOURCE = "strong_csv"


def normalize_exercise_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def _number(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)


def parse_duration_seconds(value: str | None) -> int | None:
    """Parse Strong durations such as ``1h 18min``, ``47min``, or ``30s``."""
    if not value or not value.strip():
        return None
    total = 0
    for token in value.strip().lower().split():
        if token.endswith("min"):
            total += int(float(token[:-3]) * 60)
        elif token.endswith("h"):
            total += int(float(token[:-1]) * 3600)
        elif token.endswith("s"):
            total += int(float(token[:-1]))
        else:
            raise ValueError(f"Unsupported Strong duration token: {token!r}")
    return total


@dataclass
class StrongSession:
    date_text: str
    name: str
    duration_text: str
    notes: str | None = None
    rows: list[dict[str, str]] = field(default_factory=list)

    @property
    def external_id(self) -> str:
        material = "\0".join((self.date_text, self.name, self.duration_text))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def read_sessions(csv_path: str) -> list[StrongSession]:
    grouped: OrderedDict[tuple[str, str, str], StrongSession] = OrderedDict()
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"Date", "Workout Name", "Duration", "Exercise Name", "Set Order"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Strong CSV missing required columns: {sorted(missing)}")
        for row in reader:
            key = (row["Date"].strip(), row["Workout Name"].strip(), row["Duration"].strip())
            session = grouped.get(key)
            if session is None:
                session = StrongSession(*key)
                grouped[key] = session
            workout_notes = (row.get("Workout Notes") or "").strip()
            if workout_notes and not session.notes:
                session.notes = workout_notes
            session.rows.append(row)
    return list(grouped.values())


def _exercise_id(conn, name: str, imported_at: int) -> tuple[int, bool]:
    normalized = normalize_exercise_name(name)
    row = conn.execute(
        "SELECT id FROM exercise_catalog WHERE normalized_name = ?", (normalized,)
    ).fetchone()
    if row:
        return row[0], False
    cur = conn.execute(
        "INSERT INTO exercise_catalog (canonical_name, normalized_name, created_at) VALUES (?, ?, ?)",
        (" ".join(name.split()), normalized, imported_at),
    )
    return cur.lastrowid, True


def import_into_db(conn, csv_path: str, weight_unit: str) -> dict[str, int]:
    if weight_unit not in {"kg", "lb", "unknown"}:
        raise ValueError("weight_unit must be 'kg', 'lb', or 'unknown'")
    sessions = read_sessions(csv_path)
    imported_at = int(time.time() * 1000)
    tz = local_timezone()
    counts = {
        "sessions": 0,
        "exercise_blocks": 0,
        "sets": 0,
        "rest_events": 0,
        "new_exercises": 0,
    }

    for session in sessions:
        start_local = datetime.strptime(session.date_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        duration_s = parse_duration_seconds(session.duration_text)
        end_local = start_local + timedelta(seconds=duration_s) if duration_s is not None else None
        started_ms = int(start_local.timestamp() * 1000)
        ended_ms = int(end_local.timestamp() * 1000) if end_local else None

        conn.execute(
            "INSERT INTO workout_sessions "
            "(started_at_utc, ended_at_utc, local_date, name, source, source_external_id, notes, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source, source_external_id) DO UPDATE SET "
            "started_at_utc=excluded.started_at_utc, ended_at_utc=excluded.ended_at_utc, "
            "local_date=excluded.local_date, name=excluded.name, notes=excluded.notes, "
            "imported_at=excluded.imported_at",
            (
                started_ms,
                ended_ms,
                start_local.date().isoformat(),
                session.name or "Workout",
                SOURCE,
                session.external_id,
                session.notes,
                imported_at,
            ),
        )
        session_id = conn.execute(
            "SELECT id FROM workout_sessions WHERE source = ? AND source_external_id = ?",
            (SOURCE, session.external_id),
        ).fetchone()[0]

        # The session is Strong-owned; rebuilding its children makes edits to
        # an export converge cleanly while preserving every non-Strong session.
        conn.execute("DELETE FROM workout_exercises WHERE workout_session_id = ?", (session_id,))

        exercise_order = 0
        current_name: str | None = None
        workout_exercise_id: int | None = None
        for row_index, row in enumerate(session.rows, start=1):
            exercise_name = (row.get("Exercise Name") or "").strip()
            if not exercise_name:
                continue
            if exercise_name != current_name:
                exercise_order += 1
                exercise_id, created = _exercise_id(conn, exercise_name, imported_at)
                counts["new_exercises"] += int(created)
                exercise_note = (row.get("Notes") or "").strip() or None
                cur = conn.execute(
                    "INSERT INTO workout_exercises "
                    "(workout_session_id, exercise_id, exercise_order, notes) VALUES (?, ?, ?, ?)",
                    (session_id, exercise_id, exercise_order, exercise_note),
                )
                workout_exercise_id = cur.lastrowid
                current_name = exercise_name
                counts["exercise_blocks"] += 1

            row_material = "\0".join(
                [session.external_id, str(row_index)] + [row.get(k, "") or "" for k in sorted(row)]
            )
            row_hash = hashlib.sha256(row_material.encode("utf-8")).hexdigest()
            set_order_raw = (row.get("Set Order") or "").strip()
            try:
                set_order = int(float(set_order_raw))
                set_type = "work"
            except ValueError:
                # Strong exports rest timers as rows whose Set Order is the
                # literal string "Rest Timer". Preserve them in sequence;
                # they are useful for future pacing/coaching analysis.
                set_order = row_index
                set_type = "rest"
                counts["rest_events"] += 1
            set_note = (row.get("Notes") or "").strip() or None
            conn.execute(
                "INSERT INTO workout_sets "
                "(workout_exercise_id, set_order, sequence_order, set_type, source_set_label, "
                "weight, weight_unit, reps, distance, seconds, rpe, notes, source_row_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workout_exercise_id,
                    set_order,
                    row_index,
                    set_type,
                    set_order_raw or None,
                    _number(row.get("Weight")),
                    weight_unit,
                    _number(row.get("Reps")),
                    _number(row.get("Distance")),
                    _number(row.get("Seconds")),
                    _number(row.get("RPE")),
                    set_note,
                    row_hash,
                ),
            )
            counts["sets"] += 1
        counts["sessions"] += 1

    conn.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Strong workout CSV export")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument(
        "--weight-unit",
        choices=("kg", "lb", "unknown"),
        required=True,
        help="Strong omits the unit from CSV, so an explicit choice is required",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    conn = init_db(args.db)
    counts = import_into_db(conn, args.csv, args.weight_unit)
    log.info("Imported Strong history: %s", counts)


if __name__ == "__main__":
    main()
