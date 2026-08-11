from datetime import UTC as DATETIME_UTC
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from whoop_local.compute.compute_daily import _load_history, compute_for_date, recompute_all_dates
from whoop_local.compute.config import DEFAULT_CONFIG, MetricsConfig
from whoop_local.db.schema import init_db

UTC = ZoneInfo("UTC")


def _ts(day: date, hour: int, minute: int) -> int:
    dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=DATETIME_UTC)
    return int(dt.timestamp() * 1000)


def test_compute_for_date_with_hr_only_no_rr(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    day = date(2037, 7, 1)

    # A 4-hour low, stable overnight block -> should be detected as sleep.
    for hour in range(1, 5):
        for minute in range(0, 60, 5):
            conn.execute(
                "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) VALUES (?, ?, 'mac', 'whoop')",
                (_ts(day, hour, minute), 55),
            )
    conn.commit()

    row = compute_for_date(conn, day, tz=UTC)

    assert row["date"] == "2037-07-01"
    assert row["rhr_bpm"] is not None
    assert row["rmssd_ms"] is None  # no RR data yet
    assert row["hrv_baseline_ms"] is None  # no history yet
    assert row["recovery_proxy"] is None  # can't compute without HRV baseline
    assert row["sleep_minutes"] is not None and row["sleep_minutes"] >= 3 * 60

    # A sessions row should have been written for the detected sleep window.
    sessions = conn.execute("SELECT type FROM sessions").fetchall()
    assert any(s[0] == "sleep" for s in sessions)


def test_compute_for_date_with_rr_data_computes_hrv(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    day = date(2037, 7, 1)

    # >2 minutes of clean, plausible RR intervals (alternating 800/820ms).
    ts = _ts(day, 2, 0)
    for i in range(200):
        rr = 800 if i % 2 == 0 else 820
        conn.execute(
            "INSERT INTO rr_intervals (ts_utc, rr_ms, source_host, source) VALUES (?, ?, 'mac', 'whoop')",
            (ts + i * 800, rr),
        )
    conn.commit()

    row = compute_for_date(conn, day, tz=UTC)
    assert row["rmssd_ms"] is not None
    assert row["sdnn_ms"] is not None


def test_compute_for_date_does_not_duplicate_sleep_sessions_on_rerun(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    day = date(2037, 7, 1)

    for hour in range(1, 5):
        for minute in range(0, 60, 5):
            conn.execute(
                "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) VALUES (?, ?, 'mac', 'whoop')",
                (_ts(day, hour, minute), 55),
            )
    conn.commit()

    compute_for_date(conn, day, tz=UTC)
    first_count = conn.execute("SELECT COUNT(*) FROM sessions WHERE type = 'sleep'").fetchone()[0]
    assert first_count >= 1

    compute_for_date(conn, day, tz=UTC)  # re-run for the same date
    second_count = conn.execute("SELECT COUNT(*) FROM sessions WHERE type = 'sleep'").fetchone()[0]
    assert second_count == first_count  # must not duplicate


def test_compute_for_date_is_idempotent_by_date(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    day = date(2037, 7, 1)

    compute_for_date(conn, day, tz=UTC)
    compute_for_date(conn, day, tz=UTC)  # should update, not duplicate

    rows = conn.execute(
        "SELECT COUNT(*) FROM metrics_daily WHERE date = ?", (day.isoformat(),)
    ).fetchone()
    assert rows[0] == 1


def test_recovery_proxy_appears_once_history_exists(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)

    # Seed 7 days of prior metrics_daily history (minimum for a baseline).
    for i in range(7):
        d = date(2037, 6, 20 + i)
        conn.execute(
            "INSERT INTO metrics_daily (date, rmssd_ms, rhr_bpm) VALUES (?, ?, ?)",
            (d.isoformat(), 50.0, 55.0),
        )
    conn.commit()

    today = date(2037, 6, 27)
    for hour in range(1, 5):
        for minute in range(0, 60, 5):
            conn.execute(
                "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) VALUES (?, ?, 'mac', 'whoop')",
                (_ts(today, hour, minute), 55),
            )
    ts = _ts(today, 2, 0)
    for i in range(200):
        conn.execute(
            "INSERT INTO rr_intervals (ts_utc, rr_ms, source_host, source) VALUES (?, ?, 'mac', 'whoop')",
            (ts + i * 800, 800 if i % 2 == 0 else 820),
        )
    conn.commit()

    row = compute_for_date(conn, today, tz=UTC)
    assert row["hrv_baseline_ms"] == 50.0
    assert row["recovery_proxy"] is not None
    assert row["provisional"] == 0  # full 7-day history -> not provisional


def test_recovery_proxy_is_provisional_with_3_to_6_days_of_history(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)

    # Only 3 days of prior history -- below baseline_min_days (7) but at the
    # D26 provisional floor (3).
    for i in range(3):
        d = date(2037, 6, 24 + i)
        conn.execute(
            "INSERT INTO metrics_daily (date, rmssd_ms, rhr_bpm) VALUES (?, ?, ?)",
            (d.isoformat(), 50.0, 55.0),
        )
    conn.commit()

    today = date(2037, 6, 27)
    for hour in range(1, 5):
        for minute in range(0, 60, 5):
            conn.execute(
                "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) VALUES (?, ?, 'mac', 'whoop')",
                (_ts(today, hour, minute), 55),
            )
    ts = _ts(today, 2, 0)
    for i in range(200):
        conn.execute(
            "INSERT INTO rr_intervals (ts_utc, rr_ms, source_host, source) VALUES (?, ?, 'mac', 'whoop')",
            (ts + i * 800, 800 if i % 2 == 0 else 820),
        )
    conn.commit()

    row = compute_for_date(conn, today, tz=UTC)
    assert row["hrv_baseline_ms"] == 50.0  # a real value, not withheld
    assert row["recovery_proxy"] is not None
    assert row["provisional"] == 1


def test_compute_for_date_credits_overnight_sleep_to_wake_up_date(tmp_path):
    """D25: a night that runs 23:00 UTC on day D through 06:00 UTC on D+1 must
    land entirely in D+1's row (the wake-up date) under noon-to-noon
    attribution, not get split across D/D+1 the way UTC-midnight boundaries would."""
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    night_start_day = date(2037, 7, 1)  # sleep begins late on 2037-07-01 UTC...
    wake_up_day = date(2037, 7, 2)  # ...and is credited to 2037-07-02

    dt = datetime(2037, 7, 1, 23, 0, tzinfo=DATETIME_UTC)
    ts0 = int(dt.timestamp() * 1000)
    for i in range(7 * 60 // 5):  # 7 hours, every 5 min -> low+stable block
        conn.execute(
            "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) VALUES (?, ?, 'mac', 'whoop')",
            (ts0 + i * 5 * 60 * 1000, 55),
        )
    conn.commit()

    before_row = compute_for_date(conn, night_start_day, tz=UTC)
    assert before_row["sleep_minutes"] is None  # nothing in the 07-01 noon-to-noon window yet

    after_row = compute_for_date(conn, wake_up_day, tz=UTC)
    assert after_row["sleep_minutes"] is not None and after_row["sleep_minutes"] >= 3 * 60


def test_compute_for_date_records_timezone_used(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    day = date(2037, 7, 1)

    compute_for_date(conn, day, tz=UTC)
    stored = conn.execute(
        "SELECT timezone FROM metrics_daily WHERE date = ?", (day.isoformat(),)
    ).fetchone()[0]
    assert stored == "UTC"


def test_recompute_all_dates_drops_stale_rows_outside_raw_data_range(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)

    # A stale row left over from before D25 (e.g. a pre-migration UTC-keyed date
    # with no corresponding raw data at all).
    conn.execute("INSERT INTO metrics_daily (date, rhr_bpm) VALUES ('2035-01-01', 60.0)")
    for hour in range(1, 5):
        for minute in range(0, 60, 5):
            conn.execute(
                "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) VALUES (?, ?, 'mac', 'whoop')",
                (_ts(date(2037, 7, 1), hour, minute), 55),
            )
    conn.commit()

    rows = recompute_all_dates(conn, tz=UTC)
    assert any(r["date"] == "2037-07-01" for r in rows)

    remaining = {r[0] for r in conn.execute("SELECT date FROM metrics_daily").fetchall()}
    assert "2035-01-01" not in remaining


def test_compute_for_date_coverage_pct_full_with_no_gaps(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    row = compute_for_date(conn, date(2037, 7, 1), tz=UTC)
    assert row["coverage_pct"] == 1.0


def test_compute_for_date_coverage_pct_reflects_a_real_gap(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    day = date(2037, 7, 1)
    # Noon-to-noon UTC window for 2037-07-01 is [2037-06-30T12:00Z, 2037-07-01T12:00Z),
    # a 24h = 86,400,000ms window. A 1-hour gap inside it is 1/24 of the window.
    gap_start = _ts(date(2037, 6, 30), 18, 0)
    gap_end = _ts(date(2037, 6, 30), 19, 0)
    conn.execute(
        "INSERT INTO capture_gaps (source_host, gap_start_utc, gap_end_utc, reason) VALUES ('mac', ?, ?, 'disconnected')",
        (gap_start, gap_end),
    )
    conn.commit()

    row = compute_for_date(conn, day, tz=UTC)
    assert row["coverage_pct"] == pytest.approx(23 / 24, abs=1e-6)


def test_compute_for_date_history_recovery_fills_recorded_gap(tmp_path):
    conn = init_db(tmp_path / "test.db")
    day = date(2037, 7, 1)
    gap_start = _ts(date(2037, 6, 30), 18, 0)
    gap_end = gap_start + 10_000
    conn.execute(
        "INSERT INTO capture_gaps (source_host, gap_start_utc, gap_end_utc, reason) "
        "VALUES ('mac', ?, ?, 'disconnected')",
        (gap_start, gap_end),
    )
    for offset in range(0, 10_000, 1000):
        ts = gap_start + offset
        conn.execute(
            "INSERT INTO hr_samples "
            "(ts_utc, device_ts_utc, hr_bpm, source_host, source) "
            "VALUES (?, ?, 70, 'mac', 'whoop')",
            (ts, ts),
        )
    conn.commit()

    row = compute_for_date(conn, day, tz=UTC)
    assert row["coverage_pct"] == 1.0


def test_compute_for_date_computes_strain_from_todays_rhr(tmp_path):
    import json

    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    day = date(2037, 7, 1)

    # A low, stable overnight block for RHR (sleep detection also needs this).
    for hour in range(1, 5):
        for minute in range(0, 60, 5):
            conn.execute(
                "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) VALUES (?, ?, 'mac', 'whoop')",
                (_ts(day, hour, minute), 55),
            )
    # A hard effort block later the same morning (still within the noon-to-noon
    # UTC window for `day`, which closes at 12:00 UTC) to produce real strain.
    for minute in range(0, 30):
        conn.execute(
            "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) VALUES (?, ?, 'mac', 'whoop')",
            (_ts(day, 8, minute), 150),
        )
    conn.commit()

    unconfigured = compute_for_date(conn, day, tz=UTC)
    assert unconfigured["strain_proxy"] is None
    assert unconfigured["time_in_zones_json"] is None

    config = MetricsConfig(strain_hrmax_intercept=185, strain_hrmax_age_coefficient=0)
    row = compute_for_date(conn, day, config=config, tz=UTC)
    assert row["strain_proxy"] is not None
    assert row["strain_proxy"] > 0
    zones = json.loads(row["time_in_zones_json"])
    assert sum(zones.values()) > 0


def test_compute_for_date_strain_is_none_without_any_rhr(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    row = compute_for_date(conn, date(2037, 7, 1), tz=UTC)
    assert row["strain_proxy"] is None
    assert row["time_in_zones_json"] is None


def test_rhr_history_falls_back_to_imported_data_for_missing_dates(tmp_path):
    """D33: imported_daily_metrics seeds the RHR leg of the rolling baseline
    on dates that have no real WHOOP-derived metrics_daily row."""
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)

    for i, rhr in enumerate([54.0, 55.0, 56.0]):
        d = date(2037, 6, 20 + i)
        conn.execute(
            "INSERT INTO imported_daily_metrics (date, rhr_bpm, source, imported_at) VALUES (?, ?, 'apple_health_export', 0)",
            (d.isoformat(), rhr),
        )
    conn.commit()

    history = _load_history(conn, "rhr_bpm", "2037-07-01", DEFAULT_CONFIG)
    assert history == [54.0, 55.0, 56.0]


def test_rhr_baseline_prefers_real_metrics_daily_over_imported_for_same_date(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)

    d = date(2037, 6, 20)
    conn.execute("INSERT INTO metrics_daily (date, rhr_bpm) VALUES (?, 999.0)", (d.isoformat(),))
    conn.execute(
        "INSERT INTO imported_daily_metrics (date, rhr_bpm, source, imported_at) VALUES (?, 50.0, 'apple_health_export', 0)",
        (d.isoformat(),),
    )
    conn.commit()

    history = _load_history(conn, "rhr_bpm", "2037-06-21", DEFAULT_CONFIG)
    assert history == [999.0]  # real row wins, imported duplicate excluded
