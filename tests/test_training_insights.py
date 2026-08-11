from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import whoop_local.mcp_server.queries as query_module
from whoop_local.db.schema import init_db
from whoop_local.history_import.strong import normalize_exercise_name
from whoop_local.mcp_server.queries import (
    get_plan_adherence,
    get_telegram_training_guidance,
    get_telegram_weekly_review,
    get_training_guidance,
    get_weekly_training_review,
)
from whoop_local.plans.synthetic_foundation import (
    WORKOUTS,
    seed_synthetic_foundation_plan,
)


def _conn(tmp_path):
    conn = init_db(tmp_path / "test.db")
    names = [exercise[0] for workout in WORKOUTS for exercise in workout["exercises"]]
    for index, name in enumerate(names, start=1):
        conn.execute(
            "INSERT INTO exercise_catalog (canonical_name, normalized_name, created_at) VALUES (?, ?, ?)",
            (name, normalize_exercise_name(name), index),
        )
    conn.commit()
    seed_synthetic_foundation_plan(conn)
    conn.execute("UPDATE training_plans SET starts_on='2037-07-06' WHERE status='active'")
    conn.commit()
    return conn


def _log_strength(conn, local_date, name):
    conn.execute(
        "INSERT INTO workout_sessions (started_at_utc, local_date, name, source, source_external_id, imported_at) "
        "VALUES (1, ?, ?, 'manual', ?, 1)",
        (local_date, name, f"{local_date}:{name}"),
    )
    conn.commit()


def _log_aerobic_base(conn, local_date):
    timestamp = int(local_date.replace("-", ""))
    conn.execute(
        "INSERT INTO imported_workouts (date, start_utc, end_utc, activity, duration_min, source_name, source, imported_at) "
        "VALUES (?, ?, ?, 'Aerobic base', 35, 'Example Watch', 'apple_health', 1)",
        (local_date, timestamp, timestamp + 1),
    )
    conn.commit()


def _recovery(conn, local_date, score=55, coverage=0.9, sleep=420, provisional=0):
    conn.execute(
        "INSERT INTO metrics_daily (date, recovery_proxy, rmssd_ms, rhr_bpm, sleep_minutes, provisional, coverage_pct) "
        "VALUES (?, ?, 55, 58, ?, ?, ?)",
        (local_date, score, sleep, provisional, coverage),
    )
    conn.commit()


def test_adherence_matches_sport_and_detailed_strength_without_scoring_optional_recovery(tmp_path):
    conn = _conn(tmp_path)
    _log_strength(conn, "2037-07-06", "Foundation A")
    _log_aerobic_base(conn, "2037-07-08")

    result = get_plan_adherence(conn, "2037-07-06", "2037-07-12")
    assert result["required_slots"] == 3
    assert result["completed_required_slots"] == 2
    assert result["adherence_pct"] == 66.7
    assert result["missed_required"][0]["name"] == "Foundation B"
    recovery_slot = next(slot for slot in result["slots"] if slot["kind"] == "recovery")
    assert recovery_slot["optional"] is True


def test_guidance_withholds_adjustment_when_recovery_is_not_actionable(tmp_path):
    conn = _conn(tmp_path)
    _recovery(conn, "2037-07-10", score=80, coverage=0.4)
    result = get_training_guidance(conn, "2037-07-10")
    assert result["planned"][0]["name"] == "Foundation B"
    assert result["recovery"]["value"] is None
    assert result["recommendation"]["action"] == "follow_plan_no_biometric_adjustment"
    assert result["recommendation"]["plan_mutated"] is False


def test_guidance_reduces_strength_only_with_actionable_low_recovery(tmp_path):
    conn = _conn(tmp_path)
    _recovery(conn, "2037-07-10", score=32)
    result = get_training_guidance(conn, "2037-07-10")
    assert result["recovery"]["confidence"]["actionable"] is True
    assert result["recommendation"]["action"] == "reduce_session"
    assert result["recommendation"]["sets_multiplier"] == 0.75


def test_guidance_includes_exact_prescriptions_and_planned_not_logged_contract(tmp_path):
    conn = _conn(tmp_path)
    _recovery(conn, "2037-07-10", score=55)
    result = get_training_guidance(conn, "2037-07-10")
    assert result["planned"][0]["name"] == "Foundation B"
    assert result["planned"][0]["exercises"]
    assert result["planned"][0]["exercises"][0]["weight_unit"] == "kg"
    assert result["presentation"]["session_state"] == "planned_not_logged"
    assert result["presentation"]["session_phrase"] == "Planned; not yet logged as completed."
    lines = result["presentation"]["required_telegram_lines"]
    assert lines[0] == "Planned; not yet logged as completed."
    assert "2–3 reps in reserve" in lines[2]
    assert "Leg Press: 3 × 8–10 @ 60 kg" in lines[3]
    assert lines[-2].startswith("Recovery confidence: medium (actionable).")
    assert "acute injury overrides" in lines[-1]
    relay = get_telegram_training_guidance(conn, "2037-07-10")
    assert relay["text"] == "Doop training guidance:\n" + "\n".join(lines)
    assert set(relay) == {"date", "text", "relay_contract"}


def test_guidance_on_unscheduled_day_recommends_rest_without_mutation(tmp_path):
    result = get_training_guidance(_conn(tmp_path), "2037-07-07")
    assert result["planned"] == []
    assert result["recommendation"]["action"] == "rest_or_easy_recovery"
    assert result["recommendation"]["plan_mutated"] is False


def test_weekly_review_uses_only_actionable_recovery_and_flags_unlogged_slots(tmp_path):
    conn = _conn(tmp_path)
    _log_aerobic_base(conn, "2037-07-08")
    _recovery(conn, "2037-07-08", score=60)
    _recovery(conn, "2037-07-09", score=90, coverage=0.2)
    result = get_weekly_training_review(conn, "2037-07-06", as_of_date=date(2037, 7, 12))
    assert result["actionable_recovery_days"] == 1
    assert result["average_actionable_recovery"] == 60
    assert result["adherence"]["completed_required_slots"] == 1
    assert "lack a matching completion record" in result["summary"]
    lines = result["presentation"]["required_telegram_lines"]
    assert lines[0].startswith("Week: 2037-07-06 through")
    assert any("required; no matching completion record" in line for line in lines)
    assert any("optional; not scored" in line for line in lines)
    assert lines[-1].startswith("Only D39-actionable recovery")
    relay = get_telegram_weekly_review(conn, "2037-07-06", as_of_date=date(2037, 7, 12))
    assert relay["text"] == "Doop weekly review:\n" + "\n".join(lines)
    assert set(relay) == {"week_start", "text", "relay_contract"}


def test_weekly_review_requires_monday(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="Monday"):
        get_weekly_training_review(_conn(tmp_path), "2037-07-07")


def test_weekly_review_accepts_explicit_as_of_date_for_fixed_future_fixture(tmp_path):
    conn = _conn(tmp_path)
    result = get_weekly_training_review(
        conn,
        "2037-07-06",
        as_of_date=date(2037, 7, 6),
    )

    assert result["week"]["through"] == "2037-07-06"
    relay = get_telegram_weekly_review(
        conn,
        "2037-07-06",
        as_of_date=date(2037, 7, 6),
    )
    assert "Week: 2037-07-06 through 2037-07-06." in relay["text"]


def test_weekly_review_defaults_to_runtime_local_date(tmp_path, monkeypatch):
    monkeypatch.setattr(query_module, "_local_today", lambda: date(2037, 7, 6))

    result = get_weekly_training_review(_conn(tmp_path), "2037-07-06")

    assert result["week"]["through"] == "2037-07-06"


def test_weekly_default_clock_uses_configured_timezone_not_host_timezone(monkeypatch):
    monkeypatch.setattr(query_module, "local_timezone", lambda: ZoneInfo("Pacific/Kiritimati"))
    instant = datetime(2037, 7, 5, 12, 30, tzinfo=UTC)

    assert query_module._local_today(instant) == date(2037, 7, 6)
