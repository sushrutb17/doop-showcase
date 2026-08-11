"""Import of an Apple Health export.xml (D33 daily metrics; D38 sleep,
workouts, and bounded heart-rate samples).

Two jobs, same separation principle (D6/D9 "proxies, not parity" — never
blend measurement methodologies silently):

1. `imported_daily_metrics` (D33, Phase 6): daily RestingHeartRate medians
   seed the RHR leg of the rolling baseline during the cold start only — see
   compute_daily.py's `_load_history`. Apple's HRV (SDNN) is stored for
   reference, never blended into our RMSSD-based baseline.

2. `imported_sleep` / `imported_workouts` / `imported_hr_samples` (D38,
   Phase 6): the Apple Watch as a *second observer*. These fill reporting
   gaps (nights/workouts the WHOOP band spent out of collector range) and
   back the Phase 2 DoD validation ("sanity-check against another HR
   source") — surfaced beside WHOOP-derived numbers by the MCP layer,
   always labeled with their source, never written into metrics_daily.

Day attribution for sleep/workouts follows D25 (noon-to-noon, credited to
the wake-up date) using each record's own embedded UTC offset — Apple
stamps every record with the wearer's local offset at the time, so a
record made while traveling lands on the local day it actually happened.

export.xml is large (can be multiple GB) — parsed with `iterparse` and
elements are cleared as they're consumed to keep memory bounded. Heart-rate
records are the bulk of a full export, so they're bounded by --hr-since
(cheap string-prefix date check before any datetime parsing).

Usage:
    python -m whoop_local.history_import.apple_health --export-xml /path/to/export.xml
    # optionally: --hr-since 2037-06-20   (fictional example; default: last 45 days)
"""

import argparse
import logging
import statistics
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from whoop_local.config import DEFAULT_DB_PATH
from whoop_local.db.schema import init_db

log = logging.getLogger("whoop_local.history_import.apple_health")

RECORD_TYPE_RESTING_HR = "HKQuantityTypeIdentifierRestingHeartRate"
RECORD_TYPE_HRV_SDNN = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
RECORD_TYPE_SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
RECORD_TYPE_HEART_RATE = "HKQuantityTypeIdentifierHeartRate"
SOURCE = "apple_health_export"

HR_SINCE_DEFAULT_DAYS = 45

_SLEEP_VALUE_PREFIX = "HKCategoryValueSleepAnalysis"
_SLEEP_STAGE_MAP = {
    "InBed": "inbed",
    "Asleep": "asleep_unspecified",  # pre-watchOS-9 exports have no stage detail
    "AsleepUnspecified": "asleep_unspecified",
    "AsleepCore": "asleep_core",
    "AsleepDeep": "asleep_deep",
    "AsleepREM": "asleep_rem",
    "Awake": "awake",
}
_WORKOUT_ACTIVITY_PREFIX = "HKWorkoutActivityType"


def _parse_dt(apple_dt: str) -> datetime:
    """Apple's date strings carry their own local UTC offset
    (e.g. the fictional fixture '2042-03-17 22:46:44 -0400')."""
    return datetime.strptime(apple_dt, "%Y-%m-%d %H:%M:%S %z")


def _local_date(start_date: str):
    """The wearer's local calendar date at that moment (offset is embedded)."""
    return _parse_dt(start_date).date()


def _attributed_date(end_dt: datetime) -> str:
    """D25 day attribution: the noon-to-noon window a segment's END falls in.
    Ending before local noon -> that calendar date (overnight sleep is
    credited to the wake-up date); from noon onward -> the next date's window."""
    d = end_dt.date()
    if end_dt.hour >= 12:
        d = d + timedelta(days=1)
    return d.isoformat()


def _sleep_stage(value: str) -> str:
    if value.startswith(_SLEEP_VALUE_PREFIX):
        suffix = value[len(_SLEEP_VALUE_PREFIX) :]
        return _SLEEP_STAGE_MAP.get(suffix, suffix.lower())
    return value.lower()


@dataclass
class ParsedExport:
    daily: dict = field(
        default_factory=lambda: defaultdict(lambda: {"rhr_bpm": [], "hrv_sdnn_ms": []})
    )
    sleep_segments: list = field(
        default_factory=list
    )  # (date, start_ms, end_ms, stage, source_name)
    workouts: list = field(
        default_factory=list
    )  # (date, start_ms, end_ms, activity, duration_min, source_name)
    hr_samples: list = field(default_factory=list)  # (ts_ms, bpm, source_name)


def parse_export(export_xml_path: str, hr_since_date_iso: str | None = None) -> ParsedExport:
    """Single streaming pass over export.xml. HR samples are only collected
    from `hr_since_date_iso` (YYYY-MM-DD) onward; None skips them entirely."""
    out = ParsedExport()

    context = ET.iterparse(export_xml_path, events=("start",))
    for _event, elem in context:
        if elem.tag == "Workout":
            activity = elem.get("workoutActivityType") or ""
            start_date = elem.get("startDate")
            end_date = elem.get("endDate")
            duration = elem.get("duration")
            source_name = elem.get("sourceName")
            elem.clear()
            if not start_date or not end_date:
                continue
            start_dt, end_dt = _parse_dt(start_date), _parse_dt(end_date)
            if activity.startswith(_WORKOUT_ACTIVITY_PREFIX):
                activity = activity[len(_WORKOUT_ACTIVITY_PREFIX) :]
            out.workouts.append(
                (
                    _attributed_date(end_dt),
                    int(start_dt.timestamp() * 1000),
                    int(end_dt.timestamp() * 1000),
                    activity,
                    float(duration) if duration else None,
                    source_name,
                )
            )
            continue

        if elem.tag != "Record":
            continue

        record_type = elem.get("type")

        if record_type == RECORD_TYPE_SLEEP:
            value = elem.get("value")
            start_date = elem.get("startDate")
            end_date = elem.get("endDate")
            source_name = elem.get("sourceName")
            elem.clear()
            if not value or not start_date or not end_date:
                continue
            start_dt, end_dt = _parse_dt(start_date), _parse_dt(end_date)
            out.sleep_segments.append(
                (
                    _attributed_date(end_dt),
                    int(start_dt.timestamp() * 1000),
                    int(end_dt.timestamp() * 1000),
                    _sleep_stage(value),
                    source_name,
                )
            )
            continue

        if record_type == RECORD_TYPE_HEART_RATE:
            if hr_since_date_iso is None:
                elem.clear()
                continue
            start_date = elem.get("startDate")
            # Cheap lexicographic date-prefix check before any strptime — HR
            # records are the bulk of a multi-GB export.
            if not start_date or start_date[:10] < hr_since_date_iso:
                elem.clear()
                continue
            value = elem.get("value")
            source_name = elem.get("sourceName")
            elem.clear()
            if value is None:
                continue
            out.hr_samples.append(
                (
                    int(_parse_dt(start_date).timestamp() * 1000),
                    float(value),
                    source_name,
                )
            )
            continue

        if record_type not in (RECORD_TYPE_RESTING_HR, RECORD_TYPE_HRV_SDNN):
            elem.clear()
            continue

        value = elem.get("value")
        start_date = elem.get("startDate")
        elem.clear()
        if value is None or start_date is None:
            continue

        day = _local_date(start_date)
        if record_type == RECORD_TYPE_RESTING_HR:
            out.daily[day]["rhr_bpm"].append(float(value))
        else:
            out.daily[day]["hrv_sdnn_ms"].append(float(value))

    return out


def parse_daily_values(export_xml_path: str) -> dict:
    """Back-compat wrapper: just the {date: {"rhr_bpm": [...], "hrv_sdnn_ms": [...]}} part."""
    return parse_export(export_xml_path).daily


def import_into_db(conn, export_xml_path: str, hr_since_date_iso: str | None = None) -> dict:
    """Parses export_xml_path and writes all four imported_* tables.
    Idempotent: daily metrics upsert on date; segments/workouts/HR insert-or-ignore
    on their natural keys. Returns per-table row counts written this run."""
    parsed = parse_export(export_xml_path, hr_since_date_iso=hr_since_date_iso)
    imported_at = int(time.time() * 1000)
    counts = {"daily_metrics": 0, "sleep_segments": 0, "workouts": 0, "hr_samples": 0}

    for day, values in parsed.daily.items():
        rhr = statistics.median(values["rhr_bpm"]) if values["rhr_bpm"] else None
        hrv_sdnn = statistics.median(values["hrv_sdnn_ms"]) if values["hrv_sdnn_ms"] else None
        if rhr is None and hrv_sdnn is None:
            continue
        conn.execute(
            "INSERT INTO imported_daily_metrics (date, rhr_bpm, hrv_sdnn_ms, source, imported_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET rhr_bpm=excluded.rhr_bpm, "
            "hrv_sdnn_ms=excluded.hrv_sdnn_ms, source=excluded.source, imported_at=excluded.imported_at",
            (day.isoformat(), rhr, hrv_sdnn, SOURCE, imported_at),
        )
        counts["daily_metrics"] += 1

    for date_str, start_ms, end_ms, stage, source_name in parsed.sleep_segments:
        cur = conn.execute(
            "INSERT OR IGNORE INTO imported_sleep "
            "(date, start_utc, end_utc, stage, source_name, source, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date_str, start_ms, end_ms, stage, source_name, SOURCE, imported_at),
        )
        counts["sleep_segments"] += cur.rowcount if cur.rowcount > 0 else 0

    for date_str, start_ms, end_ms, activity, duration_min, source_name in parsed.workouts:
        cur = conn.execute(
            "INSERT OR IGNORE INTO imported_workouts "
            "(date, start_utc, end_utc, activity, duration_min, source_name, source, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (date_str, start_ms, end_ms, activity, duration_min, source_name, SOURCE, imported_at),
        )
        counts["workouts"] += cur.rowcount if cur.rowcount > 0 else 0

    for ts_ms, bpm, source_name in parsed.hr_samples:
        cur = conn.execute(
            "INSERT OR IGNORE INTO imported_hr_samples "
            "(ts_utc, hr_bpm, source_name, source, imported_at) VALUES (?, ?, ?, ?, ?)",
            (ts_ms, bpm, source_name, SOURCE, imported_at),
        )
        counts["hr_samples"] += cur.rowcount if cur.rowcount > 0 else 0

    conn.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-xml", required=True, help="path to Apple Health's export.xml")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument(
        "--hr-since",
        default=(datetime.now() - timedelta(days=HR_SINCE_DEFAULT_DAYS)).strftime("%Y-%m-%d"),
        help="YYYY-MM-DD cutoff for per-sample heart-rate import (millions of rows "
        "exist in a full export); 'none' skips HR samples entirely",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    hr_since = None if args.hr_since.lower() == "none" else args.hr_since
    conn = init_db(args.db)
    started = time.monotonic()
    counts = import_into_db(conn, args.export_xml, hr_since_date_iso=hr_since)
    log.info(
        "Imported from %s in %.1fs: %d daily row(s), %d sleep segment(s), "
        "%d workout(s), %d HR sample(s) (hr-since=%s)",
        args.export_xml,
        time.monotonic() - started,
        counts["daily_metrics"],
        counts["sleep_segments"],
        counts["workouts"],
        counts["hr_samples"],
        hr_since,
    )


if __name__ == "__main__":
    main()
