from whoop_local.db.schema import init_db
from whoop_local.history_import.apple_health import (
    _attributed_date,
    _parse_dt,
    import_into_db,
    parse_daily_values,
    parse_export,
)

SAMPLE_EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
<ExportDate value="2035-02-10 20:00:00 +0000"/>
<Record type="HKQuantityTypeIdentifierRestingHeartRate" unit="count/min" value="57" sourceName="Synthetic Watch" startDate="2035-02-01 08:00:00 +0000" endDate="2035-02-01 08:00:00 +0000"/>
<Record type="HKQuantityTypeIdentifierRestingHeartRate" unit="count/min" value="63" sourceName="Synthetic Watch" startDate="2035-02-02 08:00:00 +0000" endDate="2035-02-02 08:00:00 +0000"/>
<Record type="HKQuantityTypeIdentifierRestingHeartRate" unit="count/min" value="67" sourceName="Synthetic Watch" startDate="2035-02-02 20:00:00 +0000" endDate="2035-02-02 20:00:00 +0000"/>
<Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" unit="ms" value="41.5" sourceName="Synthetic Watch" startDate="2035-02-01 08:00:00 +0000" endDate="2035-02-01 08:00:00 +0000"/>
<Record type="HKQuantityTypeIdentifierHeartRate" unit="count/min" value="81" sourceName="Synthetic Watch" startDate="2035-02-01 09:00:00 +0000" endDate="2035-02-01 09:00:00 +0000"/>
<Record type="HKQuantityTypeIdentifierHeartRate" unit="count/min" value="151" sourceName="Synthetic Watch" startDate="2035-02-05 18:30:00 +0000" endDate="2035-02-05 18:30:00 +0000"/>
<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Synthetic Watch" startDate="2035-02-01 23:30:00 +0000" endDate="2035-02-02 03:00:00 +0000" value="HKCategoryValueSleepAnalysisAsleepCore"/>
<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Synthetic Watch" startDate="2035-02-02 03:00:00 +0000" endDate="2035-02-02 04:00:00 +0000" value="HKCategoryValueSleepAnalysisAsleepDeep"/>
<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Synthetic Phone" startDate="2035-02-01 23:00:00 +0000" endDate="2035-02-02 07:00:00 +0000" value="HKCategoryValueSleepAnalysisInBed"/>
<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Synthetic Watch" startDate="2035-02-02 04:00:00 +0000" endDate="2035-02-02 04:10:00 +0000" value="HKCategoryValueSleepAnalysisAwake"/>
<Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="35.0" durationUnit="min" sourceName="Synthetic Watch" startDate="2035-02-02 18:00:00 +0000" endDate="2035-02-02 18:35:00 +0000"/>
</HealthData>
"""


def _write_sample(tmp_path):
    path = tmp_path / "export.xml"
    path.write_text(SAMPLE_EXPORT_XML)
    return str(path)


def test_parse_daily_values_extracts_resting_hr_and_hrv_sdnn(tmp_path):
    export_path = _write_sample(tmp_path)
    by_date = parse_daily_values(export_path)

    from datetime import date

    assert by_date[date(2035, 2, 1)]["rhr_bpm"] == [57.0]
    assert by_date[date(2035, 2, 1)]["hrv_sdnn_ms"] == [41.5]
    # Two RestingHeartRate records land on the same local date -- median-able.
    assert by_date[date(2035, 2, 2)]["rhr_bpm"] == [63.0, 67.0]


def test_parse_daily_values_ignores_unrelated_record_types(tmp_path):
    export_path = _write_sample(tmp_path)
    by_date = parse_daily_values(export_path)
    total_records = sum(len(v["rhr_bpm"]) + len(v["hrv_sdnn_ms"]) for v in by_date.values())
    assert total_records == 4  # 3 RestingHeartRate + 1 HRV SDNN, not the plain HeartRate records


def test_attributed_date_credits_sleep_to_wake_date():
    # Overnight sleep ending before local noon -> that calendar date (D25).
    assert _attributed_date(_parse_dt("2035-02-02 03:00:00 +0000")) == "2035-02-02"
    # An afternoon end falls in the NEXT date's noon-to-noon window.
    assert _attributed_date(_parse_dt("2035-02-02 18:35:00 +0000")) == "2035-02-03"


def test_parse_export_collects_sleep_workouts_and_bounded_hr(tmp_path):
    export_path = _write_sample(tmp_path)
    parsed = parse_export(export_path, hr_since_date_iso="2035-02-03")

    stages = sorted(seg[3] for seg in parsed.sleep_segments)
    assert stages == ["asleep_core", "asleep_deep", "awake", "inbed"]
    # All the overnight segments end before noon on 04-02 -> wake date 04-02.
    assert {seg[0] for seg in parsed.sleep_segments} == {"2035-02-02"}

    assert len(parsed.workouts) == 1
    date_str, _start, _end, activity, duration_min, source_name = parsed.workouts[0]
    assert activity == "Running"
    assert duration_min == 35.0
    assert source_name == "Synthetic Watch"
    assert date_str == "2035-02-03"  # ends after noon -> next noon-to-noon window

    # hr_since cutoff: only the 04-05 sample, not the 04-01 one.
    assert len(parsed.hr_samples) == 1
    assert parsed.hr_samples[0][1] == 151.0


def test_parse_export_skips_hr_entirely_when_no_cutoff(tmp_path):
    export_path = _write_sample(tmp_path)
    parsed = parse_export(export_path, hr_since_date_iso=None)
    assert parsed.hr_samples == []


def test_import_into_db_writes_median_per_day(tmp_path):
    conn = init_db(tmp_path / "test.db")
    export_path = _write_sample(tmp_path)

    counts = import_into_db(conn, export_path)
    assert counts["daily_metrics"] == 2  # two distinct local dates

    row = conn.execute(
        "SELECT rhr_bpm, hrv_sdnn_ms, source FROM imported_daily_metrics WHERE date = '2035-02-02'"
    ).fetchone()
    assert row[0] == 65.0  # median(63, 67)
    assert row[1] is None  # no HRV SDNN record that day
    assert row[2] == "apple_health_export"


def test_import_into_db_writes_sleep_workouts_hr(tmp_path):
    conn = init_db(tmp_path / "test.db")
    export_path = _write_sample(tmp_path)

    counts = import_into_db(conn, export_path, hr_since_date_iso="2035-02-01")
    assert counts["sleep_segments"] == 4
    assert counts["workouts"] == 1
    assert counts["hr_samples"] == 2

    row = conn.execute("SELECT date, activity, duration_min FROM imported_workouts").fetchone()
    assert row == ("2035-02-03", "Running", 35.0)


def test_import_into_db_is_idempotent(tmp_path):
    conn = init_db(tmp_path / "test.db")
    export_path = _write_sample(tmp_path)

    import_into_db(conn, export_path, hr_since_date_iso="2035-02-01")
    second = import_into_db(conn, export_path, hr_since_date_iso="2035-02-01")

    assert conn.execute("SELECT COUNT(*) FROM imported_daily_metrics").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM imported_sleep").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM imported_workouts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM imported_hr_samples").fetchone()[0] == 2
    assert second["sleep_segments"] == 0  # nothing new on re-run
    assert second["workouts"] == 0
    assert second["hr_samples"] == 0
