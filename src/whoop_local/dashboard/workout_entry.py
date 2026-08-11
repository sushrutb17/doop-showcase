"""Pure helpers for turning an active plan into a reviewed manual-log payload."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any


def plan_log_rows(active_plan: dict, workout_name: str) -> list[dict[str, Any]]:
    workout = next(
        (item for item in active_plan.get("workouts", []) if item["name"] == workout_name), None
    )
    if workout is None:
        raise ValueError(f"Workout {workout_name!r} is not in the active plan")
    rows = []
    for exercise in workout["exercises"]:
        for set_number in range(1, exercise["sets_target"] + 1):
            rows.append(
                {
                    "Include": True,
                    "Exercise": exercise["name"],
                    "Set": set_number,
                    "Weight": exercise["starting_weight"],
                    "Unit": exercise.get("weight_unit") or "bodyweight",
                    "Reps": exercise["reps"]["min"],
                    "RIR": 2,
                    "RPE": None,
                    "Notes": "",
                }
            )
    return rows


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected a number, received {value!r}") from exc
    return None if math.isnan(number) else number


def rows_to_payload(
    rows: list[dict[str, Any]],
    workout_name: str,
    workout_date: date,
    start_time: time,
    duration_minutes: int,
    timezone,
    notes: str = "",
) -> dict[str, Any]:
    """Convert edited table rows to D47's strict prepare-draft contract."""
    if duration_minutes <= 0:
        raise ValueError("Duration must be positive")
    included = [row for row in rows if bool(row.get("Include", True))]
    if not included:
        raise ValueError("Include at least one completed set")
    started = datetime.combine(workout_date, start_time, tzinfo=timezone)
    ended = started + timedelta(minutes=duration_minutes)
    exercises: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for row in included:
        exercise_name = str(row.get("Exercise") or "").strip()
        if not exercise_name:
            raise ValueError("Every included set needs an exercise")
        exercise = by_name.get(exercise_name)
        if exercise is None:
            exercise = {"name": exercise_name, "sets": []}
            by_name[exercise_name] = exercise
            exercises.append(exercise)
        weight = _optional_number(row.get("Weight"))
        unit = str(row.get("Unit") or "").strip().casefold()
        workout_set: dict[str, Any] = {
            "reps": _optional_number(row.get("Reps")),
            "rir": _optional_number(row.get("RIR")),
            "rpe": _optional_number(row.get("RPE")),
        }
        if weight is not None:
            if not unit:
                raise ValueError(f"Every weighted set for {exercise_name!r} needs a plan unit")
            workout_set.update({"weight": weight, "weight_unit": unit})
        set_notes = str(row.get("Notes") or "").strip()
        if set_notes:
            workout_set["notes"] = set_notes
        exercise["sets"].append(workout_set)
    return {
        "name": workout_name,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "notes": notes.strip() or None,
        "exercises": exercises,
    }
