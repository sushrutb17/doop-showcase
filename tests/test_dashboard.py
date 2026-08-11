import time
from datetime import date
from datetime import time as daytime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from whoop_local.dashboard.workout_entry import plan_log_rows, rows_to_payload
from whoop_local.db.schema import init_db
from whoop_local.mcp_server import queries


def test_gap_duration_calc_handles_open_and_closed_gaps(tmp_path):
    """Regression test: an earlier version crashed on tz-naive vs tz-aware
    datetime subtraction when an open gap (no end) was mixed with the
    fillna'd 'now' timestamp. Exercises the same logic dashboard/app.py uses."""
    conn = init_db(tmp_path / "test.db")
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO capture_gaps (source_host, gap_start_utc, gap_end_utc, reason) "
        "VALUES ('mac', ?, ?, 'disconnected')",
        (now_ms - 120_000, now_ms - 60_000),  # closed gap, 60s ago -> 1 min ago
    )
    conn.execute(
        "INSERT INTO capture_gaps (source_host, gap_start_utc, gap_end_utc, reason) "
        "VALUES ('mac', ?, NULL, 'disconnected')",
        (now_ms - 30_000,),  # open gap, started 30s ago, still ongoing
    )
    conn.commit()

    status = queries.get_status(conn, "test.db")
    gaps = status["coverage_gaps_24h"]
    assert len(gaps) == 2

    gap_df = pd.DataFrame(gaps)
    gap_df["start_dt"] = pd.to_datetime(gap_df["start"], utc=True)
    now_utc = pd.Timestamp.now(tz="UTC")
    gap_df["end_dt"] = pd.to_datetime(gap_df["end"], utc=True).fillna(now_utc)
    gap_df["duration_min"] = (gap_df["end_dt"] - gap_df["start_dt"]).dt.total_seconds() / 60.0

    assert gap_df["duration_min"].iloc[0] == 1.0  # closed gap: exactly 60s
    assert gap_df["duration_min"].iloc[1] > 0  # open gap: some positive duration to "now"


def test_rr_histogram_calc_does_not_crash_on_real_shaped_data():
    rr_values = np.array([700.0, 750.0, 800.0, 820.0, 1500.0])
    counts, bin_edges = np.histogram(rr_values, bins=30)
    assert counts.sum() == len(rr_values)
    hist_df = pd.DataFrame({"rr_ms": bin_edges[:-1].round(0), "count": counts}).set_index("rr_ms")
    assert not hist_df.empty


def test_plan_rows_become_timezone_aware_review_payload():
    active_plan = {
        "workouts": [
            {
                "name": "Lower strength",
                "exercises": [
                    {
                        "name": "Squat (Barbell)",
                        "sets_target": 2,
                        "starting_weight": 60,
                        "weight_unit": "kg",
                        "reps": {"min": 6, "max": 8},
                    },
                    {
                        "name": "Push Up",
                        "sets_target": 1,
                        "starting_weight": None,
                        "weight_unit": "bodyweight",
                        "reps": {"min": 8, "max": 12},
                    },
                ],
            }
        ]
    }
    rows = plan_log_rows(active_plan, "Lower strength")
    assert len(rows) == 3
    assert rows[0]["Unit"] == "kg"
    assert rows[-1]["Unit"] == "bodyweight"
    rows[0]["Reps"] = 8
    rows[1]["Include"] = False

    payload = rows_to_payload(
        rows,
        "Lower strength",
        date(2037, 7, 11),
        daytime(17, 30),
        70,
        ZoneInfo("America/New_York"),
        "Synthetic fixture session.",
    )
    assert payload["started_at"] == "2037-07-11T17:30:00-04:00"
    assert payload["ended_at"] == "2037-07-11T18:40:00-04:00"
    assert payload["exercises"][0]["sets"] == [
        {"reps": 8.0, "rir": 2.0, "rpe": None, "weight": 60.0, "weight_unit": "kg"}
    ]
    assert payload["exercises"][1]["sets"] == [{"reps": 8.0, "rir": 2.0, "rpe": None}]


def test_workout_entry_requires_at_least_one_completed_set():
    rows = [{"Include": False, "Exercise": "Squat (Barbell)", "Set": 1}]
    import pytest

    with pytest.raises(ValueError, match="at least one"):
        rows_to_payload(
            rows,
            "Lower strength",
            date(2037, 7, 11),
            daytime(17, 30),
            70,
            ZoneInfo("America/New_York"),
        )
