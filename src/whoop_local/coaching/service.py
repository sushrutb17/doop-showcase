"""Create and resolve conservative plan-progression proposals (D48)."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

from whoop_local.history_import.strong import normalize_exercise_name


def _active_plan(conn: sqlite3.Connection) -> tuple:
    row = conn.execute(
        "SELECT id, name, version, source, notes FROM training_plans WHERE status='active' "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("No active plan exists")
    return row


def propose_weight_progression(
    conn: sqlite3.Connection, workout_name: str, exercise_name: str
) -> dict:
    """Propose a small increase only after two clearly successful sessions.

    Both sessions must meet the prescription's set count/reps and record >=2
    RIR on every relevant set, using the plan's explicit kg or lb unit. This
    intentionally returns an error rather than inventing confidence from
    incomplete historical data or converting between units.
    """
    plan_id, plan_name, plan_version, _source, _notes = _active_plan(conn)
    normalized = normalize_exercise_name(exercise_name)
    target = conn.execute(
        "SELECT pw.id, pw.name, pe.id, ec.id, ec.canonical_name, pe.sets_target, pe.reps_max, "
        "pe.starting_weight, pe.weight_unit "
        "FROM plan_workouts pw JOIN plan_exercises pe ON pe.plan_workout_id=pw.id "
        "JOIN exercise_catalog ec ON ec.id=pe.exercise_id "
        "WHERE pw.training_plan_id=? AND lower(pw.name)=? AND ec.normalized_name=?",
        (plan_id, workout_name.casefold(), normalized),
    ).fetchone()
    if target is None:
        raise ValueError("Exercise is not prescribed in the named active-plan workout")
    (
        workout_id,
        canonical_workout,
        plan_exercise_id,
        exercise_id,
        canonical_exercise,
        sets_target,
        reps_max,
        old_weight,
        unit,
    ) = target
    if unit not in {"kg", "lb"} or old_weight is None or reps_max is None:
        raise ValueError(
            "Only kg- or lb-based, rep-targeted prescriptions can receive a progression proposal"
        )

    sessions = conn.execute(
        "SELECT ws.id, ws.local_date, ws.name, COUNT(*), MIN(sets.weight), MIN(sets.reps), MIN(sets.rir) "
        "FROM workout_sessions ws JOIN workout_exercises we ON we.workout_session_id=ws.id "
        "JOIN workout_sets sets ON sets.workout_exercise_id=we.id "
        "WHERE we.exercise_id=? AND sets.set_type='work' AND sets.weight_unit=? "
        "GROUP BY ws.id ORDER BY ws.started_at_utc DESC LIMIT 2",
        (exercise_id, unit),
    ).fetchall()
    if len(sessions) < 2:
        raise ValueError(
            f"Need two completed {unit}-labelled sessions before proposing progression"
        )
    successes = []
    for session_id, local_date, session_name, set_count, min_weight, min_reps, min_rir in sessions:
        success = (
            set_count >= sets_target
            and min_weight >= old_weight
            and min_reps >= reps_max
            and min_rir is not None
            and min_rir >= 2
        )
        successes.append(
            {
                "session_id": session_id,
                "date": local_date,
                "workout_name": session_name,
                "work_sets": set_count,
                "minimum_weight": min_weight,
                "weight_unit": unit,
                "minimum_reps": min_reps,
                "minimum_rir": min_rir,
                "meets_rule": success,
            }
        )
    if not all(item["meets_rule"] for item in successes):
        raise ValueError(
            "Recent completed sets do not meet the conservative progression rule; hold the current load"
        )

    is_large_lower_body_lift = any(
        word in canonical_exercise.casefold() for word in ("squat", "deadlift", "leg press")
    )
    increment = (
        (5.0 if is_large_lower_body_lift else 2.5)
        if unit == "kg"
        else (10.0 if is_large_lower_body_lift else 5.0)
    )
    new_weight = old_weight + increment
    evidence = {
        "confidence": {"level": "high", "actionable": True, "reasons": []},
        "rule": (f"two latest completed {unit} sessions met all target sets/reps at >=2 RIR"),
        "biometrics_used": False,
        "sessions": successes,
    }
    change = {
        "plan_workout_id": workout_id,
        "plan_exercise_id": plan_exercise_id,
        "exercise": canonical_exercise,
        "old_starting_weight": old_weight,
        "new_starting_weight": new_weight,
        "weight_unit": unit,
    }
    for existing_id, existing_change_json in conn.execute(
        "SELECT id, change_json FROM coaching_proposals WHERE training_plan_id=? AND status='pending'",
        (plan_id,),
    ):
        if json.loads(existing_change_json).get("plan_exercise_id") == plan_exercise_id:
            return get_proposal(conn, existing_id)
    proposal_id = str(uuid.uuid4())
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO coaching_proposals "
        "(id, training_plan_id, kind, status, title, rationale, evidence_json, change_json, created_at) "
        "VALUES (?, ?, 'weight_progression', 'pending', ?, ?, ?, ?, ?)",
        (
            proposal_id,
            plan_id,
            f"Increase {canonical_exercise} from {old_weight:g} to {new_weight:g} {unit}",
            f"Two latest completed sessions met {sets_target} sets of at least {reps_max} reps at >=2 RIR.",
            json.dumps(evidence, sort_keys=True),
            json.dumps(change, sort_keys=True),
            now,
        ),
    )
    conn.commit()
    return get_proposal(conn, proposal_id)


def get_proposal(conn: sqlite3.Connection, proposal_id: str) -> dict:
    row = conn.execute(
        "SELECT training_plan_id, kind, status, title, rationale, evidence_json, change_json, created_at, resolved_at, resulting_plan_id "
        "FROM coaching_proposals WHERE id=?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Coaching proposal {proposal_id!r} was not found")
    (
        plan_id,
        kind,
        status,
        title,
        rationale,
        evidence,
        change,
        created_at,
        resolved_at,
        resulting_plan_id,
    ) = row
    return {
        "proposal_id": proposal_id,
        "training_plan_id": plan_id,
        "kind": kind,
        "status": status,
        "title": title,
        "rationale": rationale,
        "evidence": json.loads(evidence),
        "change": json.loads(change),
        "created_at": created_at,
        "resolved_at": resolved_at,
        "resulting_plan_id": resulting_plan_id,
        "confirmation_required": status == "pending",
    }


def _clone_plan_with_change(
    conn: sqlite3.Connection, plan_id: int, proposal_id: str, change: dict
) -> int:
    name, version, source, notes = conn.execute(
        "SELECT name, version, source, notes FROM training_plans WHERE id=?", (plan_id,)
    ).fetchone()
    next_version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM training_plans WHERE name=?", (name,)
    ).fetchone()[0]
    now = int(time.time() * 1000)
    cur = conn.execute(
        "INSERT INTO training_plans (name, version, status, source, notes, created_at) VALUES (?, ?, 'draft', ?, ?, ?)",
        (
            name,
            next_version,
            f"accepted_proposal:{proposal_id}",
            f"{notes or ''}\nAccepted proposal: {proposal_id}",
            now,
        ),
    )
    new_plan_id = cur.lastrowid
    if new_plan_id is None:
        raise RuntimeError("creating a plan version did not return a row id")
    old_workouts = conn.execute(
        "SELECT id, weekday, display_order, name, kind, intensity, estimated_minutes, optional, notes "
        "FROM plan_workouts WHERE training_plan_id=? ORDER BY display_order",
        (plan_id,),
    ).fetchall()
    for old_workout in old_workouts:
        old_id, *fields = old_workout
        cur = conn.execute(
            "INSERT INTO plan_workouts (training_plan_id, weekday, display_order, name, kind, intensity, estimated_minutes, optional, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_plan_id, *fields),
        )
        for exercise in conn.execute(
            "SELECT id, exercise_id, exercise_order, block_label, sets_target, reps_min, reps_max, tempo, rest_seconds, "
            "starting_weight, weight_unit, substitution_note, notes FROM plan_exercises WHERE plan_workout_id=? ORDER BY exercise_order",
            (old_id,),
        ):
            old_exercise_id, exercise_id, *exercise_fields = exercise
            fields_list = list(exercise_fields)
            if old_exercise_id == change["plan_exercise_id"]:
                fields_list[7] = change[
                    "new_starting_weight"
                ]  # starting_weight position after exercise_id
            conn.execute(
                "INSERT INTO plan_exercises (plan_workout_id, exercise_id, exercise_order, block_label, sets_target, reps_min, reps_max, "
                "tempo, rest_seconds, starting_weight, weight_unit, substitution_note, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cur.lastrowid, exercise_id, *fields_list),
            )
    return new_plan_id


def accept_proposal(conn: sqlite3.Connection, proposal_id: str) -> dict:
    proposal = get_proposal(conn, proposal_id)
    if proposal["status"] != "pending":
        raise ValueError(
            f"Proposal {proposal_id} is {proposal['status']}; only pending proposals can be accepted"
        )
    now = int(time.time() * 1000)
    try:
        conn.execute("BEGIN")
        new_plan_id = _clone_plan_with_change(
            conn, proposal["training_plan_id"], proposal_id, proposal["change"]
        )
        conn.execute("UPDATE training_plans SET status='archived' WHERE status='active'")
        conn.execute("UPDATE training_plans SET status='active' WHERE id=?", (new_plan_id,))
        updated = conn.execute(
            "UPDATE coaching_proposals SET status='accepted', resolved_at=?, resulting_plan_id=? WHERE id=? AND status='pending'",
            (now, new_plan_id, proposal_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("Proposal acceptance was superseded")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_proposal(conn, proposal_id)


def reject_proposal(conn: sqlite3.Connection, proposal_id: str) -> dict:
    now = int(time.time() * 1000)
    updated = conn.execute(
        "UPDATE coaching_proposals SET status='rejected', resolved_at=? WHERE id=? AND status='pending'",
        (now, proposal_id),
    )
    if updated.rowcount != 1:
        raise ValueError(f"Proposal {proposal_id} is not pending and cannot be rejected")
    conn.commit()
    return get_proposal(conn, proposal_id)
