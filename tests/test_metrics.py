import statistics

import pandas as pd
import pytest

from whoop_local.compute.config import MetricsConfig
from whoop_local.compute.metrics import (
    RrSample,
    clean_rr_intervals,
    coverage_fraction,
    detect_sleep_windows,
    has_sufficient_clean_window,
    pnn50,
    recovery_proxy,
    resting_heart_rate,
    rmssd,
    rolling_baseline,
    sdnn,
    strain_proxy,
)


def _rr(ts_ms: int, rr_ms: float) -> RrSample:
    return RrSample(ts_utc=ts_ms, rr_ms=rr_ms)


def test_clean_rr_intervals_drops_out_of_range():
    samples = [_rr(0, 100), _rr(1000, 800), _rr(2000, 2500), _rr(3000, 850)]
    cleaned = clean_rr_intervals(samples)
    assert [s.rr_ms for s in cleaned] == [800, 850]


def test_clean_rr_intervals_drops_ectopic_spikes():
    # 800 -> 1200 is a 50% jump, should be dropped; 800 -> 820 is fine
    samples = [_rr(0, 800), _rr(1000, 1200), _rr(2000, 820), _rr(3000, 830)]
    cleaned = clean_rr_intervals(samples)
    assert [s.rr_ms for s in cleaned] == [800, 820, 830]


def test_has_sufficient_clean_window():
    config = MetricsConfig(min_clean_rr_window_ms=120_000)
    short = [_rr(0, 800), _rr(60_000, 810)]
    long = [_rr(0, 800), _rr(130_000, 810)]
    assert not has_sufficient_clean_window(short, config)
    assert has_sufficient_clean_window(long, config)


def test_rmssd_known_value():
    # RR intervals with constant successive difference of 100ms
    samples = [_rr(i * 1000, rr) for i, rr in enumerate([800, 900, 800, 900])]
    # diffs: 100, -100, 100 -> rmssd = sqrt(mean(100^2,100^2,100^2)) = 100
    assert rmssd(samples) == pytest.approx(100.0)


def test_rmssd_none_for_insufficient_data():
    assert rmssd([_rr(0, 800)]) is None
    assert rmssd([]) is None


def test_sdnn_known_value():
    samples = [_rr(i * 1000, rr) for i, rr in enumerate([800, 810, 820])]
    assert sdnn(samples) == pytest.approx(10.0)


def test_pnn50():
    # diffs: 60 (>50), 10 (<=50) -> pnn50 = 0.5
    samples = [_rr(0, 800), _rr(1000, 860), _rr(2000, 870)]
    assert pnn50(samples) == pytest.approx(0.5)


def test_resting_heart_rate_uses_low_percentile_of_rolling_medians():
    idx = pd.date_range("2037-07-01", periods=60, freq="1min", tz="UTC")
    # Mostly 60 bpm with a couple of high spikes that shouldn't dominate a low percentile
    values = [60] * 55 + [100, 100, 100, 100, 100]
    series = pd.Series(values, index=idx)
    rhr = resting_heart_rate(series, MetricsConfig(rhr_rolling_window_minutes=5, rhr_percentile=5))
    assert rhr == pytest.approx(60.0, abs=1.0)


def test_resting_heart_rate_empty_series():
    assert resting_heart_rate(pd.Series(dtype=float)) is None


def test_rolling_baseline_requires_minimum_provisional_history():
    config = MetricsConfig(
        baseline_min_days=7, baseline_provisional_min_days=3, baseline_window_days=30
    )
    assert rolling_baseline([50, 51], config) == (
        None,
        None,
        False,
    )  # only 2, below even provisional
    baseline, spread, provisional = rolling_baseline(
        [50, 51, 52, 53, 54, 55], config
    )  # 6: provisional
    assert (baseline, provisional) == (52.5, True)
    assert spread is None  # D35: too few days to trust a spread estimate yet
    baseline, spread, provisional = rolling_baseline(
        [50, 51, 52, 53, 54, 55, 56], config
    )  # 7: fully established
    assert (baseline, provisional) == (53, False)
    assert spread == pytest.approx(statistics.stdev([50, 51, 52, 53, 54, 55, 56]))


def test_recovery_proxy_at_baseline_is_neutral():
    score = recovery_proxy(hrv_today=50, hrv_baseline=50, rhr_today=55, rhr_baseline=55)
    assert score == pytest.approx(50.0)


def test_recovery_proxy_falls_back_to_ratio_scale_without_spread():
    # No spread passed (e.g. still-provisional baseline) -> D17's fixed ratio scale.
    score = recovery_proxy(hrv_today=60, hrv_baseline=50, rhr_today=55, rhr_baseline=55)
    assert score == pytest.approx(
        0.7 * 60.0 + 0.3 * 50.0
    )  # hrv ratio 1.2 -> 60, rhr at baseline -> 50


def test_recovery_proxy_uses_zscore_when_spread_available():
    config = MetricsConfig()
    # HRV exactly 1 stdev above baseline -> +points_per_sd over neutral 50.
    score = recovery_proxy(
        hrv_today=60,
        hrv_baseline=50,
        rhr_today=55,
        rhr_baseline=55,
        config=config,
        hrv_spread=10.0,
        rhr_spread=5.0,
    )
    expected_hrv_score = 50.0 + config.baseline_normalization_points_per_sd
    assert score == pytest.approx(
        config.recovery_w_hrv * expected_hrv_score + config.recovery_w_rhr * 50.0
    )


def test_recovery_proxy_same_absolute_gap_scores_differently_by_personal_spread():
    # Same +10ms HRV gap over baseline, but one person's history is much
    # noisier than the other's -- normalization should treat the tight-spread
    # person's +10ms as more significant than the wide-spread person's.
    tight_spread = recovery_proxy(
        hrv_today=60,
        hrv_baseline=50,
        rhr_today=55,
        rhr_baseline=55,
        hrv_spread=2.0,
        rhr_spread=5.0,
    )
    wide_spread = recovery_proxy(
        hrv_today=60,
        hrv_baseline=50,
        rhr_today=55,
        rhr_baseline=55,
        hrv_spread=20.0,
        rhr_spread=5.0,
    )
    assert tight_spread > wide_spread


def test_recovery_proxy_ignores_near_zero_spread_and_falls_back_to_ratio():
    # A near-zero spread (e.g. a coincidentally flat window) must not blow up
    # the z-score via division by ~0 -- falls back to the ratio scale instead.
    score = recovery_proxy(
        hrv_today=60,
        hrv_baseline=50,
        rhr_today=55,
        rhr_baseline=55,
        hrv_spread=0.0,
        rhr_spread=0.0,
    )
    assert score == pytest.approx(0.7 * 60.0 + 0.3 * 50.0)


def test_coverage_fraction_full_coverage_with_no_gaps():
    assert coverage_fraction([], window_start_ms=0, window_end_ms=1000) == 1.0


def test_coverage_fraction_gap_fully_inside_window():
    # 100ms gap out of a 1000ms window -> 90% coverage
    assert coverage_fraction([(400, 500)], window_start_ms=0, window_end_ms=1000) == pytest.approx(
        0.9
    )


def test_coverage_fraction_gap_partially_overlapping_window_edges():
    # gap starts before the window and ends after it -> the whole window is covered by the gap
    assert coverage_fraction([(-500, 1500)], window_start_ms=0, window_end_ms=1000) == 0.0


def test_coverage_fraction_still_open_gap_counts_through_now():
    # open gap (end=None) from 500 to "now" (900) inside a 1000ms window -> 400ms gap
    result = coverage_fraction([(500, None)], window_start_ms=0, window_end_ms=1000, now_ms=900)
    assert result == pytest.approx(0.6)


def test_coverage_fraction_ignores_gaps_outside_window():
    assert coverage_fraction([(2000, 3000)], window_start_ms=0, window_end_ms=1000) == 1.0


def test_coverage_fraction_sums_multiple_gaps():
    gaps = [(100, 150), (300, 350)]  # 50ms + 50ms = 100ms out of 1000ms
    assert coverage_fraction(gaps, window_start_ms=0, window_end_ms=1000) == pytest.approx(0.9)


def test_recovery_proxy_better_hrv_and_lower_rhr_scores_higher():
    good = recovery_proxy(hrv_today=70, hrv_baseline=50, rhr_today=50, rhr_baseline=55)
    baseline = recovery_proxy(hrv_today=50, hrv_baseline=50, rhr_today=55, rhr_baseline=55)
    assert good > baseline


def test_recovery_proxy_clamped_to_0_100():
    score = recovery_proxy(hrv_today=1000, hrv_baseline=1, rhr_today=1, rhr_baseline=1000)
    assert score == 100.0


def test_detect_sleep_windows_finds_long_stable_low_block():
    idx = pd.date_range("2037-07-01 23:00", periods=300, freq="1min", tz="UTC")  # 5 hours
    values = [55] * 300  # low and stable throughout
    series = pd.Series(values, index=idx)
    windows = detect_sleep_windows(series, rhr_baseline=55)
    assert len(windows) == 1
    assert windows[0].sleep_minutes >= 3 * 60


def test_detect_sleep_windows_ignores_short_blocks():
    idx = pd.date_range("2037-07-01 23:00", periods=60, freq="1min", tz="UTC")  # 1 hour, too short
    series = pd.Series([55] * 60, index=idx)
    windows = detect_sleep_windows(series, rhr_baseline=55)
    assert windows == []


def test_detect_sleep_windows_bridges_brief_interruptions():
    # 4 hours of low+stable HR, but with a 10-minute spike (movement/arousal)
    # in the middle that would break a naive contiguous-block check.
    idx = pd.date_range("2037-07-01 23:00", periods=240, freq="1min", tz="UTC")
    values = [55] * 120 + [95] * 10 + [55] * 110
    series = pd.Series(values, index=idx)
    windows = detect_sleep_windows(series, rhr_baseline=55)
    assert len(windows) == 1
    # Bridged window should span (approximately) the full 240 minutes, not
    # just one of the two halves either side of the interruption.
    assert windows[0].sleep_minutes >= 200


def test_detect_sleep_windows_does_not_bridge_long_gaps():
    # Two separate ~2h blocks separated by a 45-minute active gap (longer
    # than sleep_max_interruption_minutes) should NOT be merged into one,
    # and neither is individually long enough to count as sleep on its own.
    idx = pd.date_range("2037-07-01 23:00", periods=245, freq="1min", tz="UTC")
    values = [55] * 120 + [95] * 45 + [55] * 80
    series = pd.Series(values, index=idx)
    windows = detect_sleep_windows(series, rhr_baseline=55)
    assert windows == []


def test_detect_sleep_windows_ignores_high_hr():
    idx = pd.date_range("2037-07-01 12:00", periods=300, freq="1min", tz="UTC")
    series = pd.Series([120] * 300, index=idx)  # awake/active, not sleep
    windows = detect_sleep_windows(series, rhr_baseline=55)
    assert windows == []


def test_strain_proxy_empty_series_returns_none():
    assert strain_proxy(pd.Series(dtype=float), hr_rest=55, hr_max=185) is None


def test_strain_proxy_invalid_hrmax_returns_none():
    idx = pd.date_range("2037-07-01 12:00", periods=10, freq="1min", tz="UTC")
    series = pd.Series([100] * 10, index=idx)
    assert strain_proxy(series, hr_rest=100, hr_max=100) is None  # hrr <= 0


def test_strain_proxy_all_rest_gives_near_zero_strain():
    idx = pd.date_range("2037-07-01 12:00", periods=30, freq="1min", tz="UTC")
    series = pd.Series([55] * 30, index=idx)  # exactly at rest the whole time
    result = strain_proxy(series, hr_rest=55, hr_max=185)
    assert result is not None
    assert result.strain_proxy == pytest.approx(0.0)
    assert sum(result.time_in_zones_min.values()) == 0  # 0% HRR, below even z1


def test_strain_proxy_hard_effort_scores_higher_than_easy_effort():
    idx = pd.date_range("2037-07-01 12:00", periods=30, freq="1min", tz="UTC")
    easy = strain_proxy(pd.Series([120] * 30, index=idx), hr_rest=55, hr_max=185)
    hard = strain_proxy(pd.Series([170] * 30, index=idx), hr_rest=55, hr_max=185)
    assert easy is not None and hard is not None
    assert hard.strain_proxy > easy.strain_proxy


def test_strain_proxy_time_in_zones_sums_to_total_minutes():
    idx = pd.date_range("2037-07-01 12:00", periods=40, freq="1min", tz="UTC")
    # HRR fractions: 20min at ~55% (z1), 20min at ~95% (z5)
    values = [55 + 0.55 * (185 - 55)] * 20 + [55 + 0.95 * (185 - 55)] * 20
    series = pd.Series(values, index=idx)
    result = strain_proxy(series, hr_rest=55, hr_max=185)
    assert result is not None
    assert result.time_in_zones_min["z1"] == 20
    assert result.time_in_zones_min["z5"] == 20
    assert sum(result.time_in_zones_min.values()) == 40
