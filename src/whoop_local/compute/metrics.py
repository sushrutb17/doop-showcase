"""HRV / recovery / strain / sleep proxies. Contract defined in METRICS.md —
these are independent proxies, never presented as equivalent to WHOOP's
proprietary scores (D9).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

import numpy as np
import pandas as pd

from whoop_local.compute.config import DEFAULT_CONFIG, MetricsConfig


@dataclass
class RrSample:
    ts_utc: int
    rr_ms: float


def clean_rr_intervals(
    samples: list[RrSample], config: MetricsConfig = DEFAULT_CONFIG
) -> list[RrSample]:
    """Range filter + successive-difference ectopic filter. METRICS.md 'RR cleaning'."""
    in_range = [s for s in samples if config.rr_min_ms <= s.rr_ms <= config.rr_max_ms]

    cleaned: list[RrSample] = []
    prev_accepted: float | None = None
    for s in in_range:
        if (
            prev_accepted is None
            or abs(s.rr_ms - prev_accepted) / prev_accepted <= config.rr_ectopic_pct
        ):
            cleaned.append(s)
            prev_accepted = s.rr_ms
    return cleaned


def has_sufficient_clean_window(
    samples: list[RrSample], config: MetricsConfig = DEFAULT_CONFIG
) -> bool:
    if len(samples) < 2:
        return False
    span_ms = samples[-1].ts_utc - samples[0].ts_utc
    return span_ms >= config.min_clean_rr_window_ms


def rmssd(samples: list[RrSample]) -> float | None:
    """RMSSD in ms over cleaned RR intervals. Primary HRV metric."""
    if len(samples) < 2:
        return None
    diffs = np.diff([s.rr_ms for s in samples])
    return float(np.sqrt(np.mean(diffs**2)))


def sdnn(samples: list[RrSample]) -> float | None:
    """SDNN in ms over cleaned RR intervals."""
    if len(samples) < 2:
        return None
    return float(statistics.stdev(s.rr_ms for s in samples))


def pnn50(samples: list[RrSample]) -> float | None:
    """Proportion of successive RR differences exceeding 50ms."""
    if len(samples) < 2:
        return None
    diffs = np.abs(np.diff([s.rr_ms for s in samples]))
    return float(np.mean(diffs > 50.0))


def resting_heart_rate(
    hr_series: pd.Series, config: MetricsConfig = DEFAULT_CONFIG
) -> float | None:
    """Low percentile of rolling N-min medians of hr_bpm. METRICS.md 'Resting heart rate'.

    Parameters
    ----------
    hr_series:
        pandas Series of hr_bpm values indexed by a DatetimeIndex (UTC).
    """
    if hr_series.empty:
        return None
    rolling_medians = (
        hr_series.resample(f"{config.rhr_rolling_window_minutes}min").median().dropna()
    )
    if rolling_medians.empty:
        return None
    return float(np.percentile(rolling_medians, config.rhr_percentile))


def rolling_baseline(
    values: list[float], config: MetricsConfig = DEFAULT_CONFIG
) -> tuple[float | None, float | None, bool]:
    """Median (+ spread) over a trailing window. Returns (baseline, spread, provisional).

    D26: `baseline` is None until `baseline_provisional_min_days` history
    exists. Between that and `baseline_min_days`, a real median is returned
    with `provisional=True` — softens the cold start without fabricating
    full confidence. `provisional` drops to False once `baseline_min_days`
    history exists.

    D35: `spread` (sample stdev of the same window) is only computed once
    the baseline is fully established (not provisional) — a 3-4 day sample's
    stdev is too noisy to normalize against and would risk wilder swings
    than the fixed-ratio fallback it's meant to improve on. `spread` is
    `None` while provisional (or before any baseline exists).
    """
    if len(values) < config.baseline_provisional_min_days:
        return None, None, False
    window = values[-config.baseline_window_days :]
    baseline = float(statistics.median(window))
    provisional = len(values) < config.baseline_min_days
    spread = None if provisional else float(statistics.stdev(window))
    return baseline, spread, provisional


def _score_vs_baseline(
    today: float,
    baseline: float,
    higher_is_better: bool,
    spread: float | None = None,
    config: MetricsConfig = DEFAULT_CONFIG,
) -> float:
    """Map today's value to a 0-100 score, 50 == at baseline.

    D35: when a trustworthy `spread` (this person's own rolling stdev) is
    available, score by z-score — today's deviation from baseline in units
    of their own historical day-to-day variability — scaled by
    `baseline_normalization_points_per_sd` points per stdev, clamped to
    [0, 100]. Falls back to D17's original fixed ratio-to-baseline scaling
    (ratio 1.0 -> 50) when `spread` is `None` or too near zero to divide by
    safely — this keeps the cold-start/provisional behavior unchanged.
    """
    if baseline <= 0:
        return 50.0
    if spread is not None and spread > config.baseline_normalization_min_spread:
        z = (today - baseline) / spread
        if not higher_is_better:
            z = -z
        score = 50.0 + z * config.baseline_normalization_points_per_sd
        return max(0.0, min(100.0, score))

    ratio = today / baseline
    if not higher_is_better:
        ratio = 1.0 / ratio if ratio > 0 else float("inf")
    score = 50.0 * ratio
    return max(0.0, min(100.0, score))


def recovery_proxy(
    hrv_today: float,
    hrv_baseline: float,
    rhr_today: float,
    rhr_baseline: float,
    config: MetricsConfig = DEFAULT_CONFIG,
    hrv_spread: float | None = None,
    rhr_spread: float | None = None,
) -> float:
    """0-100 recovery proxy. METRICS.md 'Recovery proxy' — NOT WHOOP's score (D9)."""
    hrv_score = _score_vs_baseline(
        hrv_today, hrv_baseline, higher_is_better=True, spread=hrv_spread, config=config
    )
    rhr_score = _score_vs_baseline(
        rhr_today, rhr_baseline, higher_is_better=False, spread=rhr_spread, config=config
    )
    combined = config.recovery_w_hrv * hrv_score + config.recovery_w_rhr * rhr_score
    return max(0.0, min(100.0, combined))


@dataclass
class StrainResult:
    strain_proxy: float
    time_in_zones_min: dict[str, int]


def strain_proxy(
    hr_series: pd.Series,
    hr_rest: float,
    hr_max: float,
    config: MetricsConfig = DEFAULT_CONFIG,
) -> StrainResult | None:
    """TRIMP-style cardiovascular load proxy. METRICS.md 'Strain proxy' — a
    custom scale, explicitly NOT WHOOP's 0-21 (D9).

    v1 (D32): classic Banister per-minute heart-rate-reserve weighting
    (`frac * coef * e^(exp * frac)`), summed across the day's 1-minute
    average HR. Starting point per METRICS.md ("start... validate... then
    tune") — same spirit as D17's recovery formula.
    """
    if hr_series.empty or hr_max <= hr_rest:
        return None

    minute_hr = hr_series.resample("1min").mean().dropna()
    if minute_hr.empty:
        return None

    hrr = hr_max - hr_rest
    fractions = ((minute_hr - hr_rest) / hrr).clip(lower=0.0, upper=1.0)

    trimp_total = float(
        (
            fractions
            * config.strain_trimp_coefficient
            * np.exp(config.strain_trimp_exponent * fractions)
        ).sum()
    )

    zone_edges = list(config.strain_zone_boundaries) + [1.0 + 1e-9]
    time_in_zones_min: dict[str, int] = {}
    for i in range(len(zone_edges) - 1):
        low, high = zone_edges[i], zone_edges[i + 1]
        time_in_zones_min[f"z{i + 1}"] = int(((fractions >= low) & (fractions < high)).sum())

    return StrainResult(strain_proxy=trimp_total, time_in_zones_min=time_in_zones_min)


@dataclass
class SleepWindow:
    start_utc: int
    end_utc: int
    sleep_minutes: int


def detect_sleep_windows(
    hr_series: pd.Series, rhr_baseline: float, config: MetricsConfig = DEFAULT_CONFIG
) -> list[SleepWindow]:
    """Coarse sleep detection: low + stable HR block >= threshold hours, bridging
    brief interruptions (movement/arousals) rather than requiring one perfectly
    unbroken stretch. METRICS.md 'Sleep detection (v1 - coarse)'; interruption
    Short interruptions are bridged according to configuration so one
    candidate window is not split by brief threshold excursions.
    No accelerometer input yet.
    """
    if hr_series.empty:
        return []

    window = f"{config.sleep_rolling_window_minutes}min"
    resampled = hr_series.resample(window).median().dropna()
    rolling_std = hr_series.resample(window).std().reindex(resampled.index)

    hr_threshold = rhr_baseline * config.sleep_hr_threshold_factor
    is_candidate = (resampled <= hr_threshold) & (
        rolling_std.fillna(0) <= config.sleep_hr_stability_std_bpm
    )

    raw_runs: list[tuple] = []
    block_start = None
    prev_ts = None
    for ts, candidate in is_candidate.items():
        if candidate:
            if block_start is None:
                block_start = ts
        else:
            if block_start is not None:
                raw_runs.append((block_start, prev_ts))
                block_start = None
        prev_ts = ts
    if block_start is not None:
        raw_runs.append((block_start, prev_ts))

    if not raw_runs:
        return []

    max_gap = pd.Timedelta(minutes=config.sleep_max_interruption_minutes)
    merged_runs = [raw_runs[0]]
    for start, end in raw_runs[1:]:
        last_start, last_end = merged_runs[-1]
        if start - last_end <= max_gap:
            merged_runs[-1] = (last_start, end)
        else:
            merged_runs.append((start, end))

    windows = [_maybe_window(start, end, config) for start, end in merged_runs]
    return [w for w in windows if w is not None]


def coverage_fraction(
    gaps: list[tuple[int, int | None]],
    window_start_ms: int,
    window_end_ms: int,
    now_ms: int | None = None,
    recovered_timestamps_ms: list[int] | None = None,
) -> float:
    """Fraction of [window_start_ms, window_end_ms) covered by live or history.

    Starts with 1 - (recorded live-gap overlap / window duration), then restores
    the unique one-second samples recovered from onboard history (D69). This
    lets reporting phrase genuinely missing sleep/recovery data as a floor while
    treating a successfully backfilled BLE gap as covered. A still-open gap
    (`gap_end is None`) is treated as ongoing through `now_ms`.
    """
    window_duration = window_end_ms - window_start_ms
    if window_duration <= 0:
        return 1.0
    now_ms = window_end_ms if now_ms is None else now_ms

    total_gap_ms = 0
    effective_gaps: list[tuple[int, int]] = []
    for gap_start, gap_end in gaps:
        effective_end = now_ms if gap_end is None else gap_end
        clipped_start = max(gap_start, window_start_ms)
        clipped_end = min(effective_end, window_end_ms)
        overlap = clipped_end - clipped_start
        total_gap_ms += max(0, overlap)
        if overlap > 0:
            effective_gaps.append((clipped_start, clipped_end))

    # D69: a BLE disconnect is no longer missing coverage once the same seconds
    # have been recovered from the band's onboard history. Count unique 1 Hz
    # historical timestamps that fall inside a recorded gap, capped by the gap
    # total so replays/overlap can never inflate coverage above 100%.
    recovered_seconds = {
        ts // 1000
        for ts in (recovered_timestamps_ms or [])
        if any(start <= ts < end for start, end in effective_gaps)
    }
    total_gap_ms = max(0, total_gap_ms - min(total_gap_ms, len(recovered_seconds) * 1000))

    coverage = 1.0 - (total_gap_ms / window_duration)
    return max(0.0, min(1.0, coverage))


def _maybe_window(start, end, config: MetricsConfig) -> SleepWindow | None:
    duration_hours = (end - start).total_seconds() / 3600.0
    if duration_hours < config.sleep_min_duration_hours:
        return None
    return SleepWindow(
        start_utc=int(start.timestamp() * 1000),
        end_utc=int(end.timestamp() * 1000),
        sleep_minutes=int(duration_hours * 60),
    )
