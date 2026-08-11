"""Compute a `metrics_daily` row for one *local* day from raw samples.

Day boundary (D25): a metrics day D is local noon on D-1 through local noon
on D — overnight sleep is credited to the wake-up date, not split across UTC
midnight. See ``METRICS.md`` under "Day attribution."

Usage:
    python -m whoop_local.compute.compute_daily --date 2037-07-01
    python -m whoop_local.compute.compute_daily          # defaults to the most
                                                          # recently completed
                                                          # local noon-to-noon window
    python -m whoop_local.compute.compute_daily --catch-up-days 3
                                                          # also recompute the
                                                          # prior 2 days (idempotent)
"""

import argparse
import json
import logging
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from whoop_local.compute.config import DEFAULT_CONFIG, MetricsConfig, strain_hrmax_bpm
from whoop_local.compute.metrics import (
    RrSample,
    clean_rr_intervals,
    coverage_fraction,
    detect_sleep_windows,
    has_sufficient_clean_window,
    recovery_proxy,
    resting_heart_rate,
    rmssd,
    rolling_baseline,
    sdnn,
    strain_proxy,
)
from whoop_local.config import (
    DEFAULT_DB_PATH,
    default_compute_date,
    local_day_window_utc_ms,
    local_timezone,
)
from whoop_local.db.schema import init_db

log = logging.getLogger("whoop_local.compute_daily")


def _load_hr_series(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> pd.Series:
    rows = conn.execute(
        "SELECT ts_utc, hr_bpm FROM hr_samples WHERE ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc",
        (start_ms, end_ms),
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True)
    return pd.Series([r[1] for r in rows], index=idx)


def _load_rr_samples(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> list[RrSample]:
    rows = conn.execute(
        "SELECT ts_utc, rr_ms FROM rr_intervals WHERE ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc",
        (start_ms, end_ms),
    ).fetchall()
    return [RrSample(ts_utc=r[0], rr_ms=r[1]) for r in rows]


def _load_gaps(
    conn: sqlite3.Connection, start_ms: int, end_ms: int
) -> list[tuple[int, int | None]]:
    """Gaps overlapping [start_ms, end_ms): starts before the window ends,
    and either still open or ends after the window starts."""
    rows = conn.execute(
        "SELECT gap_start_utc, gap_end_utc FROM capture_gaps "
        "WHERE gap_start_utc < ? AND (gap_end_utc IS NULL OR gap_end_utc > ?)",
        (end_ms, start_ms),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _load_recovered_history_timestamps(
    conn: sqlite3.Connection, start_ms: int, end_ms: int
) -> list[int]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT device_ts_utc FROM hr_samples "
            "WHERE device_ts_utc IS NOT NULL AND device_ts_utc >= ? AND device_ts_utc < ?",
            (start_ms, end_ms),
        )
    ]


def _load_history(
    conn: sqlite3.Connection, column: str, before_date: str, config: MetricsConfig
) -> list[float]:
    """History for the rolling baseline, oldest -> newest (rolling_baseline
    takes a trailing window off this end).

    D33: for `rhr_bpm`, if real WHOOP-derived history hasn't yet reached the
    D26 provisional floor, fills the remainder from `imported_daily_metrics`
    (e.g. an Apple Health export). Once real recent history is sufficient on
    its own, imports are **not** blended in -- otherwise a months-old import
    could quietly outweigh (via the trailing-window median) a baseline that
    already has enough current real data to stand on its own, silently
    shifting today's answer without it being obvious. `rmssd_ms`/`sdnn_ms`
    never use imports: Apple's HRV metric is SDNN-based, not our RMSSD, and
    blending them would misrepresent two different methodologies as
    equivalent (D9).
    """
    limit = config.baseline_window_days
    real_rows = conn.execute(
        f"SELECT date, {column} FROM metrics_daily WHERE {column} IS NOT NULL AND date < ? "
        f"ORDER BY date DESC LIMIT ?",
        (before_date, limit),
    ).fetchall()

    if column != "rhr_bpm" or len(real_rows) >= config.baseline_provisional_min_days:
        return [r[1] for r in reversed(real_rows)]

    imported_rows = conn.execute(
        "SELECT date, rhr_bpm FROM imported_daily_metrics WHERE rhr_bpm IS NOT NULL AND date < ? "
        "AND date NOT IN (SELECT date FROM metrics_daily WHERE rhr_bpm IS NOT NULL) "
        "ORDER BY date DESC LIMIT ?",
        (before_date, limit - len(real_rows)),
    ).fetchall()
    combined = sorted(real_rows + imported_rows)  # 'YYYY-MM-DD' strings sort chronologically
    return [r[1] for r in combined]


def compute_for_date(
    conn: sqlite3.Connection,
    day: date,
    config: MetricsConfig = DEFAULT_CONFIG,
    tz: ZoneInfo | None = None,
) -> dict:
    tz = tz or local_timezone()
    date_str = day.isoformat()
    start_ms, end_ms = local_day_window_utc_ms(day, tz)

    hr_series = _load_hr_series(conn, start_ms, end_ms)
    rr_samples = clean_rr_intervals(_load_rr_samples(conn, start_ms, end_ms), config)

    # D26: what fraction of this day's window actually had live capture, so
    # reporting layers can phrase sleep/recovery as a floor when it's gappy.
    gaps = _load_gaps(conn, start_ms, end_ms)
    recovered_timestamps = _load_recovered_history_timestamps(conn, start_ms, end_ms)
    coverage_pct = coverage_fraction(
        gaps,
        start_ms,
        end_ms,
        now_ms=int(time.time() * 1000),
        recovered_timestamps_ms=recovered_timestamps,
    )

    rhr_bpm = resting_heart_rate(hr_series, config)

    rmssd_ms = sdnn_ms = None
    if has_sufficient_clean_window(rr_samples, config):
        rmssd_ms = rmssd(rr_samples)
        sdnn_ms = sdnn(rr_samples)
    else:
        log.info(
            "Insufficient clean RR window for %s (%d samples) — HRV withheld",
            date_str,
            len(rr_samples),
        )

    rmssd_history = _load_history(conn, "rmssd_ms", date_str, config)
    rhr_history = _load_history(conn, "rhr_bpm", date_str, config)
    hrv_baseline_ms, hrv_spread_ms, hrv_provisional = rolling_baseline(rmssd_history, config)
    rhr_baseline, rhr_spread, rhr_provisional = rolling_baseline(rhr_history, config)

    recovery = None
    provisional = False
    if (
        rmssd_ms is not None
        and hrv_baseline_ms is not None
        and rhr_bpm is not None
        and rhr_baseline is not None
    ):
        recovery = recovery_proxy(
            rmssd_ms,
            hrv_baseline_ms,
            rhr_bpm,
            rhr_baseline,
            config,
            hrv_spread=hrv_spread_ms,
            rhr_spread=rhr_spread,
        )
        # D26: if either baseline leg is still short of the full window, the
        # recovery number built on it is provisional too.
        provisional = hrv_provisional or rhr_provisional

    # Re-running for the same date must not duplicate sleep sessions — clear
    # this date's auto-detected sleep rows before recomputing.
    conn.execute(
        "DELETE FROM sessions WHERE type = 'sleep' AND source = 'auto' AND start_utc >= ? AND start_utc < ?",
        (start_ms, end_ms),
    )

    sleep_minutes = None
    sleep_baseline = rhr_baseline or rhr_bpm
    if sleep_baseline is not None:
        windows = detect_sleep_windows(hr_series, sleep_baseline, config)
        for window in windows:
            conn.execute(
                "INSERT INTO sessions (type, start_utc, end_utc, source) VALUES ('sleep', ?, ?, 'auto')",
                (window.start_utc, window.end_utc),
            )
        if windows:
            sleep_minutes = sum(w.sleep_minutes for w in windows)

    # D32: strain needs a resting HR and user-supplied age/DOB or explicit
    # HRmax. Missing athlete context withholds the proxy instead of silently
    # computing it for the synthetic person used by the original showcase.
    strain_result = None
    strain_hr_rest = rhr_bpm or rhr_baseline
    hr_max = strain_hrmax_bpm(day, config)
    if strain_hr_rest is not None and hr_max is not None:
        strain_result = strain_proxy(hr_series, strain_hr_rest, hr_max, config)

    row = {
        "date": date_str,
        "rmssd_ms": rmssd_ms,
        "sdnn_ms": sdnn_ms,
        "rhr_bpm": rhr_bpm,
        "hrv_baseline_ms": hrv_baseline_ms,
        "recovery_proxy": recovery,
        "strain_proxy": strain_result.strain_proxy if strain_result else None,
        "time_in_zones_json": json.dumps(strain_result.time_in_zones_min)
        if strain_result
        else None,
        "sleep_minutes": sleep_minutes,
        "computed_at": int(time.time() * 1000),
        "timezone": str(tz),
        "provisional": int(provisional),
        "coverage_pct": coverage_pct,
    }

    conn.execute(
        "INSERT INTO metrics_daily (date, rmssd_ms, sdnn_ms, rhr_bpm, hrv_baseline_ms, "
        "recovery_proxy, strain_proxy, time_in_zones_json, sleep_minutes, computed_at, timezone, "
        "provisional, coverage_pct) "
        "VALUES (:date, :rmssd_ms, :sdnn_ms, :rhr_bpm, :hrv_baseline_ms, "
        ":recovery_proxy, :strain_proxy, :time_in_zones_json, :sleep_minutes, :computed_at, :timezone, "
        ":provisional, :coverage_pct) "
        "ON CONFLICT(date) DO UPDATE SET "
        "rmssd_ms=excluded.rmssd_ms, sdnn_ms=excluded.sdnn_ms, rhr_bpm=excluded.rhr_bpm, "
        "hrv_baseline_ms=excluded.hrv_baseline_ms, recovery_proxy=excluded.recovery_proxy, "
        "strain_proxy=excluded.strain_proxy, time_in_zones_json=excluded.time_in_zones_json, "
        "sleep_minutes=excluded.sleep_minutes, "
        "computed_at=excluded.computed_at, timezone=excluded.timezone, provisional=excluded.provisional, "
        "coverage_pct=excluded.coverage_pct",
        row,
    )
    conn.commit()
    return row


def recompute_all_dates(
    conn: sqlite3.Connection, config: MetricsConfig = DEFAULT_CONFIG, tz: ZoneInfo | None = None
) -> list[dict]:
    """Recompute every local day that has any raw HR/RR data, and drop any
    stale `metrics_daily` rows outside that range (D25 migration: old rows
    were keyed by UTC calendar date under the pre-D25 convention, which can
    disagree with the local-day keys this recomputes)."""
    tz = tz or local_timezone()
    bounds = conn.execute(
        "SELECT MIN(ts_utc), MAX(ts_utc) FROM ("
        "SELECT ts_utc FROM hr_samples UNION ALL SELECT ts_utc FROM rr_intervals)"
    ).fetchone()
    if bounds[0] is None:
        return []

    first_local_day = datetime.fromtimestamp(bounds[0] / 1000, tz=UTC).astimezone(tz).date()
    last_local_day = datetime.fromtimestamp(bounds[1] / 1000, tz=UTC).astimezone(tz).date()

    days = []
    d = first_local_day
    while d <= last_local_day:
        days.append(d)
        d += timedelta(days=1)

    rows = [compute_for_date(conn, d, config, tz) for d in days]

    date_strs = [r["date"] for r in rows]
    placeholders = ",".join("?" * len(date_strs))
    conn.execute(f"DELETE FROM metrics_daily WHERE date NOT IN ({placeholders})", date_strs)
    conn.commit()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument(
        "--date", default=None, help="local date YYYY-MM-DD; defaults per --catch-up-days"
    )
    parser.add_argument(
        "--catch-up-days",
        type=int,
        default=1,
        help="when --date is omitted, also recompute this many prior local days "
        "(idempotent) for robustness against a missed scheduled run (default 1 = "
        "just the most recently completed window)",
    )
    parser.add_argument(
        "--recompute-all",
        action="store_true",
        help="recompute every local day that has raw data and drop stale metrics_daily "
        "rows outside that range (D25 migration helper)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = init_db(args.db)
    tz = local_timezone()

    if args.recompute_all:
        rows = recompute_all_dates(conn, tz=tz)
        for row in rows:
            log.info("metrics_daily[%s] = %s", row["date"], row)
        return

    if args.date:
        rows = [compute_for_date(conn, date.fromisoformat(args.date), tz=tz)]
    else:
        end_day = default_compute_date(tz)
        days = [end_day - timedelta(days=i) for i in range(args.catch_up_days)]
        rows = [compute_for_date(conn, d, tz=tz) for d in reversed(days)]

    for row in rows:
        log.info("metrics_daily[%s] = %s", row["date"], row)


if __name__ == "__main__":
    main()
