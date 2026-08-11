import pytest

from whoop_local.db.schema import init_db
from whoop_local.history_import.strong import normalize_exercise_name
from whoop_local.manual_logging.service import (
    cancel_draft,
    confirm_draft,
    get_draft,
    prepare_draft,
)


def _conn(tmp_path):
    conn = init_db(tmp_path / "test.db")
    for name in ("Squat (Barbell)", "Bench Press (Barbell)"):
        conn.execute(
            "INSERT INTO exercise_catalog (canonical_name, normalized_name, created_at) VALUES (?, ?, 1)",
            (name, normalize_exercise_name(name)),
        )
    conn.commit()
    return conn


def _payload():
    return {
        "name": "Synthetic strength session",
        "started_at": "2035-03-10T17:30:00+00:00",
        "ended_at": "2035-03-10T18:40:00+00:00",
        "notes": "Synthetic fixture: hydration logged.",
        "exercises": [
            {
                "name": "Squat (Barbell)",
                "sets": [
                    {"weight": 40, "weight_unit": "kg", "reps": 8, "rir": 2},
                    {"weight": 40, "weight_unit": "kg", "reps": 8, "rir": 2},
                ],
            },
            {
                "name": "Bench Press (Barbell)",
                "notes": "Controlled tempo",
                "sets": [
                    {"weight": 60, "weight_unit": "lb", "reps": 10, "rpe": 7},
                ],
            },
        ],
    }


def test_prepare_draft_does_not_write_canonical_workout(tmp_path):
    conn = _conn(tmp_path)
    result = prepare_draft(conn, _payload())

    assert result["status"] == "pending"
    assert result["confirmation_required"] is True
    assert conn.execute("SELECT COUNT(*) FROM workout_sessions").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM manual_workout_drafts WHERE status='pending'"
        ).fetchone()[0]
        == 1
    )
    assert result["workout"]["local_date"] == "2035-03-10"


def test_confirm_materializes_once_with_explicit_provenance(tmp_path):
    conn = _conn(tmp_path)
    draft = prepare_draft(conn, _payload())
    result = confirm_draft(conn, draft["draft_id"])

    assert result["status"] == "confirmed"
    session = conn.execute(
        "SELECT source, source_external_id, name, local_date FROM workout_sessions"
    ).fetchone()
    assert session == (
        "manual",
        f"draft:{draft['draft_id']}",
        "Synthetic strength session",
        "2035-03-10",
    )
    assert conn.execute("SELECT COUNT(*) FROM workout_exercises").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM workout_sets").fetchone()[0] == 3
    assert {row[0] for row in conn.execute("SELECT DISTINCT weight_unit FROM workout_sets")} == {
        "kg",
        "lb",
    }
    assert get_draft(conn, draft["draft_id"])["workout_session_id"] == result["workout_session_id"]
    with pytest.raises(ValueError, match="only pending"):
        confirm_draft(conn, draft["draft_id"])
    assert conn.execute("SELECT COUNT(*) FROM workout_sessions").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "mutator, message",
    [
        (
            lambda p: p.update(
                {"exercises": [{"name": "No Such Exercise", "sets": [{"reps": 8}]}]}
            ),
            "Unknown canonical",
        ),
        (
            lambda p: p["exercises"][0]["sets"][0].update({"weight_unit": "stone"}),
            "weight_unit",
        ),
        (lambda p: p.update({"started_at": "2035-03-10T17:30:00"}), "timezone"),
    ],
)
def test_prepare_rejects_unsafe_or_ambiguous_payloads(tmp_path, mutator, message):
    conn = _conn(tmp_path)
    payload = _payload()
    mutator(payload)
    with pytest.raises(ValueError, match=message):
        prepare_draft(conn, payload)
    assert conn.execute("SELECT COUNT(*) FROM manual_workout_drafts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM workout_sessions").fetchone()[0] == 0


def test_cancelled_draft_cannot_be_confirmed(tmp_path):
    conn = _conn(tmp_path)
    draft = prepare_draft(conn, _payload())
    assert cancel_draft(conn, draft["draft_id"])["status"] == "cancelled"
    with pytest.raises(ValueError, match="only pending"):
        confirm_draft(conn, draft["draft_id"])
    assert conn.execute("SELECT COUNT(*) FROM workout_sessions").fetchone()[0] == 0
