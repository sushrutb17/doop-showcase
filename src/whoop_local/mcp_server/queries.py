"""Read-only query functions backing the MCP tools. Pure functions over a
sqlite3.Connection so they're testable without spinning up the MCP/stdio
transport. Contract: MCP_SPEC.md.

Conventions (MCP_SPEC.md): dates are local-day strings 'YYYY-MM-DD'; returned
timestamps are ISO-8601 UTC; missing data is explicit ('status': 'no_data'),
never fabricated; every metric response carries a 'note' about proxy status.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

from whoop_local.compute.config import DEFAULT_CONFIG
from whoop_local.config import local_day_window_utc_ms, local_timezone
from whoop_local.db import battery_store
from whoop_local.exercise_library import service as exercise_library

RECOVERY_NOTE = "Independent proxy; not a WHOOP recovery score."
HRV_NOTE = "RMSSD/SDNN computed over cleaned RR intervals for the day; not WHOOP-equivalent."
SLEEP_NOTE = "Coarse HR-based detection; no accelerometer input yet, no clinical sleep staging."
SLEEP_NOTE_LOW_COVERAGE_SUFFIX = (
    " Capture had gaps this window, so sleep_minutes is a floor — actual sleep may be more."
)
RECOVERY_WITHHELD_NOTE = (
    " Recovery was computed and retained locally, but is withheld from decision-making "
    "because today's inputs do not meet the confidence gate (D39)."
)
STRAIN_NOTE = (
    "TRIMP-style proxy on a custom scale (Banister heart-rate-reserve weighting); "
    "not WHOOP strain (0-21). Interpretation depends on locally configured "
    "heart-rate context; the metric row does not persist HRmax provenance."
)
BATTERY_NOTE = (
    "battery_pct is self-calibrated against the highest charge level observed so "
    "far, not a manufacturer-calibrated 0-100% reading (D28) — treat it as relative, "
    "not absolute, until more full-charge cycles have been seen."
)
# D28: the band's own raw counter hit exactly 0 right before a real battery-death
# event and declined roughly linearly for ~44h before that — this threshold gives
# a few hours' runway to charge before the band goes dark and stops recording.
BATTERY_LOW_RAW_THRESHOLD = 50

IMPORTED_SLEEP_NOTE = (
    "imported_sleep_minutes comes from an Apple Health export (a second, independent "
    "observer) — shown beside the WHOOP-derived number for gap-filling and comparison, "
    "never blended into it (D38)."
)
IMPORTED_WORKOUTS_NOTE = (
    "workouts come from an Apple Health export — independent context for validating "
    "the strain proxy, not an input to it (D38)."
)
WORKOUT_LOG_NOTE = (
    "Detailed workout data comes from the canonical local logbook. Strong CSV "
    "does not declare its weight unit; imports must preserve or explicitly confirm the unit."
)
EXERCISE_LIBRARY_NOTE = (
    "Prospective external metadata for discovery only (D60/D61). An entry is not performed, "
    "planned, or canonical unless review_status is approved and a canonical_name is present. "
    "Media is excluded; instructions are non-medical reference text."
)
PLAN_NOTE = (
    "This is an intended, versioned workout template—not a record of completed work. "
    "It cannot be changed through this read-only tool."
)
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_METRIC_TABLES = {
    "hr": ("hr_samples", "hr_bpm"),
    "rr": ("rr_intervals", "rr_ms"),
    "spo2": ("spo2_samples", "spo2_pct"),
    "temp": ("temp_samples", "skin_temp_c"),
    # D38: Apple-export HR (bounded by the importer's --hr-since) for direct
    # WHOOP-vs-Watch overlay where captures overlap (Phase 2 DoD validation).
    "hr_apple": ("imported_hr_samples", "hr_bpm"),
}


def _merged_minutes(segments: list[tuple[int, int]]) -> float:
    """Total minutes covered by the union of [start_ms, end_ms) segments —
    overlapping records from multiple sources (iPhone + Watch) mustn't double-count."""
    total_ms = 0
    current_start = current_end = None
    for start, end in sorted(segments):
        if current_end is None or start > current_end:
            if current_end is not None:
                assert current_start is not None
                total_ms += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        assert current_start is not None
        total_ms += current_end - current_start
    return total_ms / 60000.0


def imported_sleep_summary(conn: sqlite3.Connection, date: str) -> dict | None:
    """Apple-export sleep for a D25-attributed date: merged minutes across the
    'asleep*' stages (union across sources), falling back to 'inbed' when the
    export predates stage detail. None if nothing was imported for that date."""
    rows = conn.execute(
        "SELECT start_utc, end_utc, stage, source_name FROM imported_sleep WHERE date = ?",
        (date,),
    ).fetchall()
    if not rows:
        return None

    asleep = [(s, e) for s, e, stage, _src in rows if stage.startswith("asleep")]
    basis = "asleep_stages" if asleep else "inbed"
    segments = asleep or [(s, e) for s, e, stage, _src in rows if stage == "inbed"]
    if not segments:
        return None
    return {
        "imported_sleep_minutes": round(_merged_minutes(segments)),
        "basis": basis,
        "sources": sorted({src for _s, _e, _stage, src in rows if src}),
    }


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recovery_confidence(
    recovery_proxy: float | None,
    rmssd_ms: float | None,
    rhr_bpm: float | None,
    sleep_minutes: int | None,
    provisional: bool | None,
    coverage_pct: float | None,
) -> dict:
    """Decision-quality gate for recovery (D39).

    The compute layer deliberately preserves every derived value. This gate
    controls whether an agent or user-facing screen may act on it. A missing
    signal is a reason to withhold, not an invitation to infer confidence.
    """
    reasons: list[str] = []
    if recovery_proxy is None or rmssd_ms is None or rhr_bpm is None:
        reasons.append("missing_recovery_inputs")
    if coverage_pct is None:
        reasons.append("coverage_unknown")
    elif coverage_pct < DEFAULT_CONFIG.recovery_actionable_min_coverage:
        reasons.append("insufficient_capture_coverage")
    if sleep_minutes is None:
        reasons.append("sleep_not_detected")
    elif sleep_minutes < DEFAULT_CONFIG.recovery_actionable_min_sleep_minutes:
        reasons.append("sleep_window_too_short")
    if provisional is None:
        reasons.append("baseline_missing")
    elif provisional:
        reasons.append("baseline_provisional")

    actionable = not reasons
    if (
        actionable
        and coverage_pct is not None
        and coverage_pct >= DEFAULT_CONFIG.coverage_low_threshold
    ):
        level = "high"
    elif actionable:
        level = "medium"
    else:
        level = "low"
    return {"level": level, "actionable": actionable, "reasons": reasons}


def _strain_confidence(strain_proxy: float | None, coverage_pct: float | None) -> dict:
    reasons: list[str] = []
    if strain_proxy is None:
        reasons.append("strain_not_computed")
    if coverage_pct is None:
        reasons.append("coverage_unknown")
    elif coverage_pct < DEFAULT_CONFIG.strain_actionable_min_coverage:
        reasons.append("insufficient_capture_coverage")
    actionable = not reasons
    if actionable:
        assert coverage_pct is not None
        level = "high" if coverage_pct >= DEFAULT_CONFIG.coverage_low_threshold else "medium"
    else:
        level = "low"
    return {"level": level, "actionable": actionable, "reasons": reasons}


def _day_bounds_utc_ms(date_str: str) -> tuple[int, int]:
    """D25: date args are local calendar dates; the window is local noon-to-noon."""
    return local_day_window_utc_ms(date.fromisoformat(date_str))


def get_status(conn: sqlite3.Connection, db_path: str) -> dict:
    last_reading = conn.execute("SELECT MAX(ts_utc) FROM hr_samples").fetchone()[0]

    last_sync_by_host: dict[str, str | None] = {}
    for (host,) in conn.execute("SELECT DISTINCT source_host FROM hr_samples"):
        ts = conn.execute(
            "SELECT MAX(ts_utc) FROM hr_samples WHERE source_host = ?", (host,)
        ).fetchone()[0]
        last_sync_by_host[host] = _iso(ts) if ts else None

    cutoff = int((datetime.now(UTC) - timedelta(hours=24)).timestamp() * 1000)
    gaps = conn.execute(
        "SELECT gap_start_utc, gap_end_utc FROM capture_gaps WHERE gap_start_utc >= ? ORDER BY gap_start_utc",
        (cutoff,),
    ).fetchall()

    row_counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("hr_samples", "rr_intervals")
    }

    schema_row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()

    battery_pct = None
    battery_low = None
    battery_raw = None
    latest_battery = battery_store.latest_battery(conn)
    raw_max = battery_store.battery_raw_max(conn)
    if latest_battery is not None:
        _battery_ts, battery_raw = latest_battery
        battery_low = battery_raw <= BATTERY_LOW_RAW_THRESHOLD
        if raw_max:
            battery_pct = round(100 * min(battery_raw, raw_max) / raw_max)

    return {
        "last_reading_utc": _iso(last_reading) if last_reading else None,
        "last_sync_by_host": last_sync_by_host,
        "coverage_gaps_24h": [
            {"start": _iso(start), "end": _iso(end) if end else None} for start, end in gaps
        ],
        "rows": row_counts,
        "battery_pct": battery_pct,
        "battery_raw": battery_raw,
        "battery_low": battery_low,
        "battery_note": BATTERY_NOTE if battery_pct is not None else None,
        "db_path": db_path,
        "schema_version": int(schema_row[0]) if schema_row else None,
    }


def get_latest_recovery(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT date, recovery_proxy, rmssd_ms, hrv_baseline_ms, rhr_bpm, sleep_minutes, "
        "provisional, coverage_pct FROM metrics_daily ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"status": "no_data", "note": RECOVERY_NOTE}

    (
        date_str,
        recovery_proxy,
        rmssd_ms,
        hrv_baseline_ms,
        rhr_bpm,
        sleep_minutes,
        provisional_raw,
        coverage_pct,
    ) = row
    provisional = bool(provisional_raw) if hrv_baseline_ms is not None else None
    confidence = _recovery_confidence(
        recovery_proxy, rmssd_ms, rhr_bpm, sleep_minutes, provisional, coverage_pct
    )
    note = RECOVERY_NOTE
    if coverage_pct is not None and coverage_pct < DEFAULT_CONFIG.coverage_low_threshold:
        note += SLEEP_NOTE_LOW_COVERAGE_SUFFIX
    if not confidence["actionable"] and recovery_proxy is not None:
        note += RECOVERY_WITHHELD_NOTE
    return {
        "date": date_str,
        # D39: preserve the computed number in metrics_daily, but do not hand
        # an unreliable score to an agent as if it were safe to act on.
        "recovery_proxy": recovery_proxy if confidence["actionable"] else None,
        "rmssd_ms": rmssd_ms,
        "hrv_baseline_ms": hrv_baseline_ms,
        "rhr_bpm": rhr_bpm,
        "sleep_minutes": sleep_minutes,
        # D26: True while hrv_baseline_ms/recovery_proxy are built on
        # 3-6 days of history instead of the full baseline_min_days window.
        "provisional": provisional,
        # D26: fraction of this day's window with live capture; below
        # coverage_low_threshold, sleep_minutes/recovery are a floor, not truth.
        "coverage_pct": coverage_pct,
        "confidence": confidence,
        "note": note,
    }


def get_hrv(conn: sqlite3.Connection, start_date: str, end_date: str) -> dict:
    rows = conn.execute(
        "SELECT date, rmssd_ms, sdnn_ms FROM metrics_daily WHERE date BETWEEN ? AND ? ORDER BY date",
        (start_date, end_date),
    ).fetchall()
    series = [{"date": d, "rmssd_ms": rmssd, "sdnn_ms": sdnn} for d, rmssd, sdnn in rows]

    result = {"range": {"start": start_date, "end": end_date}, "series": series, "note": HRV_NOTE}
    if not series:
        result["status"] = "no_data"
    return result


def get_sleep(conn: sqlite3.Connection, date: str) -> dict:
    day_start, day_end = _day_bounds_utc_ms(date)
    row = conn.execute(
        "SELECT sleep_minutes, coverage_pct FROM metrics_daily WHERE date = ?", (date,)
    ).fetchone()
    segments = conn.execute(
        "SELECT start_utc, end_utc FROM sessions WHERE type = 'sleep' AND start_utc >= ? AND start_utc < ? "
        "ORDER BY start_utc",
        (day_start, day_end),
    ).fetchall()

    imported = imported_sleep_summary(conn, date)

    if row is None and not segments and imported is None:
        return {"status": "no_data", "date": date, "note": SLEEP_NOTE}

    sleep_minutes, coverage_pct = row if row else (None, None)
    note = SLEEP_NOTE
    if coverage_pct is not None and coverage_pct < DEFAULT_CONFIG.coverage_low_threshold:
        note += SLEEP_NOTE_LOW_COVERAGE_SUFFIX

    result = {
        "date": date,
        "sleep_minutes": sleep_minutes,
        "coverage_pct": coverage_pct,
        "segments": [{"start": _iso(s), "end": _iso(e) if e else None} for s, e in segments],
        "note": note,
    }
    if imported is not None:
        result["imported_sleep"] = imported
        result["imported_note"] = IMPORTED_SLEEP_NOTE
    return result


def imported_workouts_for_date(conn: sqlite3.Connection, date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT start_utc, end_utc, activity, duration_min, source_name "
        "FROM imported_workouts WHERE date = ? ORDER BY start_utc",
        (date,),
    ).fetchall()
    return [
        {
            "start": _iso(s),
            "end": _iso(e),
            "activity": activity,
            "duration_min": duration_min,
            "source_name": source_name,
        }
        for s, e, activity, duration_min, source_name in rows
    ]


def get_strain(conn: sqlite3.Connection, date: str) -> dict:
    row = conn.execute(
        "SELECT strain_proxy, time_in_zones_json, coverage_pct FROM metrics_daily WHERE date = ?",
        (date,),
    ).fetchone()
    workouts = imported_workouts_for_date(conn, date)

    if (row is None or row[0] is None) and not workouts:
        return {"status": "no_data", "date": date, "note": STRAIN_NOTE}

    strain_value, zones_json, coverage_pct = row if row else (None, None, None)
    confidence = _strain_confidence(strain_value, coverage_pct)
    result = {
        "date": date,
        "strain_proxy": strain_value if confidence["actionable"] else None,
        "time_in_zones_min": json.loads(zones_json) if zones_json else None,
        "coverage_pct": coverage_pct,
        "confidence": confidence,
        "note": STRAIN_NOTE,
    }
    if workouts:
        result["workouts"] = workouts
        result["workouts_note"] = IMPORTED_WORKOUTS_NOTE
    return result


def query_readings(
    conn: sqlite3.Connection, metric: str, start_utc: int, end_utc: int, limit: int = 5000
) -> dict:
    if metric not in _METRIC_TABLES:
        raise ValueError(f"Unknown metric {metric!r}; must be one of {sorted(_METRIC_TABLES)}")
    table, column = _METRIC_TABLES[metric]

    rows = conn.execute(
        f"SELECT ts_utc, {column} FROM {table} WHERE ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc LIMIT ?",
        (start_utc, end_utc, limit),
    ).fetchall()

    return {
        "metric": metric,
        "count": len(rows),
        "samples": [{"ts": _iso(ts), "value": value} for ts, value in rows],
    }


def get_workout_history(
    conn: sqlite3.Connection, start_date: str, end_date: str, limit: int = 50
) -> dict:
    """Canonical workout sessions with compact set/exercise counts (D41)."""
    limit = max(1, min(limit, 200))
    rows = conn.execute(
        "SELECT ws.id, ws.local_date, ws.started_at_utc, ws.ended_at_utc, ws.name, ws.source, "
        "COUNT(DISTINCT we.id), "
        "SUM(CASE WHEN sets.set_type = 'work' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN sets.set_type = 'rest' THEN 1 ELSE 0 END) "
        "FROM workout_sessions ws "
        "LEFT JOIN workout_exercises we ON we.workout_session_id = ws.id "
        "LEFT JOIN workout_sets sets ON sets.workout_exercise_id = we.id "
        "WHERE ws.local_date BETWEEN ? AND ? "
        "GROUP BY ws.id ORDER BY ws.started_at_utc DESC LIMIT ?",
        (start_date, end_date, limit),
    ).fetchall()
    workouts = []
    for session_id, local_date, start, end, name, source, exercises, work_sets, rest_events in rows:
        workouts.append(
            {
                "id": session_id,
                "date": local_date,
                "start": _iso(start),
                "end": _iso(end) if end else None,
                "duration_min": round((end - start) / 60000, 1) if end else None,
                "name": name,
                "source": source,
                "exercise_count": exercises or 0,
                "work_sets": work_sets or 0,
                "rest_events": rest_events or 0,
            }
        )
    result = {
        "range": {"start": start_date, "end": end_date},
        "count": len(workouts),
        "workouts": workouts,
        "note": WORKOUT_LOG_NOTE,
    }
    if not workouts:
        result["status"] = "no_data"
    return result


def _estimated_one_rep_max(weight: float | None, reps: float | None) -> float | None:
    """Epley estimate, intentionally limited to ordinary strength sets."""
    if weight is None or reps is None or weight <= 0 or reps <= 0 or reps > 12:
        return None
    return round(weight * (1 + reps / 30.0), 2)


def get_exercise_history(conn: sqlite3.Connection, exercise_name: str, limit: int = 20) -> dict:
    """Recent performed sets and conservative progress statistics for one exercise."""
    limit = max(1, min(limit, 100))
    normalized = " ".join(exercise_name.split()).casefold()
    exercise = conn.execute(
        "SELECT id, canonical_name FROM exercise_catalog WHERE normalized_name = ?",
        (normalized,),
    ).fetchone()
    if exercise is None:
        suggestions = search_exercises(conn, exercise_name, limit=5)["exercises"]
        return {
            "status": "no_data",
            "exercise": exercise_name,
            "sessions": [],
            "suggestions": suggestions,
            "note": WORKOUT_LOG_NOTE,
        }

    exercise_id, canonical_name = exercise
    rows = conn.execute(
        "SELECT ws.id, ws.local_date, ws.name, sets.set_order, sets.weight, sets.weight_unit, "
        "sets.reps, sets.seconds, sets.rpe, sets.rir, sets.notes "
        "FROM workout_exercises we "
        "JOIN workout_sessions ws ON ws.id = we.workout_session_id "
        "JOIN workout_sets sets ON sets.workout_exercise_id = we.id "
        "WHERE we.exercise_id = ? AND sets.set_type = 'work' "
        "ORDER BY ws.started_at_utc DESC, we.exercise_order, sets.sequence_order LIMIT ?",
        (exercise_id, limit * 20),
    ).fetchall()

    sessions_by_id: dict[int, dict] = {}
    for (
        session_id,
        local_date,
        workout_name,
        set_order,
        weight,
        unit,
        reps,
        seconds,
        rpe,
        rir,
        notes,
    ) in rows:
        if session_id not in sessions_by_id and len(sessions_by_id) >= limit:
            continue
        session = sessions_by_id.setdefault(
            session_id,
            {"date": local_date, "workout_name": workout_name, "sets": []},
        )
        session["sets"].append(
            {
                "set_order": set_order,
                "weight": weight,
                "weight_unit": unit,
                "reps": reps,
                "seconds": seconds,
                "rpe": rpe,
                "rir": rir,
                "estimated_1rm": _estimated_one_rep_max(weight, reps),
                "notes": notes,
            }
        )

    sessions = list(sessions_by_id.values())
    all_sets = [s for session in sessions for s in session["sets"]]
    weighted = [s for s in all_sets if s["weight"] is not None and s["weight"] > 0]
    estimates = [s["estimated_1rm"] for s in all_sets if s["estimated_1rm"] is not None]
    return {
        "exercise": canonical_name,
        "session_count": len(sessions),
        "sessions": sessions,
        "summary": {
            "max_weight": max((s["weight"] for s in weighted), default=None),
            "weight_units": sorted({s["weight_unit"] for s in weighted}),
            "best_estimated_1rm": max(estimates, default=None),
            "work_sets_returned": len(all_sets),
        },
        "note": WORKOUT_LOG_NOTE,
    }


def search_exercises(conn: sqlite3.Connection, query: str = "", limit: int = 20) -> dict:
    """Discover canonical exercise names before asking for exact history."""
    limit = max(1, min(limit, 100))
    normalized = " ".join(query.split()).casefold()
    rows = conn.execute(
        "SELECT ec.canonical_name, COUNT(DISTINCT ws.id) AS sessions, MAX(ws.local_date) "
        "FROM exercise_catalog ec "
        "LEFT JOIN workout_exercises we ON we.exercise_id = ec.id "
        "LEFT JOIN workout_sessions ws ON ws.id = we.workout_session_id "
        "WHERE ec.normalized_name LIKE ? "
        "GROUP BY ec.id ORDER BY sessions DESC, ec.canonical_name LIMIT ?",
        (f"%{normalized}%", limit),
    ).fetchall()
    return {
        "query": query,
        "exercises": [
            {"name": name, "session_count": sessions, "last_performed_date": last_date}
            for name, sessions, last_date in rows
        ],
        "note": WORKOUT_LOG_NOTE,
    }


def search_exercise_library(
    conn: sqlite3.Connection,
    query: str = "",
    equipment: str | None = None,
    target: str | None = None,
    body_part: str | None = None,
    review_status: str = "all",
    limit: int = 20,
) -> dict:
    """Discover prospective exercises without promoting or recommending them."""
    entries = exercise_library.search_entries(
        conn,
        query=query,
        equipment=equipment,
        target=target,
        body_part=body_part,
        review_status=review_status,
        limit=limit,
    )
    # Search responses stay compact; the detail tool carries full instructions.
    compact = [
        {
            "external_id": entry["source_external_id"],
            "name": entry["name"],
            "body_part": entry["body_part"],
            "equipment": entry["equipment"],
            "target": entry["target"],
            "muscle_group": entry["muscle_group"],
            "secondary_muscles": entry["secondary_muscles"],
            "review_status": entry["review_status"],
            "canonical_name": entry["canonical_name"],
        }
        for entry in entries
    ]
    return {
        "query": query,
        "filters": {
            "equipment": equipment,
            "target": target,
            "body_part": body_part,
            "review_status": review_status,
        },
        "count": len(compact),
        "exercises": compact,
        "note": EXERCISE_LIBRARY_NOTE,
    }


def get_exercise_library_entry(conn: sqlite3.Connection, external_id: str) -> dict:
    """Read one prospective entry including English instructions and provenance."""
    try:
        entry = exercise_library.get_entry(conn, external_id)
    except ValueError:
        return {"status": "no_data", "external_id": external_id, "note": EXERCISE_LIBRARY_NOTE}
    return {"exercise": entry, "note": EXERCISE_LIBRARY_NOTE}


def get_training_summary(conn: sqlite3.Connection, start_date: str, end_date: str) -> dict:
    """Decision-oriented workload summary over an explicit local-date range."""
    session_count = conn.execute(
        "SELECT COUNT(*) FROM workout_sessions WHERE local_date BETWEEN ? AND ?",
        (start_date, end_date),
    ).fetchone()[0]
    set_stats = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(COALESCE(sets.reps, 0)), 0), "
        "COALESCE(SUM(COALESCE(sets.seconds, 0)), 0) "
        "FROM workout_sets sets "
        "JOIN workout_exercises we ON we.id = sets.workout_exercise_id "
        "JOIN workout_sessions ws ON ws.id = we.workout_session_id "
        "WHERE ws.local_date BETWEEN ? AND ? AND sets.set_type = 'work'",
        (start_date, end_date),
    ).fetchone()
    volume_rows = conn.execute(
        "SELECT sets.weight_unit, SUM(sets.weight * sets.reps) "
        "FROM workout_sets sets "
        "JOIN workout_exercises we ON we.id = sets.workout_exercise_id "
        "JOIN workout_sessions ws ON ws.id = we.workout_session_id "
        "WHERE ws.local_date BETWEEN ? AND ? AND sets.set_type = 'work' "
        "AND sets.weight > 0 AND sets.reps > 0 GROUP BY sets.weight_unit",
        (start_date, end_date),
    ).fetchall()
    top_rows = conn.execute(
        "SELECT ec.canonical_name, COUNT(*) AS work_sets "
        "FROM workout_sets sets "
        "JOIN workout_exercises we ON we.id = sets.workout_exercise_id "
        "JOIN workout_sessions ws ON ws.id = we.workout_session_id "
        "JOIN exercise_catalog ec ON ec.id = we.exercise_id "
        "WHERE ws.local_date BETWEEN ? AND ? AND sets.set_type = 'work' "
        "GROUP BY ec.id ORDER BY work_sets DESC, ec.canonical_name LIMIT 10",
        (start_date, end_date),
    ).fetchall()
    result = {
        "range": {"start": start_date, "end": end_date},
        "session_count": session_count,
        "work_sets": set_stats[0],
        "total_reps": set_stats[1],
        "timed_work_minutes": round(set_stats[2] / 60, 1),
        "volume_by_unit": {unit: round(volume, 2) for unit, volume in volume_rows},
        "top_exercises": [{"exercise": name, "work_sets": count} for name, count in top_rows],
        "note": WORKOUT_LOG_NOTE,
    }
    if not session_count:
        result["status"] = "no_data"
    return result


def get_active_plan(conn: sqlite3.Connection) -> dict:
    """Return the sole active intended plan and its exercise prescriptions (D46)."""
    plan = conn.execute(
        "SELECT id, name, version, starts_on, ends_on, source, notes FROM training_plans "
        "WHERE status = 'active' ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if plan is None:
        return {"status": "no_data", "workouts": [], "note": PLAN_NOTE}

    plan_id, name, version, starts_on, ends_on, source, notes = plan
    workout_rows = conn.execute(
        "SELECT id, weekday, display_order, name, kind, intensity, estimated_minutes, optional, notes "
        "FROM plan_workouts WHERE training_plan_id = ? ORDER BY weekday, display_order",
        (plan_id,),
    ).fetchall()
    workouts = []
    for (
        workout_id,
        weekday,
        _display,
        workout_name,
        kind,
        intensity,
        minutes,
        optional,
        workout_notes,
    ) in workout_rows:
        exercise_rows = conn.execute(
            "SELECT ec.canonical_name, pe.exercise_order, pe.block_label, pe.sets_target, pe.reps_min, "
            "pe.reps_max, pe.tempo, pe.rest_seconds, pe.starting_weight, pe.weight_unit, "
            "pe.substitution_note, pe.notes "
            "FROM plan_exercises pe JOIN exercise_catalog ec ON ec.id = pe.exercise_id "
            "WHERE pe.plan_workout_id = ? ORDER BY pe.exercise_order",
            (workout_id,),
        ).fetchall()
        workouts.append(
            {
                "weekday": weekday,
                "day": _WEEKDAY_NAMES[weekday],
                "name": workout_name,
                "kind": kind,
                "intensity": intensity,
                "estimated_minutes": minutes,
                "optional": bool(optional),
                "notes": workout_notes,
                "exercises": [
                    {
                        "name": exercise_name,
                        "order": order,
                        "block_label": block_label,
                        "sets_target": sets_target,
                        "reps": {"min": reps_min, "max": reps_max},
                        "tempo": tempo,
                        "rest_seconds": rest_seconds,
                        "starting_weight": starting_weight,
                        "weight_unit": weight_unit,
                        "substitution_note": substitution_note,
                        "notes": exercise_notes,
                    }
                    for (
                        exercise_name,
                        order,
                        block_label,
                        sets_target,
                        reps_min,
                        reps_max,
                        tempo,
                        rest_seconds,
                        starting_weight,
                        weight_unit,
                        substitution_note,
                        exercise_notes,
                    ) in exercise_rows
                ],
            }
        )
    return {
        "plan": {
            "id": plan_id,
            "name": name,
            "version": version,
            "starts_on": starts_on,
            "ends_on": ends_on,
            "source": source,
            "notes": notes,
        },
        "workouts": workouts,
        "note": PLAN_NOTE,
    }


def get_coaching_proposals(
    conn: sqlite3.Connection, status: str = "pending", limit: int = 20
) -> dict:
    """Review evidence-backed coaching proposals; this read tool cannot resolve them."""
    if status not in {"pending", "accepted", "rejected", "all"}:
        raise ValueError("status must be pending, accepted, rejected, or all")
    limit = max(1, min(limit, 100))
    where = "" if status == "all" else "WHERE cp.status = ?"
    params = (limit,) if status == "all" else (status, limit)
    rows = conn.execute(
        "SELECT cp.id, cp.kind, cp.status, cp.title, cp.rationale, cp.evidence_json, cp.change_json, "
        "cp.created_at, cp.resolved_at, cp.resulting_plan_id, tp.name, tp.version "
        "FROM coaching_proposals cp JOIN training_plans tp ON tp.id=cp.training_plan_id "
        f"{where} ORDER BY cp.created_at DESC LIMIT ?",
        params,
    ).fetchall()
    return {
        "status_filter": status,
        "count": len(rows),
        "proposals": [
            {
                "id": proposal_id,
                "kind": kind,
                "status": proposal_status,
                "title": title,
                "rationale": rationale,
                "evidence": json.loads(evidence),
                "change": json.loads(change),
                "created_at": created_at,
                "resolved_at": resolved_at,
                "resulting_plan_id": resulting_plan_id,
                "plan": {"name": plan_name, "version": plan_version},
                "confirmation_required": proposal_status == "pending",
            }
            for (
                proposal_id,
                kind,
                proposal_status,
                title,
                rationale,
                evidence,
                change,
                created_at,
                resolved_at,
                resulting_plan_id,
                plan_name,
                plan_version,
            ) in rows
        ],
        "note": "Proposals are evidence-backed suggestions. This tool cannot accept, reject, or alter a plan.",
    }


def _active_plan_row(conn: sqlite3.Connection) -> tuple | None:
    return conn.execute(
        "SELECT id, name, version, created_at, starts_on FROM training_plans WHERE status='active' "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()


def _planned_slots(
    conn: sqlite3.Connection, start: date, end: date
) -> tuple[dict | None, list[dict]]:
    plan = _active_plan_row(conn)
    if plan is None:
        return None, []
    plan_id, plan_name, version, created_at, starts_on = plan
    effective_start = (
        date.fromisoformat(starts_on)
        if starts_on
        else datetime.fromtimestamp(created_at / 1000, tz=UTC).astimezone().date()
    )
    range_start = max(start, effective_start)
    if range_start > end:
        return {
            "id": plan_id,
            "name": plan_name,
            "version": version,
            "effective_start": effective_start.isoformat(),
        }, []
    templates = conn.execute(
        "SELECT weekday, name, kind, intensity, estimated_minutes, optional FROM plan_workouts "
        "WHERE training_plan_id=? ORDER BY display_order",
        (plan_id,),
    ).fetchall()
    slots = []
    cursor = range_start
    while cursor <= end:
        for weekday, name, kind, intensity, minutes, optional in templates:
            if cursor.weekday() != weekday:
                continue
            completed = False
            completion_source = None
            if kind == "sport":
                completed = (
                    conn.execute(
                        "SELECT 1 FROM imported_workouts WHERE date=? AND lower(activity)=lower(?) LIMIT 1",
                        (cursor.isoformat(), name),
                    ).fetchone()
                    is not None
                )
                completion_source = "apple_health" if completed else None
            elif kind == "strength":
                completed = (
                    conn.execute(
                        "SELECT 1 FROM workout_sessions WHERE local_date=? AND lower(name)=lower(?) LIMIT 1",
                        (cursor.isoformat(), name),
                    ).fetchone()
                    is not None
                )
                completion_source = "detailed_logbook" if completed else None
            slots.append(
                {
                    "date": cursor.isoformat(),
                    "day": _WEEKDAY_NAMES[weekday],
                    "name": name,
                    "kind": kind,
                    "intensity": intensity,
                    "estimated_minutes": minutes,
                    "optional": bool(optional),
                    "completed": completed,
                    "completion_source": completion_source,
                }
            )
        cursor += timedelta(days=1)
    return {
        "id": plan_id,
        "name": plan_name,
        "version": version,
        "effective_start": effective_start.isoformat(),
    }, slots


def get_plan_adherence(conn: sqlite3.Connection, start_date: str, end_date: str) -> dict:
    """Compare planned required slots with completed detailed/sport records."""
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if end < start:
        raise ValueError("end_date cannot be before start_date")
    if (end - start).days > 92:
        raise ValueError("adherence ranges are limited to 93 days")
    plan, slots = _planned_slots(conn, start, end)
    if plan is None:
        return {
            "status": "no_data",
            "range": {"start": start_date, "end": end_date},
            "slots": [],
            "note": "No active plan exists.",
        }
    required = [slot for slot in slots if not slot["optional"]]
    completed = [slot for slot in required if slot["completed"]]
    return {
        "range": {"start": start_date, "end": end_date},
        "plan": plan,
        "slots": slots,
        "required_slots": len(required),
        "completed_required_slots": len(completed),
        "adherence_pct": round(100 * len(completed) / len(required), 1) if required else None,
        "missed_required": [slot for slot in required if not slot["completed"]],
        "note": "Strength completion requires a matching detailed log; sport completion uses labeled Apple Health workouts. Optional recovery does not lower adherence.",
    }


def _recovery_for_date(conn: sqlite3.Connection, date_str: str) -> dict:
    row = conn.execute(
        "SELECT recovery_proxy, rmssd_ms, rhr_bpm, sleep_minutes, provisional, coverage_pct "
        "FROM metrics_daily WHERE date=?",
        (date_str,),
    ).fetchone()
    if row is None:
        return {
            "value": None,
            "confidence": {"level": "low", "actionable": False, "reasons": ["no_recovery_row"]},
        }
    recovery, rmssd, rhr, sleep, provisional_raw, coverage = row
    provisional = bool(provisional_raw) if provisional_raw is not None else None
    confidence = _recovery_confidence(recovery, rmssd, rhr, sleep, provisional, coverage)
    return {
        "value": recovery if confidence["actionable"] else None,
        "confidence": confidence,
        "coverage_pct": coverage,
        "sleep_minutes": sleep,
    }


def get_training_guidance(conn: sqlite3.Connection, date_str: str) -> dict:
    """Return today's plan plus a D39-gated, non-mutating adjustment suggestion."""
    target = date.fromisoformat(date_str)
    plan, slots = _planned_slots(conn, target, target)
    recovery = _recovery_for_date(conn, date_str)
    if plan is None:
        return {
            "status": "no_data",
            "date": date_str,
            "planned": [],
            "recovery": recovery,
            "recommendation": {
                "action": "no_active_plan",
                "message": "No active training plan exists.",
            },
        }
    if not slots:
        return {
            "date": date_str,
            "plan": plan,
            "planned": [],
            "recovery": recovery,
            "recommendation": {
                "action": "rest_or_easy_recovery",
                "message": "No session is planned today. Rest or use easy mobility if helpful.",
                "plan_mutated": False,
            },
        }
    active_plan = get_active_plan(conn)
    active_plan_meta = active_plan.get("plan", {})
    if active_plan_meta.get("notes"):
        plan["notes"] = active_plan_meta["notes"]
    plan_workouts = active_plan.get("workouts", [])
    for slot in slots:
        matching = next(
            (
                workout
                for workout in plan_workouts
                if workout["day"] == slot["day"] and workout["name"] == slot["name"]
            ),
            None,
        )
        if matching is not None:
            slot["workout_notes"] = matching.get("notes")
            slot["exercises"] = matching.get("exercises", [])
    primary = next((slot for slot in slots if not slot["optional"]), slots[0])
    confidence = recovery["confidence"]
    if not confidence["actionable"]:
        recommendation = {
            "action": "follow_plan_no_biometric_adjustment",
            "message": "Recovery evidence is not reliable enough to adjust the plan. Follow the scheduled session using symptoms and perceived effort.",
            "reason_codes": confidence["reasons"],
            "plan_mutated": False,
        }
    elif recovery["value"] < 40:
        if primary["kind"] == "strength":
            recommendation = {
                "action": "reduce_session",
                "message": "Keep the movement pattern but reduce planned work: about 75% of sets and 90% of load; stop well before failure.",
                "sets_multiplier": 0.75,
                "load_multiplier": 0.9,
                "plan_mutated": False,
            }
        else:
            recommendation = {
                "action": "easy_or_skip",
                "message": "Recovery is low. Keep conditioning easy or skip if symptoms, pain, or unusual fatigue are present.",
                "plan_mutated": False,
            }
    elif recovery["value"] <= 65:
        recommendation = {
            "action": "perform_as_planned",
            "message": "Recovery is adequate. Perform the session as written and keep 2–3 reps in reserve.",
            "plan_mutated": False,
        }
    else:
        recommendation = {
            "action": "perform_as_planned",
            "message": "Recovery is strong. Perform the session as written; do not add unplanned volume or failure work.",
            "plan_mutated": False,
        }
    presentation = {
        "session_state": "completed" if primary["completed"] else "planned_not_logged",
        "session_phrase": (
            "Completed and present in the canonical log."
            if primary["completed"]
            else "Planned; not yet logged as completed."
        ),
        "recovery_phrase": recommendation["message"],
        "rules": [
            "Do not call a planned session confirmed, completed, done, or green-lit.",
            "Confidence describes evidence quality only; do not invent recovery or sleep categories.",
            "Preserve exercise names, loads, units, sets, reps, RIR, and safety notes exactly.",
        ],
    }
    required_lines = [presentation["session_phrase"]]
    required_lines.append(
        f"{primary['name']} — {primary['intensity']} — about {primary['estimated_minutes']} minutes."
    )
    if plan.get("notes"):
        required_lines.append(f"Plan instructions: {plan['notes']}")
    for exercise in primary.get("exercises", []):
        reps = exercise["reps"]
        rep_text = (
            str(reps["min"]) if reps["min"] == reps["max"] else f"{reps['min']}–{reps['max']}"
        )
        weight = exercise.get("starting_weight")
        weight_text = f" @ {weight:g} {exercise['weight_unit']}" if weight is not None else ""
        line = (
            f"{exercise['name']}: {exercise['sets_target']} × {rep_text}{weight_text}; "
            f"tempo {exercise['tempo']}; rest {exercise['rest_seconds']}s."
        )
        if exercise.get("substitution_note"):
            line += f" {exercise['substitution_note']}"
        if exercise.get("notes"):
            line += f" {exercise['notes']}"
        required_lines.append(line)
    actionable_text = "actionable" if recovery["confidence"]["actionable"] else "not actionable"
    required_lines.append(
        f"Recovery confidence: {recovery['confidence']['level']} ({actionable_text}). "
        f"{presentation['recovery_phrase']}"
    )
    required_lines.append(
        "Pain, illness, severe fatigue, dizziness, chest pain, or acute injury overrides this guidance."
    )
    presentation["required_telegram_lines"] = required_lines
    return {
        "date": date_str,
        "plan": plan,
        "planned": slots,
        "recovery": recovery,
        "recommendation": recommendation,
        "presentation": presentation,
        "note": "Guidance is advisory and never edits the active plan. Pain, illness, or severe fatigue overrides biometric guidance.",
    }


def get_telegram_training_guidance(conn: sqlite3.Connection, date_str: str) -> dict:
    """Minimum-disclosure, deterministic relay envelope for Telegram."""
    guidance = get_training_guidance(conn, date_str)
    presentation = guidance.get("presentation", {})
    lines = presentation.get("required_telegram_lines")
    if not lines:
        recommendation = guidance.get("recommendation", {})
        recovery = guidance.get("recovery", {})
        confidence = recovery.get("confidence", {"level": "low", "actionable": False})
        actionable = "actionable" if confidence.get("actionable") else "not actionable"
        lines = [
            f"Date: {date_str}.",
            recommendation.get("message", "No active training plan exists."),
            f"Recovery confidence: {confidence.get('level', 'low')} ({actionable}).",
            "Pain, illness, severe fatigue, dizziness, chest pain, or acute injury overrides this guidance.",
        ]
    return {
        "date": date_str,
        "text": "Doop training guidance:\n" + "\n".join(lines),
        "relay_contract": "Return text verbatim as the visible answer. Do not call another tool or add health facts.",
    }


def _local_today(now_utc: datetime | None = None) -> date:
    instant = now_utc or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("local-day clock must be timezone-aware")
    return instant.astimezone(local_timezone()).date()


def get_weekly_training_review(
    conn: sqlite3.Connection,
    week_start: str,
    *,
    as_of_date: date | None = None,
) -> dict:
    """Seven-day adherence, completed workload, and actionable-recovery summary."""
    start = date.fromisoformat(week_start)
    if start.weekday() != 0:
        raise ValueError("week_start must be a Monday")
    end = start + timedelta(days=6)
    today = as_of_date or _local_today()
    through = min(end, today)
    if through < start:
        return {
            "status": "no_data",
            "week": {"start": start.isoformat(), "end": end.isoformat(), "through": None},
            "summary": "This week has not started yet.",
            "note": "Future sessions are never counted as missed.",
        }
    adherence = get_plan_adherence(conn, start.isoformat(), through.isoformat())
    training = get_training_summary(conn, start.isoformat(), through.isoformat())
    recovery_days = []
    for offset in range((through - start).days + 1):
        day = start + timedelta(days=offset)
        item = _recovery_for_date(conn, day.isoformat())
        recovery_days.append({"date": day.isoformat(), **item})
    actionable_values = [item["value"] for item in recovery_days if item["value"] is not None]
    missed = adherence.get("missed_required", [])
    if not adherence.get("required_slots"):
        summary = "No required plan slots fell in this week."
    elif not missed:
        summary = (
            "All required plan slots were completed. Keep progression conservative and repeatable."
        )
    else:
        summary = f"{len(missed)} required slot(s) lack a matching completion record. Confirm whether they were missed or simply not logged."
    note = (
        "Only D39-actionable recovery values contribute to the weekly average; future sessions "
        "are never counted as missed, and missing detailed logs are not automatically proof a "
        "workout was skipped."
    )
    result = {
        "week": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "through": through.isoformat(),
        },
        "adherence": adherence,
        "training": training,
        "recovery_days": recovery_days,
        "actionable_recovery_days": len(actionable_values),
        "average_actionable_recovery": round(sum(actionable_values) / len(actionable_values), 1)
        if actionable_values
        else None,
        "summary": summary,
        "note": note,
    }
    lines = [f"Week: {start.isoformat()} through {through.isoformat()}."]
    lines.append(
        f"Completion: {adherence.get('completed_required_slots', 0)}/"
        f"{adherence.get('required_slots', 0)} required slots have matching completion records."
    )
    for slot in adherence.get("slots", []):
        if slot["completed"]:
            state = "completed in the canonical record"
        elif slot["optional"]:
            state = "optional; not scored"
        else:
            state = "required; no matching completion record"
        lines.append(f"{slot['date']} — {slot['name']}: {state}.")
    average = result["average_actionable_recovery"]
    average_text = f"average {average:g}" if average is not None else "no average available"
    lines.append(
        f"Recovery evidence: {result['actionable_recovery_days']} actionable day(s); {average_text}."
    )
    lines.extend((summary, note))
    result["presentation"] = {
        "required_telegram_lines": lines,
        "rules": [
            "Copy every line's factual content; Markdown labels may not replace facts.",
            "Do not call a no-record slot missed or skipped.",
            "Do not count future or optional sessions as missed.",
        ],
    }
    return result


def get_telegram_weekly_review(
    conn: sqlite3.Connection,
    week_start: str,
    *,
    as_of_date: date | None = None,
) -> dict:
    """Minimum-disclosure, deterministic weekly relay envelope for Telegram."""
    review = get_weekly_training_review(conn, week_start, as_of_date=as_of_date)
    lines = review.get("presentation", {}).get("required_telegram_lines", [])
    return {
        "week_start": week_start,
        "text": "Doop weekly review:\n" + "\n".join(lines),
        "relay_contract": (
            "Return text verbatim as the visible answer. Do not call another tool, add health facts, "
            "or turn a missing log into proof of missed training."
        ),
    }
