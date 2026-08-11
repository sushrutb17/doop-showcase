"""Create the minimal isolated synthetic database used only for showcase filming.

This is intentionally not a general fixture framework or product demo. It
writes CRC-valid synthetic realtime packets, parses them through the production
protocol functions, computes metrics through ``compute_for_date``, seeds one
synthetic plan, and records two prior-week completions for the two relay shots.
"""

from __future__ import annotations

import argparse
import json
import zlib
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from whoop_local.capture.proprietary_protocol import (
    CHAR_DATA_FROM_STRAP,
    FRAME_START,
    PACKET_TYPE_REALTIME_DATA,
    crc8,
    parse_frame,
    parse_realtime_hr,
    parse_realtime_rr,
)
from whoop_local.compute.compute_daily import compute_for_date
from whoop_local.compute.config import MetricsConfig
from whoop_local.db.schema import init_db
from whoop_local.history_import.strong import normalize_exercise_name
from whoop_local.manual_logging.service import confirm_draft, prepare_draft
from whoop_local.mcp_server.queries import (
    get_telegram_training_guidance,
    get_telegram_weekly_review,
)
from whoop_local.plans.synthetic_foundation import (
    WORKOUTS,
    seed_synthetic_foundation_plan,
)

UTC_ZONE = ZoneInfo("UTC")
DEFAULT_FILM_DATE = date(2037, 7, 6)
SYNTHETIC_METRICS_CONFIG = MetricsConfig(
    strain_hrmax_intercept=190.0,
    strain_hrmax_age_coefficient=0.0,
)


def _frame(hr_bpm: int, rr_ms: int, counter: int) -> bytes:
    payload = bytearray(20)
    payload[0] = PACKET_TYPE_REALTIME_DATA
    payload[1] = 2
    payload[2:6] = counter.to_bytes(4, "little")
    payload[8] = hr_bpm
    payload[9] = 4
    for index, value in enumerate((rr_ms, rr_ms + 10, rr_ms, rr_ms + 10)):
        offset = 10 + index * 2
        payload[offset : offset + 2] = value.to_bytes(2, "little")
    payload[18] = 1
    payload[19] = 1
    declared_len = len(payload) + 4
    header = bytes([FRAME_START]) + declared_len.to_bytes(2, "little")
    header += bytes([crc8(header[1:3])])
    return header + payload + (zlib.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "little")


def _insert_synthetic_packets(conn, metric_day: date, day_index: int) -> None:
    start = datetime.combine(metric_day, time(1, 0), tzinfo=UTC)
    for sample_index in range(49):
        timestamp = int((start + timedelta(minutes=5 * sample_index)).timestamp() * 1000)
        resting_hr = 52 + day_index % 3
        hr_bpm = resting_hr if sample_index <= 36 else 118 + day_index % 5
        rr_ms = 800 + day_index * 3 + (10 if sample_index % 2 else -10)
        raw = _frame(hr_bpm, rr_ms, day_index * 100 + sample_index)
        parsed = parse_frame(raw)
        assert parsed.header_crc_valid and parsed.payload_crc_valid
        parsed_hr = parse_realtime_hr(parsed.payload)
        parsed_rr = parse_realtime_rr(parsed.payload)
        assert parsed_hr is not None and parsed_rr
        conn.execute(
            "INSERT INTO raw_packets (ts_utc, source_uuid, source_host, origin, payload_hex) "
            "VALUES (?, ?, 'showcase', 'synthetic', ?)",
            (timestamp, CHAR_DATA_FROM_STRAP, raw.hex()),
        )
        conn.execute(
            "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) "
            "VALUES (?, ?, 'showcase', 'whoop')",
            (timestamp, parsed_hr),
        )
        for rr_index, value in enumerate(parsed_rr):
            conn.execute(
                "INSERT INTO rr_intervals "
                "(ts_utc, rr_ms, source_host, source, rr_index) "
                "VALUES (?, ?, 'showcase', 'whoop', ?)",
                (timestamp, value, rr_index),
            )
    conn.commit()


def _seed_plan(conn, film_date: date) -> None:
    names = {exercise[0] for workout in WORKOUTS for exercise in workout["exercises"]}
    for index, name in enumerate(sorted(names), start=1):
        conn.execute(
            "INSERT INTO exercise_catalog (canonical_name, normalized_name, created_at) "
            "VALUES (?, ?, ?)",
            (name, normalize_exercise_name(name), index),
        )
    conn.commit()
    seed_synthetic_foundation_plan(conn)

    week_start = film_date - timedelta(days=film_date.weekday())
    conn.execute(
        "UPDATE training_plans SET starts_on=? WHERE status='active'",
        ((week_start - timedelta(days=7)).isoformat(),),
    )
    conn.commit()


def _seed_prior_week(conn, film_date: date) -> date:
    current_monday = film_date - timedelta(days=film_date.weekday())
    prior_monday = current_monday - timedelta(days=7)
    workout_days = dict(
        conn.execute(
            "SELECT name, weekday FROM plan_workouts WHERE name IN ('Aerobic base', 'Foundation B')"
        ).fetchall()
    )
    aerobic_day = prior_monday + timedelta(days=workout_days["Aerobic base"])
    foundation_b_day = prior_monday + timedelta(days=workout_days["Foundation B"])

    aerobic_start = int(datetime.combine(aerobic_day, time(18, 0), tzinfo=UTC).timestamp() * 1000)
    conn.execute(
        "INSERT INTO imported_workouts "
        "(date, start_utc, end_utc, activity, duration_min, source_name, source, imported_at) "
        "VALUES (?, ?, ?, 'Aerobic base', 35, 'Synthetic observer', 'synthetic', ?)",
        (aerobic_day.isoformat(), aerobic_start, aerobic_start + 2_100_000, 1),
    )
    payload = {
        "name": "Foundation B",
        "started_at": datetime.combine(foundation_b_day, time(10, 0), tzinfo=UTC).isoformat(),
        "ended_at": datetime.combine(foundation_b_day, time(10, 55), tzinfo=UTC).isoformat(),
        "notes": "Synthetic confirmed showcase session.",
        "exercises": [
            {
                "name": "Leg Press",
                "sets": [
                    {"weight": 60, "weight_unit": "kg", "reps": 10, "rir": 2} for _ in range(3)
                ],
            }
        ],
    }
    draft = prepare_draft(conn, payload)
    confirm_draft(conn, draft["draft_id"])
    conn.commit()
    return prior_monday


def create_film_seed(output: Path, film_date: date) -> dict:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing seed: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(output)
    try:
        for day_index in range(8):
            metric_day = film_date - timedelta(days=7 - day_index)
            _insert_synthetic_packets(conn, metric_day, day_index)
            compute_for_date(
                conn,
                metric_day,
                config=SYNTHETIC_METRICS_CONFIG,
                tz=UTC_ZONE,
            )
        _seed_plan(conn, film_date)
        week_start = _seed_prior_week(conn, film_date)
        guidance = get_telegram_training_guidance(conn, film_date.isoformat())
        weekly = get_telegram_weekly_review(
            conn,
            week_start.isoformat(),
            as_of_date=film_date,
        )
        metrics = conn.execute(
            "SELECT recovery_proxy, strain_proxy, coverage_pct, sleep_minutes, provisional "
            "FROM metrics_daily WHERE date=?",
            (film_date.isoformat(),),
        ).fetchone()
        assert metrics is not None
        summary = {
            "film_date": film_date.isoformat(),
            "week_start": week_start.isoformat(),
            "raw_packets": conn.execute("SELECT COUNT(*) FROM raw_packets").fetchone()[0],
            "metrics_days": conn.execute("SELECT COUNT(*) FROM metrics_daily").fetchone()[0],
            "confirmed_sessions": conn.execute(
                "SELECT COUNT(*) FROM workout_sessions WHERE source='manual'"
            ).fetchone()[0],
            "recovery_computed": metrics[0] is not None,
            "strain_proxy": metrics[1],
            "coverage_pct": metrics[2],
            "sleep_minutes": metrics[3],
            "provisional": bool(metrics[4]),
            "guidance_relay": guidance,
            "weekly_relay": weekly,
        }
        return summary
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--film-date",
        type=date.fromisoformat,
        default=DEFAULT_FILM_DATE,
        help=f"fictional metric date (default: {DEFAULT_FILM_DATE.isoformat()})",
    )
    args = parser.parse_args()
    print(json.dumps(create_film_seed(args.output, args.film_date), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
