from datetime import UTC, date, datetime

from whoop_local.dashboard.training_os import (
    build_decision,
    build_week_rows,
    rep_range,
    system_freshness,
)


def test_decision_preserves_planned_not_logged_and_actionability():
    result = build_decision(
        {
            "planned": [
                {
                    "name": "Lower strength",
                    "kind": "strength",
                    "intensity": "heavy",
                    "estimated_minutes": 75,
                    "completed": False,
                }
            ],
            "recovery": {"value": 40.1, "confidence": {"level": "high", "actionable": True}},
            "recommendation": {
                "action": "perform_as_planned",
                "message": "Perform the session as written.",
            },
        }
    )
    assert result["headline"] == "DO THE PLANNED LOWER STRENGTH SESSION"
    assert result["status"] == "Planned · Not yet logged"
    assert result["recovery"] == "40"
    assert result["confidence"] == "High / Actionable"
    assert result["evidence"][0] == "Perform the session as written."


def test_decision_never_exposes_non_actionable_recovery_value():
    result = build_decision(
        {
            "planned": [],
            "recovery": {"value": None, "confidence": {"level": "low", "actionable": False}},
            "recommendation": {"action": "rest_or_easy_recovery", "message": "Rest."},
        }
    )
    assert result["recovery"] == "Withheld"
    assert result["confidence"] == "Low / Not actionable"


def test_week_rows_distinguish_missing_log_from_missed_workout():
    active = {
        "workouts": [
            {
                "weekday": 1,
                "name": "Conditioning",
                "intensity": "hard",
                "estimated_minutes": 90,
                "optional": False,
            },
            {
                "weekday": 2,
                "name": "Recovery",
                "intensity": "easy",
                "estimated_minutes": 30,
                "optional": True,
            },
            {
                "weekday": 5,
                "name": "Lower strength",
                "intensity": "heavy",
                "estimated_minutes": 75,
                "optional": False,
            },
        ]
    }
    weekly = {
        "adherence": {
            "slots": [
                {"date": "2037-07-14", "completed": False},
                {"date": "2037-07-15", "completed": False},
                {"date": "2037-07-18", "completed": False},
            ]
        }
    }
    rows = build_week_rows(active, weekly, date(2037, 7, 18))
    assert rows[1]["state"] == "No matching log"
    assert rows[2]["state"] == "Optional"
    assert rows[5]["state"] == "Today"
    assert rows[6]["state"] == "Rest"


def test_system_freshness_reports_current_and_stale_without_overclaiming():
    now = datetime(2037, 7, 18, 20, 0, tzinfo=UTC)
    current = system_freshness(
        {"last_reading_utc": "2037-07-18T19:57:00Z", "battery_pct": 62, "battery_low": False}, now
    )
    stale = system_freshness(
        {"last_reading_utc": "2037-07-18T19:00:00Z", "battery_pct": 62, "battery_low": False}, now
    )
    assert current == {"label": "Data current · Battery 62%", "tone": "ok"}
    assert stale == {"label": "Data sync is stale", "tone": "attention"}


def test_rep_range_preserves_the_plan_targets():
    assert rep_range({"sets_target": 4, "reps": {"min": 6, "max": 8}}) == "4 × 6–8"
    assert rep_range({"sets_target": 1, "reps": {"min": 1, "max": 1}}) == "1 × 1"
