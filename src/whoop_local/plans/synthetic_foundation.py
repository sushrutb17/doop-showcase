"""Seed an obviously fictional, generic showcase training plan.

This module creates an *intended* plan only. It does not log completed work,
change historic workouts, or make recovery-driven adjustments. The schedule,
loads, and exercise selection are synthetic examples rather than an athlete
profile.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from typing import TypedDict

from whoop_local.config import DEFAULT_DB_PATH
from whoop_local.db.schema import init_db
from whoop_local.history_import.strong import normalize_exercise_name

PLAN_NAME = "Synthetic Foundation"
PLAN_VERSION = 1
PLAN_SOURCE = "showcase_synthetic_foundation_v1"

PLAN_NOTES = (
    "Fictional template with two full-body sessions, one aerobic-base session, and optional "
    "mobility. Keep 2–3 reps in reserve and stop well before failure. "
    "This is an intended prescription, not a completed workout log."
)

ExercisePrescription = tuple[
    str,
    int,
    int,
    int,
    str | None,
    int,
    int | float | None,
    str,
    str | None,
]


class WorkoutDefinition(TypedDict):
    weekday: int
    name: str
    kind: str
    intensity: str
    estimated_minutes: int
    optional: bool
    notes: str
    exercises: tuple[ExercisePrescription, ...]


WORKOUTS: tuple[WorkoutDefinition, ...] = (
    {
        "weekday": 0,
        "name": "Foundation A",
        "kind": "strength",
        "intensity": "moderate",
        "estimated_minutes": 55,
        "optional": False,
        "notes": "Fictional full-body session. Use controlled technique and repeatable loads.",
        "exercises": (
            ("Goblet Squat (Dumbbell)", 3, 8, 10, "3010", 90, 16, "kg", None),
            ("Push Up", 3, 8, 12, "2010", 75, None, "bodyweight", None),
            ("Seated Row (Cable)", 3, 8, 12, "2011", 75, 25, "kg", None),
            (
                "Romanian Deadlift (Dumbbell)",
                2,
                8,
                10,
                "3010",
                90,
                14,
                "kg",
                "The listed load is per dumbbell.",
            ),
        ),
    },
    {
        "weekday": 2,
        "name": "Aerobic base",
        "kind": "sport",
        "intensity": "easy",
        "estimated_minutes": 35,
        "optional": False,
        "notes": "Fictional steady session at a conversational effort.",
        "exercises": (),
    },
    {
        "weekday": 4,
        "name": "Foundation B",
        "kind": "strength",
        "intensity": "moderate",
        "estimated_minutes": 55,
        "optional": False,
        "notes": "Fictional full-body session. Finish every set with clean technique.",
        "exercises": (
            ("Leg Press", 3, 8, 10, "3010", 90, 60, "kg", None),
            (
                "Bench Press (Dumbbell)",
                3,
                8,
                10,
                "3010",
                90,
                12,
                "kg",
                "The listed load is per dumbbell.",
            ),
            ("Lat Pulldown (Cable)", 3, 8, 12, "2011", 75, 30, "kg", None),
            ("Hip Thrust (Barbell)", 2, 10, 12, "2011", 90, 35, "kg", None),
        ),
    },
    {
        "weekday": 6,
        "name": "Mobility reset",
        "kind": "recovery",
        "intensity": "easy",
        "estimated_minutes": 20,
        "optional": True,
        "notes": "Optional fictional mobility session; keep the effort easy.",
        "exercises": (),
    },
)


def _exercise_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM exercise_catalog WHERE normalized_name = ?",
        (normalize_exercise_name(name),),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"Synthetic plan requires catalog exercise {name!r}; seed the catalog first "
            "or import your own exercise history."
        )
    return row[0]


def seed_synthetic_foundation_plan(conn: sqlite3.Connection) -> dict[str, int]:
    """Idempotently create the active fictional plan and its prescription."""
    created_at = int(time.time() * 1000)
    conn.execute("UPDATE training_plans SET status = 'archived' WHERE status = 'active'")
    conn.execute(
        "INSERT INTO training_plans (name, version, status, source, notes, created_at) "
        "VALUES (?, ?, 'active', ?, ?, ?) "
        "ON CONFLICT(name, version) DO UPDATE SET status='active', source=excluded.source, notes=excluded.notes",
        (PLAN_NAME, PLAN_VERSION, PLAN_SOURCE, PLAN_NOTES, created_at),
    )
    plan_id = conn.execute(
        "SELECT id FROM training_plans WHERE name = ? AND version = ?",
        (PLAN_NAME, PLAN_VERSION),
    ).fetchone()[0]
    conn.execute("DELETE FROM plan_workouts WHERE training_plan_id = ?", (plan_id,))

    exercise_count = 0
    for display_order, workout in enumerate(WORKOUTS, start=1):
        cur = conn.execute(
            "INSERT INTO plan_workouts "
            "(training_plan_id, weekday, display_order, name, kind, intensity, estimated_minutes, optional, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                workout["weekday"],
                display_order,
                workout["name"],
                workout["kind"],
                workout["intensity"],
                workout["estimated_minutes"],
                int(workout["optional"]),
                workout["notes"],
            ),
        )
        for exercise_order, exercise in enumerate(workout["exercises"], start=1):
            (
                name,
                sets_target,
                reps_min,
                reps_max,
                tempo,
                rest_seconds,
                starting_weight,
                weight_unit,
                substitution,
            ) = exercise
            conn.execute(
                "INSERT INTO plan_exercises "
                "(plan_workout_id, exercise_id, exercise_order, sets_target, reps_min, reps_max, tempo, "
                "rest_seconds, starting_weight, weight_unit, substitution_note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cur.lastrowid,
                    _exercise_id(conn, name),
                    exercise_order,
                    sets_target,
                    reps_min,
                    reps_max,
                    tempo,
                    rest_seconds,
                    starting_weight,
                    weight_unit,
                    substitution,
                ),
            )
            exercise_count += 1
    conn.commit()
    return {"plan_id": plan_id, "workouts": len(WORKOUTS), "exercises": exercise_count}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()
    conn = init_db(args.db)
    try:
        print(seed_synthetic_foundation_plan(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
