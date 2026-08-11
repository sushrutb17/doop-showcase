import pytest

from whoop_local.db.schema import init_db
from whoop_local.history_import.strong import (
    import_into_db,
    normalize_exercise_name,
    parse_duration_seconds,
    read_sessions,
)

CSV = """Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,Distance,Seconds,Notes,Workout Notes,RPE
2042-08-11 07:15:00,Synthetic Session Alpha,52min,Box Step Up,1,17.5,11,0,0,,Invented import fixture,5
2042-08-11 07:15:00,Synthetic Session Alpha,52min,Box Step Up,2,19,9,0,0,,,7
2042-08-11 07:15:00,Synthetic Session Alpha,52min,Box Step Up,Rest Timer,0,0,0,95,,,
2042-08-11 07:15:00,Synthetic Session Alpha,52min,Resistance Band Pull Apart,1,0,16,0,0,,,
2042-08-14 09:45:00,Synthetic Session Beta,33min,Rowing,1,0,0,6.25,1980,Designed interval row,,4
"""


def _csv(tmp_path):
    path = tmp_path / "strong.csv"
    path.write_text(CSV)
    return path


def test_helpers(tmp_path):
    assert parse_duration_seconds("2h 3min") == 7380
    assert parse_duration_seconds("33min") == 1980
    assert normalize_exercise_name("  Box   STEP UP ") == "box step up"
    sessions = read_sessions(str(_csv(tmp_path)))
    assert len(sessions) == 2
    assert len(sessions[0].rows) == 4


def test_import_writes_normalized_logbook(tmp_path):
    conn = init_db(tmp_path / "test.db")
    result = import_into_db(conn, str(_csv(tmp_path)), weight_unit="kg")

    assert result == {
        "sessions": 2,
        "exercise_blocks": 3,
        "sets": 5,
        "rest_events": 1,
        "new_exercises": 3,
    }
    assert conn.execute("SELECT COUNT(*) FROM workout_sessions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM exercise_catalog").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM workout_exercises").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM workout_sets").fetchone()[0] == 5
    assert conn.execute("SELECT weight_unit FROM workout_sets LIMIT 1").fetchone()[0] == "kg"
    assert (
        conn.execute(
            "SELECT notes FROM workout_sessions WHERE name='Synthetic Session Alpha'"
        ).fetchone()[0]
        == "Invented import fixture"
    )
    assert (
        conn.execute("SELECT seconds FROM workout_sets WHERE set_type='rest'").fetchone()[0] == 95
    )


def test_import_is_idempotent(tmp_path):
    conn = init_db(tmp_path / "test.db")
    first = import_into_db(conn, str(_csv(tmp_path)), weight_unit="lb")
    second = import_into_db(conn, str(_csv(tmp_path)), weight_unit="lb")

    assert first["sets"] == second["sets"] == 5
    assert second["new_exercises"] == 0
    assert conn.execute("SELECT COUNT(*) FROM workout_sessions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM workout_sets").fetchone()[0] == 5
    assert {row[0] for row in conn.execute("SELECT weight_unit FROM workout_sets")} == {"lb"}


def test_import_requires_a_supported_explicit_weight_unit(tmp_path):
    conn = init_db(tmp_path / "test.db")

    with pytest.raises(ValueError, match="weight_unit"):
        import_into_db(conn, str(_csv(tmp_path)), weight_unit="stone")
