import pytest

from whoop_local.coaching.service import (
    accept_proposal,
    propose_weight_progression,
    reject_proposal,
)
from whoop_local.db.schema import init_db
from whoop_local.history_import.strong import normalize_exercise_name
from whoop_local.mcp_server.queries import get_active_plan, get_coaching_proposals
from whoop_local.plans.synthetic_foundation import (
    WORKOUTS,
    seed_synthetic_foundation_plan,
)


def _conn(tmp_path):
    conn = init_db(tmp_path / "test.db")
    names = [exercise[0] for workout in WORKOUTS for exercise in workout["exercises"]]
    for i, name in enumerate(names, start=1):
        conn.execute(
            "INSERT INTO exercise_catalog (canonical_name, normalized_name, created_at) VALUES (?, ?, ?)",
            (name, normalize_exercise_name(name), i),
        )
    conn.commit()
    seed_synthetic_foundation_plan(conn)
    return conn


def _set_leg_press_load(conn, weight, unit):
    conn.execute(
        "UPDATE plan_exercises SET starting_weight=?, weight_unit=? WHERE exercise_id=("
        "SELECT id FROM exercise_catalog WHERE canonical_name='Leg Press')",
        (weight, unit),
    )
    conn.commit()


def _successful_leg_press_session(conn, session_number, date, weight=60, unit="kg"):
    leg_press_id = conn.execute(
        "SELECT id FROM exercise_catalog WHERE canonical_name='Leg Press'"
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO workout_sessions (started_at_utc, local_date, name, source, source_external_id, imported_at) "
        "VALUES (?, ?, 'Foundation B', 'manual', ?, 1)",
        (session_number * 1000, date, f"session-{session_number}"),
    )
    cur = conn.execute(
        "INSERT INTO workout_exercises (workout_session_id, exercise_id, exercise_order) VALUES (?, ?, 1)",
        (cur.lastrowid, leg_press_id),
    )
    for order in range(1, 4):
        conn.execute(
            "INSERT INTO workout_sets (workout_exercise_id, set_order, sequence_order, set_type, weight, weight_unit, reps, rir, source_row_hash) "
            "VALUES (?, ?, ?, 'work', ?, ?, 10, 2, ?)",
            (cur.lastrowid, order, order, weight, unit, f"{session_number}-{order}"),
        )
    conn.commit()


def test_progression_proposal_has_frozen_evidence_and_requires_acceptance(tmp_path):
    conn = _conn(tmp_path)
    _successful_leg_press_session(conn, 1, "2035-03-03")
    _successful_leg_press_session(conn, 2, "2035-03-10")

    proposal = propose_weight_progression(conn, "Foundation B", "Leg Press")
    assert proposal["status"] == "pending"
    assert proposal["confirmation_required"] is True
    assert proposal["change"]["old_starting_weight"] == 60
    assert proposal["change"]["new_starting_weight"] == 65
    assert proposal["change"]["weight_unit"] == "kg"
    assert proposal["evidence"]["confidence"]["actionable"] is True
    assert len(proposal["evidence"]["sessions"]) == 2
    duplicate = propose_weight_progression(conn, "Foundation B", "Leg Press")
    assert duplicate["proposal_id"] == proposal["proposal_id"]
    assert conn.execute("SELECT COUNT(*) FROM coaching_proposals").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM training_plans WHERE status='active'").fetchone()[0] == 1
    )

    accepted = accept_proposal(conn, proposal["proposal_id"])
    assert accepted["status"] == "accepted"
    assert accepted["resulting_plan_id"] is not None
    active = get_active_plan(conn)
    assert active["plan"]["version"] == 2
    leg_press = next(
        e
        for w in active["workouts"]
        if w["name"] == "Foundation B"
        for e in w["exercises"]
        if e["name"] == "Leg Press"
    )
    assert leg_press["starting_weight"] == 65
    assert leg_press["weight_unit"] == "kg"
    assert (
        conn.execute("SELECT COUNT(*) FROM training_plans WHERE status='active'").fetchone()[0] == 1
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(ValueError, match="only pending"):
        accept_proposal(conn, proposal["proposal_id"])


def test_weak_or_incomplete_history_cannot_generate_progression(tmp_path):
    conn = _conn(tmp_path)
    _successful_leg_press_session(conn, 1, "2035-03-03")
    with pytest.raises(ValueError, match="two completed"):
        propose_weight_progression(conn, "Foundation B", "Leg Press")
    assert conn.execute("SELECT COUNT(*) FROM coaching_proposals").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("unit", "old_weight", "expected_weight"),
    [("kg", 60, 65), ("lb", 100, 110)],
)
def test_progression_uses_the_plan_unit_without_conversion(
    tmp_path, unit, old_weight, expected_weight
):
    conn = _conn(tmp_path)
    _set_leg_press_load(conn, old_weight, unit)
    _successful_leg_press_session(conn, 1, "2035-03-03", old_weight, unit)
    _successful_leg_press_session(conn, 2, "2035-03-10", old_weight, unit)

    proposal = propose_weight_progression(conn, "Foundation B", "Leg Press")

    assert proposal["change"]["new_starting_weight"] == expected_weight
    assert proposal["change"]["weight_unit"] == unit
    assert all(session["weight_unit"] == unit for session in proposal["evidence"]["sessions"])


def test_rejection_does_not_change_active_plan_and_read_query_is_bounded(tmp_path):
    conn = _conn(tmp_path)
    _successful_leg_press_session(conn, 1, "2035-03-03")
    _successful_leg_press_session(conn, 2, "2035-03-10")
    proposal = propose_weight_progression(conn, "Foundation B", "Leg Press")
    assert get_coaching_proposals(conn)["count"] == 1
    assert reject_proposal(conn, proposal["proposal_id"])["status"] == "rejected"
    assert get_active_plan(conn)["plan"]["version"] == 1
    assert get_coaching_proposals(conn, "pending")["count"] == 0
    with pytest.raises(ValueError, match="status must"):
        get_coaching_proposals(conn, "invalid")
