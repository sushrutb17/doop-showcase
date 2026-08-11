"""Prepare, inspect, confirm, or cancel manual workout entries (D47).

Prepared drafts do not write to the canonical workout logbook. Only
``confirm_draft`` materializes a pending draft, atomically, after an explicit
user action. This module intentionally has no MCP write tool.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime
from typing import Any

from whoop_local.history_import.strong import normalize_exercise_name

SOURCE = "manual"
ALLOWED_UNITS = {"kg", "lb", "bodyweight"}
ALLOWED_SET_FIELDS = {"weight", "weight_unit", "reps", "seconds", "rpe", "rir", "notes"}


def _utc_ms(value: str, field: str) -> tuple[int, str]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp with timezone") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    return int(parsed.timestamp() * 1000), parsed.date().isoformat()


def _number(value: Any, field: str, *, positive: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    value = float(value)
    if positive and value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _catalog_exercise(conn: sqlite3.Connection, name: str) -> tuple[int, str]:
    normalized = normalize_exercise_name(name)
    row = conn.execute(
        "SELECT id, canonical_name FROM exercise_catalog WHERE normalized_name = ?", (normalized,)
    ).fetchone()
    if row is None:
        suggestions = [
            name
            for (name,) in conn.execute(
                "SELECT canonical_name FROM exercise_catalog WHERE normalized_name LIKE ? "
                "ORDER BY canonical_name LIMIT 5",
                (f"%{normalized}%",),
            )
        ]
        suffix = f" Suggestions: {suggestions}." if suggestions else ""
        raise ValueError(f"Unknown canonical exercise {name!r}.{suffix}")
    return row[0], row[1]


def validate_payload(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize and validate a manual log payload without writing any workout."""
    if not isinstance(payload, dict):
        raise ValueError("Workout payload must be an object")
    name = payload.get("name")
    started_at = payload.get("started_at")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if not isinstance(started_at, str):
        raise ValueError("started_at is required")
    started_at_utc, local_date = _utc_ms(started_at, "started_at")
    ended_at = payload.get("ended_at")
    ended_at_utc = None
    if ended_at is not None:
        if not isinstance(ended_at, str):
            raise ValueError("ended_at must be an ISO-8601 timestamp with timezone")
        ended_at_utc, _ = _utc_ms(ended_at, "ended_at")
        if ended_at_utc < started_at_utc:
            raise ValueError("ended_at cannot be before started_at")
    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("notes must be text when provided")
    exercises = payload.get("exercises")
    if not isinstance(exercises, list) or not exercises:
        raise ValueError("at least one exercise is required")

    normalized_exercises = []
    for exercise_index, exercise in enumerate(exercises, start=1):
        if not isinstance(exercise, dict) or not isinstance(exercise.get("name"), str):
            raise ValueError(f"exercises[{exercise_index}].name is required")
        exercise_id, canonical_name = _catalog_exercise(conn, exercise["name"])
        exercise_notes = exercise.get("notes")
        if exercise_notes is not None and not isinstance(exercise_notes, str):
            raise ValueError(f"exercises[{exercise_index}].notes must be text")
        sets = exercise.get("sets")
        if not isinstance(sets, list) or not sets:
            raise ValueError(f"exercises[{exercise_index}] needs at least one set")
        normalized_sets = []
        for set_index, workout_set in enumerate(sets, start=1):
            if not isinstance(workout_set, dict):
                raise ValueError(f"set {exercise_index}.{set_index} must be an object")
            unexpected = set(workout_set) - ALLOWED_SET_FIELDS
            if unexpected:
                raise ValueError(
                    f"set {exercise_index}.{set_index} has unsupported fields: {sorted(unexpected)}"
                )
            weight = _number(
                workout_set.get("weight"), f"set {exercise_index}.{set_index}.weight", positive=True
            )
            reps = _number(
                workout_set.get("reps"), f"set {exercise_index}.{set_index}.reps", positive=True
            )
            seconds = _number(
                workout_set.get("seconds"),
                f"set {exercise_index}.{set_index}.seconds",
                positive=True,
            )
            rpe = _number(
                workout_set.get("rpe"), f"set {exercise_index}.{set_index}.rpe", positive=True
            )
            rir = _number(workout_set.get("rir"), f"set {exercise_index}.{set_index}.rir")
            if rpe is not None and rpe > 10:
                raise ValueError(f"set {exercise_index}.{set_index}.rpe must be at most 10")
            if rir is not None and rir < 0:
                raise ValueError(f"set {exercise_index}.{set_index}.rir cannot be negative")
            if reps is None and seconds is None:
                raise ValueError(f"set {exercise_index}.{set_index} needs reps or seconds")
            unit = workout_set.get("weight_unit")
            if weight is not None:
                if unit not in ALLOWED_UNITS:
                    allowed = ", ".join(sorted(ALLOWED_UNITS))
                    raise ValueError(
                        f"weighted manual sets require an explicit supported weight_unit: {allowed}"
                    )
            elif unit is not None:
                raise ValueError("weight_unit is only allowed when weight is provided")
            set_notes = workout_set.get("notes")
            if set_notes is not None and not isinstance(set_notes, str):
                raise ValueError(f"set {exercise_index}.{set_index}.notes must be text")
            normalized_sets.append(
                {
                    "weight": weight,
                    "weight_unit": unit or "bodyweight",
                    "reps": reps,
                    "seconds": seconds,
                    "rpe": rpe,
                    "rir": rir,
                    "notes": set_notes,
                }
            )
        normalized_exercises.append(
            {
                "exercise_id": exercise_id,
                "name": canonical_name,
                "notes": exercise_notes,
                "sets": normalized_sets,
            }
        )
    return {
        "name": " ".join(name.split()),
        "started_at_utc": started_at_utc,
        "ended_at_utc": ended_at_utc,
        "local_date": local_date,
        "notes": notes,
        "exercises": normalized_exercises,
    }


def prepare_draft(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Store a validated pending draft; no canonical workout rows are written."""
    normalized = validate_payload(conn, payload)
    draft_id = str(uuid.uuid4())
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO manual_workout_drafts (id, payload_json, status, created_at) VALUES (?, ?, 'pending', ?)",
        (draft_id, json.dumps(normalized, sort_keys=True), now),
    )
    conn.commit()
    return {
        "draft_id": draft_id,
        "status": "pending",
        "workout": normalized,
        "confirmation_required": True,
    }


def get_draft(conn: sqlite3.Connection, draft_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_json, status, created_at, confirmed_at, workout_session_id "
        "FROM manual_workout_drafts WHERE id = ?",
        (draft_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Manual workout draft {draft_id!r} was not found")
    payload_json, status, created_at, confirmed_at, workout_session_id = row
    return {
        "draft_id": draft_id,
        "status": status,
        "created_at": created_at,
        "confirmed_at": confirmed_at,
        "workout_session_id": workout_session_id,
        "workout": json.loads(payload_json),
        "confirmation_required": status == "pending",
    }


def confirm_draft(conn: sqlite3.Connection, draft_id: str) -> dict[str, Any]:
    """Materialize exactly one pending draft into canonical workout rows."""
    draft = get_draft(conn, draft_id)
    if draft["status"] != "pending":
        raise ValueError(
            f"Draft {draft_id} is {draft['status']}; only pending drafts can be confirmed"
        )
    workout = draft["workout"]
    now = int(time.time() * 1000)
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            "INSERT INTO workout_sessions "
            "(started_at_utc, ended_at_utc, local_date, name, source, source_external_id, notes, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workout["started_at_utc"],
                workout["ended_at_utc"],
                workout["local_date"],
                workout["name"],
                SOURCE,
                f"draft:{draft_id}",
                workout["notes"],
                now,
            ),
        )
        session_id = cur.lastrowid
        for exercise_order, exercise in enumerate(workout["exercises"], start=1):
            cur = conn.execute(
                "INSERT INTO workout_exercises (workout_session_id, exercise_id, exercise_order, notes) "
                "VALUES (?, ?, ?, ?)",
                (session_id, exercise["exercise_id"], exercise_order, exercise["notes"]),
            )
            workout_exercise_id = cur.lastrowid
            for set_order, workout_set in enumerate(exercise["sets"], start=1):
                source_hash = hashlib.sha256(
                    f"{draft_id}:{exercise_order}:{set_order}".encode()
                ).hexdigest()
                conn.execute(
                    "INSERT INTO workout_sets "
                    "(workout_exercise_id, set_order, sequence_order, set_type, weight, weight_unit, reps, "
                    "seconds, rpe, rir, notes, source_row_hash) "
                    "VALUES (?, ?, ?, 'work', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        workout_exercise_id,
                        set_order,
                        set_order,
                        workout_set["weight"],
                        workout_set["weight_unit"],
                        workout_set["reps"],
                        workout_set["seconds"],
                        workout_set["rpe"],
                        workout_set["rir"],
                        workout_set["notes"],
                        source_hash,
                    ),
                )
        updated = conn.execute(
            "UPDATE manual_workout_drafts SET status='confirmed', confirmed_at=?, workout_session_id=? "
            "WHERE id=? AND status='pending'",
            (now, session_id, draft_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("Draft confirmation was superseded")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"draft_id": draft_id, "status": "confirmed", "workout_session_id": session_id}


def cancel_draft(conn: sqlite3.Connection, draft_id: str) -> dict[str, Any]:
    """Cancel a pending draft. Completed logs are immutable through this API."""
    updated = conn.execute(
        "UPDATE manual_workout_drafts SET status='cancelled' WHERE id=? AND status='pending'",
        (draft_id,),
    )
    if updated.rowcount != 1:
        raise ValueError(f"Draft {draft_id} is not pending and cannot be cancelled")
    conn.commit()
    return {"draft_id": draft_id, "status": "cancelled"}
