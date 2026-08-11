"""MCP query contract tests using invented, future-epoch data only.

Every date, timestamp, device reading, workout, and physiological value in
this module is synthetic. None is copied from an owner profile or capture.
"""

import json
from zoneinfo import ZoneInfo

import pytest

from whoop_local.db.schema import SCHEMA_VERSION, init_db
from whoop_local.exercise_library.service import import_dataset
from whoop_local.mcp_server.queries import (
    get_exercise_history,
    get_exercise_library_entry,
    get_hrv,
    get_latest_recovery,
    get_sleep,
    get_status,
    get_strain,
    get_training_summary,
    get_workout_history,
    query_readings,
    search_exercise_library,
    search_exercises,
)


def _conn(tmp_path):
    return init_db(tmp_path / "test.db")


def test_get_status_empty_db(tmp_path):
    conn = _conn(tmp_path)
    status = get_status(conn, "test.db")
    assert status["last_reading_utc"] is None
    assert status["last_sync_by_host"] == {}
    assert status["coverage_gaps_24h"] == []
    assert status["rows"] == {"hr_samples": 0, "rr_intervals": 0}
    assert status["schema_version"] == int(SCHEMA_VERSION)


def test_get_status_with_data_and_gap(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) "
        "VALUES (2278663200000, 68, 'synthetic-laptop', 'whoop')"
    )
    conn.execute(
        "INSERT INTO capture_gaps (source_host, gap_start_utc, gap_end_utc, reason) "
        "VALUES ('synthetic-laptop', 2278663200000, 2278663320000, 'disconnected')"
    )
    conn.commit()

    status = get_status(conn, "test.db")
    assert status["last_reading_utc"] == "2042-03-17T10:00:00Z"
    assert status["last_sync_by_host"] == {"synthetic-laptop": "2042-03-17T10:00:00Z"}
    assert status["rows"]["hr_samples"] == 1


def test_get_status_battery_fields_absent_when_no_samples(tmp_path):
    conn = _conn(tmp_path)
    status = get_status(conn, "test.db")
    assert status["battery_pct"] is None
    assert status["battery_low"] is None
    assert status["battery_note"] is None


def test_get_status_battery_pct_relative_to_observed_max(tmp_path):
    from whoop_local.db.battery_store import record_battery_sample

    conn = _conn(tmp_path)
    # Synthetic raw counter values exercise relative calibration only.
    record_battery_sample(conn, 2278663200000, 613, "synthetic-laptop")
    record_battery_sample(conn, 2278663260000, 337, "synthetic-laptop")
    conn.commit()

    status = get_status(conn, "test.db")
    assert status["battery_raw"] == 337
    assert status["battery_pct"] == 55
    assert status["battery_low"] is False
    assert status["battery_note"] is not None


def test_get_status_battery_low_warning(tmp_path):
    from whoop_local.db.battery_store import record_battery_sample

    conn = _conn(tmp_path)
    record_battery_sample(conn, 2278663200000, 613, "synthetic-laptop")
    record_battery_sample(conn, 2278663260000, 17, "synthetic-laptop")
    conn.commit()

    status = get_status(conn, "test.db")
    assert status["battery_low"] is True


def test_get_latest_recovery_no_data(tmp_path):
    conn = _conn(tmp_path)
    result = get_latest_recovery(conn)
    assert result["status"] == "no_data"
    assert "note" in result


def test_get_latest_recovery_returns_latest_row_with_nulls_preserved(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO metrics_daily (date, rhr_bpm) VALUES ('2042-03-16', 66.0)")
    conn.execute(
        "INSERT INTO metrics_daily (date, rhr_bpm, recovery_proxy) "
        "VALUES ('2042-03-17', 62.7, 44.0)"
    )
    conn.commit()

    result = get_latest_recovery(conn)
    assert result["date"] == "2042-03-17"
    assert result["recovery_proxy"] is None  # missing inputs are withheld
    assert result["confidence"]["actionable"] is False
    assert result["rmssd_ms"] is None  # never fabricated
    assert result["provisional"] is None  # no hrv_baseline_ms at all -- nothing to flag


def test_get_latest_recovery_flags_provisional_baseline(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, rhr_bpm, hrv_baseline_ms, recovery_proxy, provisional) "
        "VALUES ('2042-03-17', 61.5, 46.5, 57.2, 1)"
    )
    conn.commit()

    result = get_latest_recovery(conn)
    assert result["provisional"] is True


def test_get_latest_recovery_low_coverage_adds_caveat_to_note(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, rhr_bpm, sleep_minutes, coverage_pct) "
        "VALUES ('2042-03-17', 65.5, 211, 0.43)"
    )
    conn.commit()

    result = get_latest_recovery(conn)
    assert result["coverage_pct"] == 0.43
    assert "floor" in result["note"]


def test_get_latest_recovery_withholds_low_coverage_score(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, recovery_proxy, rmssd_ms, hrv_baseline_ms, rhr_bpm, "
        "sleep_minutes, provisional, coverage_pct) "
        "VALUES ('2042-03-17', 63.4, 41.7, 48.9, 67.2, 347, 0, 0.11)"
    )
    conn.commit()

    result = get_latest_recovery(conn)
    assert result["recovery_proxy"] is None
    assert result["confidence"] == {
        "level": "low",
        "actionable": False,
        "reasons": ["insufficient_capture_coverage"],
    }
    assert "withheld" in result["note"]


def test_get_latest_recovery_returns_actionable_score_when_quality_passes(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, recovery_proxy, rmssd_ms, hrv_baseline_ms, rhr_bpm, "
        "sleep_minutes, provisional, coverage_pct) "
        "VALUES ('2042-03-17', 69.3, 51.4, 49.6, 60.8, 392, 0, 0.97)"
    )
    conn.commit()

    result = get_latest_recovery(conn)
    assert result["recovery_proxy"] == 69.3
    assert result["confidence"] == {"level": "high", "actionable": True, "reasons": []}


def test_get_hrv_no_data_in_range(tmp_path):
    conn = _conn(tmp_path)
    result = get_hrv(conn, "2042-02-01", "2042-02-07")
    assert result["status"] == "no_data"
    assert result["series"] == []


def test_get_hrv_returns_series_in_range(tmp_path):
    conn = _conn(tmp_path)
    for d, rmssd in [("2042-03-10", 45.2), ("2042-03-11", 53.8), ("2042-03-18", 88.4)]:
        conn.execute("INSERT INTO metrics_daily (date, rmssd_ms) VALUES (?, ?)", (d, rmssd))
    conn.commit()

    result = get_hrv(conn, "2042-03-10", "2042-03-11")
    assert [s["date"] for s in result["series"]] == ["2042-03-10", "2042-03-11"]
    assert "status" not in result


def test_get_sleep_no_data(tmp_path):
    conn = _conn(tmp_path)
    result = get_sleep(conn, "2042-03-17")
    assert result["status"] == "no_data"


def test_get_sleep_with_segments(tmp_path, monkeypatch):
    # Day bounds are local noon-to-noon; pin the system tz to UTC so this
    # test doesn't depend on the machine it runs on.
    monkeypatch.setattr("whoop_local.config.local_timezone", lambda: ZoneInfo("UTC"))

    conn = _conn(tmp_path)
    conn.execute("INSERT INTO metrics_daily (date, sleep_minutes) VALUES ('2042-03-17', 386)")
    # Synthetic 2042-03-17T02:00:00Z -> 09:00:00Z falls inside the
    # 2042-03-17 local noon-to-noon UTC window.
    conn.execute(
        "INSERT INTO sessions (type, start_utc, end_utc, source) "
        "VALUES ('sleep', 2278634400000, 2278659600000, 'synthetic')"
    )
    conn.commit()

    result = get_sleep(conn, "2042-03-17")
    assert result["sleep_minutes"] == 386
    assert len(result["segments"]) == 1


def test_get_sleep_low_coverage_adds_at_least_caveat(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, sleep_minutes, coverage_pct) "
        "VALUES ('2042-03-17', 243, 0.68)"
    )
    conn.commit()

    result = get_sleep(conn, "2042-03-17")
    assert result["coverage_pct"] == 0.68
    assert "floor" in result["note"]


def test_get_sleep_full_coverage_has_no_caveat(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, sleep_minutes, coverage_pct) "
        "VALUES ('2042-03-17', 397, 1.0)"
    )
    conn.commit()

    result = get_sleep(conn, "2042-03-17")
    assert "floor" not in result["note"]


def test_get_strain_no_data(tmp_path):
    conn = _conn(tmp_path)
    result = get_strain(conn, "2042-03-17")
    assert result["status"] == "no_data"


def test_get_strain_withholds_stored_value_when_coverage_unknown(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, strain_proxy, time_in_zones_json) "
        "VALUES ('2042-03-17', 13.7, '{\"z1\": 17, \"z2\": 6}')"
    )
    conn.commit()

    result = get_strain(conn, "2042-03-17")
    assert result["strain_proxy"] is None  # unknown coverage is withheld
    assert result["confidence"]["actionable"] is False
    assert result["time_in_zones_min"] == {"z1": 17, "z2": 6}
    assert "status" not in result


def test_get_strain_returns_value_when_coverage_passes(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, strain_proxy, time_in_zones_json, coverage_pct) "
        "VALUES ('2042-03-17', 13.7, '{\"z1\": 17}', 0.97)"
    )
    conn.commit()

    result = get_strain(conn, "2042-03-17")
    assert result["strain_proxy"] == 13.7
    assert result["confidence"] == {"level": "high", "actionable": True, "reasons": []}
    assert "locally configured heart-rate context" in result["note"]
    assert "estimated from age" not in result["note"]


def test_query_readings_hr(tmp_path):
    conn = _conn(tmp_path)
    for ts, hr in [(2278663200000, 64), (2278663201000, 66), (2278663202000, 65)]:
        conn.execute(
            "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) "
            "VALUES (?, ?, 'synthetic-laptop', 'whoop')",
            (ts, hr),
        )
    conn.commit()

    result = query_readings(conn, "hr", 2278663200000, 2278663202500)
    assert result["metric"] == "hr"
    assert result["count"] == 3
    assert result["samples"][0]["value"] == 64


def test_query_readings_respects_limit(tmp_path):
    conn = _conn(tmp_path)
    for i in range(10):
        conn.execute(
            "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) "
            "VALUES (?, ?, 'synthetic-laptop', 'whoop')",
            (2278663200000 + i * 1000, 64 + i),
        )
    conn.commit()

    result = query_readings(conn, "hr", 2278663200000, 2278663220000, limit=3)
    assert result["count"] == 3


def test_query_readings_rejects_unknown_metric(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        query_readings(conn, "bogus", 2278663200000, 2278663200001)


# --- Apple-export data surfaced beside WHOOP-derived numbers ---


def _insert_imported_sleep(conn, date, start_ms, end_ms, stage, source_name="Synthetic Watch"):
    conn.execute(
        "INSERT INTO imported_sleep (date, start_utc, end_utc, stage, source_name, source, imported_at) "
        "VALUES (?, ?, ?, ?, ?, 'apple_health_export', ?)",
        (date, start_ms, end_ms, stage, source_name, 2280488400000),
    )


def test_get_sleep_includes_imported_sleep_when_present(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, sleep_minutes, coverage_pct) "
        "VALUES ('2042-04-07', NULL, 0.46)"
    )
    # Synthetic 23:10-03:10 core plus overlapping 02:40-04:25 deep merges
    # to 315 minutes, not the 345-minute sum.
    _insert_imported_sleep(conn, "2042-04-07", 2280438600000, 2280453000000, "asleep_core")
    _insert_imported_sleep(conn, "2042-04-07", 2280451200000, 2280457500000, "asleep_deep")
    # inbed and awake segments don't inflate the asleep number
    _insert_imported_sleep(
        conn,
        "2042-04-07",
        2280438000000,
        2280459600000,
        "inbed",
        source_name="Synthetic Phone",
    )
    conn.commit()

    result = get_sleep(conn, "2042-04-07")
    assert result["sleep_minutes"] is None  # WHOOP-derived value untouched
    assert result["imported_sleep"]["imported_sleep_minutes"] == 315
    assert result["imported_sleep"]["basis"] == "asleep_stages"
    assert result["imported_sleep"]["sources"] == ["Synthetic Phone", "Synthetic Watch"]
    assert "imported_note" in result


def test_get_sleep_imported_falls_back_to_inbed_for_old_exports(tmp_path):
    conn = _conn(tmp_path)
    _insert_imported_sleep(
        conn,
        "2042-04-07",
        2280439800000,
        2280463620000,
        "inbed",
        source_name="Synthetic Phone",
    )
    conn.commit()

    result = get_sleep(conn, "2042-04-07")  # no metrics_daily row at all
    assert "status" not in result  # imported data alone is enough to answer
    assert result["imported_sleep"]["imported_sleep_minutes"] == 397
    assert result["imported_sleep"]["basis"] == "inbed"


def test_get_sleep_without_imported_data_has_no_imported_keys(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, sleep_minutes, coverage_pct) "
        "VALUES ('2042-04-07', 376, 0.91)"
    )
    conn.commit()

    result = get_sleep(conn, "2042-04-07")
    assert "imported_sleep" not in result
    assert "imported_note" not in result


def test_get_strain_includes_imported_workouts(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, strain_proxy, time_in_zones_json) "
        "VALUES ('2042-04-07', 10.6, '{\"z1\": 14}')"
    )
    conn.execute(
        "INSERT INTO imported_workouts (date, start_utc, end_utc, activity, duration_min, "
        "source_name, source, imported_at) "
        "VALUES ('2042-04-07', 2280484800000, 2280487020000, 'Synthetic Circuit', "
        "37.0, 'Synthetic Watch', 'apple_health_export', 2280488400000)"
    )
    conn.commit()

    result = get_strain(conn, "2042-04-07")
    assert result["strain_proxy"] is None
    assert result["workouts"][0]["activity"] == "Synthetic Circuit"
    assert result["workouts"][0]["duration_min"] == 37.0
    assert "workouts_note" in result


def test_get_strain_workouts_alone_are_not_no_data(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO imported_workouts (date, start_utc, end_utc, activity, duration_min, "
        "source_name, source, imported_at) "
        "VALUES ('2042-04-07', 2280484800000, 2280487020000, 'Synthetic Circuit', "
        "37.0, 'Synthetic Watch', 'apple_health_export', 2280488400000)"
    )
    conn.commit()

    result = get_strain(conn, "2042-04-07")
    assert "status" not in result
    assert result["strain_proxy"] is None  # honest: no WHOOP-derived strain that day
    assert len(result["workouts"]) == 1


def test_query_readings_hr_apple(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO imported_hr_samples (ts_utc, hr_bpm, source_name, source, imported_at) "
        "VALUES (2278663200000, 83.5, 'Synthetic Watch', 'apple_health_export', "
        "2280488400000)"
    )
    conn.commit()

    result = query_readings(conn, "hr_apple", 2278663200000, 2278663202500)
    assert result["count"] == 1
    assert result["samples"][0]["value"] == 83.5


# --- Canonical workout analytics for agent context ---


def _insert_logbook_workout(conn):
    conn.execute(
        "INSERT INTO exercise_catalog (id, canonical_name, normalized_name, created_at) "
        "VALUES (1, 'Bench Press', 'bench press', 2279987400000)"
    )
    conn.execute(
        "INSERT INTO workout_sessions "
        "(id, started_at_utc, ended_at_utc, local_date, name, source, source_external_id, imported_at) "
        "VALUES (1, 2279988000000, 2279991600000, '2042-04-01', "
        "'Synthetic Push Day', 'strong_csv', 'synthetic-001', 2279991900000)"
    )
    conn.execute(
        "INSERT INTO workout_exercises (id, workout_session_id, exercise_id, exercise_order) "
        "VALUES (1, 1, 1, 1)"
    )
    conn.execute(
        "INSERT INTO workout_sets "
        "(workout_exercise_id, set_order, sequence_order, set_type, source_set_label, weight, "
        "weight_unit, reps, rpe, seconds, source_row_hash) "
        "VALUES (1, 1, 1, 'work', '1', 74, 'kg', 9, 7.5, 0, 'synthetic-a')"
    )
    conn.execute(
        "INSERT INTO workout_sets "
        "(workout_exercise_id, set_order, sequence_order, set_type, source_set_label, weight, "
        "weight_unit, reps, seconds, source_row_hash) "
        "VALUES (1, 2, 2, 'rest', 'Rest Timer', 0, 'kg', 0, 105, 'synthetic-b')"
    )
    conn.commit()


def test_get_workout_history_returns_compact_sessions(tmp_path):
    conn = _conn(tmp_path)
    _insert_logbook_workout(conn)
    result = get_workout_history(conn, "2042-04-01", "2042-04-07")
    assert result["count"] == 1
    assert result["workouts"][0]["duration_min"] == 60.0
    assert result["workouts"][0]["exercise_count"] == 1
    assert result["workouts"][0]["work_sets"] == 1
    assert result["workouts"][0]["rest_events"] == 1


def test_get_exercise_history_returns_sets_and_estimated_1rm(tmp_path):
    conn = _conn(tmp_path)
    _insert_logbook_workout(conn)
    result = get_exercise_history(conn, "bench press")
    assert result["exercise"] == "Bench Press"
    assert result["session_count"] == 1
    assert result["summary"]["max_weight"] == 74
    assert result["summary"]["best_estimated_1rm"] == 96.2
    assert len(result["sessions"][0]["sets"]) == 1  # rest event excluded


def test_get_exercise_history_unknown_name_is_no_data(tmp_path):
    conn = _conn(tmp_path)
    result = get_exercise_history(conn, "Not An Exercise")
    assert result["status"] == "no_data"


def test_search_exercises_discovers_canonical_name(tmp_path):
    conn = _conn(tmp_path)
    _insert_logbook_workout(conn)
    result = search_exercises(conn, "bench")
    assert result["exercises"] == [
        {"name": "Bench Press", "session_count": 1, "last_performed_date": "2042-04-01"}
    ]


def test_exercise_library_reads_are_explicitly_prospective(tmp_path):
    conn = _conn(tmp_path)
    path = tmp_path / "library.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "0042",
                    "name": "cable neutral grip row",
                    "body_part": "back",
                    "equipment": "cable",
                    "target": "upper back",
                    "muscle_group": "lats",
                    "secondary_muscles": ["biceps"],
                    "instructions": {"en": "Keep the forearms aligned and row."},
                    "instruction_steps": {"en": ["Set the grip.", "Row."]},
                    "image": "images/not-imported.jpg",
                    "gif_url": "videos/not-imported.gif",
                }
            ]
        ),
        encoding="utf-8",
    )
    import_dataset(conn, path)

    search = search_exercise_library(conn, query="neutral", equipment="cable")
    assert search["count"] == 1
    assert search["exercises"][0]["review_status"] == "unreviewed"
    assert search["exercises"][0]["canonical_name"] is None
    assert "not performed" in search["note"]
    detail = get_exercise_library_entry(conn, "0042")
    assert detail["exercise"]["instruction_steps"] == ["Set the grip.", "Row."]
    assert detail["exercise"]["source_license"].startswith("MIT")


def test_exercise_library_missing_detail_is_no_data(tmp_path):
    result = get_exercise_library_entry(_conn(tmp_path), "missing")
    assert result["status"] == "no_data"


def test_get_training_summary_aggregates_work_only(tmp_path):
    conn = _conn(tmp_path)
    _insert_logbook_workout(conn)
    result = get_training_summary(conn, "2042-04-01", "2042-04-07")
    assert result["session_count"] == 1
    assert result["work_sets"] == 1
    assert result["total_reps"] == 9
    assert result["volume_by_unit"] == {"kg": 666.0}
    assert result["top_exercises"] == [{"exercise": "Bench Press", "work_sets": 1}]
