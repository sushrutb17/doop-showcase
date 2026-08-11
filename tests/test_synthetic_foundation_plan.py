from whoop_local.db.schema import init_db
from whoop_local.history_import.strong import normalize_exercise_name
from whoop_local.mcp_server.queries import get_active_plan
from whoop_local.plans.synthetic_foundation import (
    WORKOUTS,
    seed_synthetic_foundation_plan,
)


def _conn_with_catalog(tmp_path):
    conn = init_db(tmp_path / "test.db")
    names = [exercise[0] for workout in WORKOUTS for exercise in workout["exercises"]]
    for index, name in enumerate(names, start=1):
        conn.execute(
            "INSERT INTO exercise_catalog (canonical_name, normalized_name, created_at) "
            "VALUES (?, ?, ?)",
            (name, normalize_exercise_name(name), index),
        )
    conn.commit()
    return conn


def test_synthetic_foundation_uses_a_generic_weekday_schedule(tmp_path):
    conn = _conn_with_catalog(tmp_path)
    counts = seed_synthetic_foundation_plan(conn)

    assert counts == {"plan_id": 1, "workouts": 4, "exercises": 8}
    result = get_active_plan(conn)
    assert result["plan"]["name"] == "Synthetic Foundation"
    assert result["plan"]["version"] == 1
    assert [(w["day"], w["kind"], w["intensity"], w["optional"]) for w in result["workouts"]] == [
        ("Monday", "strength", "moderate", False),
        ("Wednesday", "sport", "easy", False),
        ("Friday", "strength", "moderate", False),
        ("Sunday", "recovery", "easy", True),
    ]
    assert result["workouts"][0]["exercises"][0]["name"] == "Goblet Squat (Dumbbell)"
    assert result["workouts"][2]["exercises"][0]["name"] == "Leg Press"


def test_synthetic_foundation_is_idempotent_and_unit_explicit(tmp_path):
    conn = _conn_with_catalog(tmp_path)
    first = seed_synthetic_foundation_plan(conn)
    second = seed_synthetic_foundation_plan(conn)

    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM training_plans").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM plan_workouts").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM plan_exercises").fetchone()[0] == 8
    units = {row[0] for row in conn.execute("SELECT DISTINCT weight_unit FROM plan_exercises")}
    assert units == {"bodyweight", "kg"}
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_active_plan_is_explicit_when_not_seeded(tmp_path):
    result = get_active_plan(init_db(tmp_path / "test.db"))
    assert result["status"] == "no_data"
    assert result["workouts"] == []
