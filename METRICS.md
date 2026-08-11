# METRICS.md

> How every metric is computed. These definitions are the contract — the compute layer implements exactly this, and the MCP layer reports it. All metrics are **independent proxies**, not WHOOP's proprietary scores.

## Ground rules

- **PPG-derived intervals are noisier than ECG.** Compute HRV only over **rest / detected sleep** windows; motion corrupts optical RR.
- **Clean RR before any HRV math.** Drop impossible intervals and ectopic artifacts first.
- **Normalize against the person, not absolute thresholds.** Recovery/strain are meaningful only relative to the configured user's rolling baseline.
- **Expect gaps.** With intermittent local capture, data can arrive in bursts (live sessions + buffer downloads). Compute over whatever clean data exists in a window; never assume continuous coverage. Prefer the longest clean segment available.

## Public evidence boundary

The numerical constants below are public software parameters and formulas, not
the owner's measurements. Tests exercise them with generated HR/RR series,
synthetic gaps, and temporary databases. Numerical personal validation—including
physiological values, device-to-device deltas, wear duration, and capture
totals—remains private. These proxies make no clinical or vendor-parity claim.

## Day attribution

- A metrics "day" `D` is the **local-time window from noon on `D−1` to noon on `D`**, *not* the UTC calendar date. Overnight sleep — and the HRV/RHR/recovery derived from it — is credited to the **wake-up date**: "how did I sleep last night?" asked on Tuesday means the row for Tuesday.
- Each `metrics_daily` row records the timezone used, so history stays reproducible if the owner travels.
- **Why:** sleep straddles UTC midnight for most timezones; UTC-date attribution splits one night across two rows and answers "last night" with the wrong day. Raw inputs are preserved, so changing attribution can be handled by an explicit recompute rather than silently rewriting source data.
- **Implementation:** `whoop_local.config.local_timezone()` resolves the system's real IANA zone (via `/etc/localtime`) and `local_day_window_utc_ms()` computes the noon-to-noon bounds; both `compute_daily.py` and `mcp_server/queries.py` share this one helper.

## RR cleaning (do this first)

1. Keep only `300 ms ≤ rr ≤ 2000 ms` (≈ 30–200 bpm).
2. Ectopic filter: drop any interval differing from the previous accepted one by > ~20% (successive-difference filter); optionally a moving-median filter.
3. Require a minimum clean window (e.g. ≥ 2 min of accepted intervals) before reporting HRV.

## HRV

- **RMSSD** (primary): `sqrt(mean((rr[i+1] - rr[i])^2))` over cleaned intervals, ms. Basis of the recovery proxy.
- **SDNN:** `std(rr)` over the window, ms.
- **pNN50** (optional): proportion of successive differences > 50 ms.

**Reporting window:** longest stable segment within detected sleep (pre-sleep-detection: lowest-HR stable nighttime segment). Record which window was used.

## Resting heart rate (RHR)

- Low percentile (≈5th) or min of rolling 5-min medians of `hr_bpm` during the sleep window. Avoid the raw single-beat minimum.

## Rolling baseline

- For RMSSD and RHR: median over a trailing window (default 30 days, min 7 to start).
- Exclude days with too little clean data from the baseline.
- **Provisional baseline:** from day 3 (`baseline_provisional_min_days`), `rolling_baseline()` computes a median over qualifying days and returns it—and the recovery proxy built on it—with `provisional=True`, instead of returning `None` until the 7-day minimum (`baseline_min_days`). The flag drops once the configured minimum is met. This softens the cold start without fabricating certainty; `metrics_daily.provisional` and `get_latest_recovery` carry the state through (see [MCP_SPEC.md](MCP_SPEC.md)).
- **History-import fallback for RHR only:** `compute_daily.py`'s `_load_history()` fills the RHR leg of the trailing window from `imported_daily_metrics` (for example, an Apple Health export) **only** while device-derived history has not reached `baseline_provisional_min_days`. Once recent device history is sufficient on its own, imports are not consulted—an old import must never silently outweigh an adequate current baseline. RMSSD/HRV never use imports: Apple's HRV metric is SDNN-based, not this project's RMSSD, and treating them as interchangeable would misrepresent different measurement methods.
- **Baseline spread:** `rolling_baseline()` also returns the trailing window's sample standard deviation (`spread`), but only once the baseline is fully established (`>= baseline_min_days`, not provisional). A very short sample's spread is too noisy to trust; `spread` is `None` during the provisional period.

## Recovery proxy (0–100)

1. `hrv_score` = today's RMSSD vs personal baseline (ratio, clamped/scaled).
2. `rhr_score` = today's RHR vs baseline (lower is better).
3. `recovery_proxy = clamp(0, 100, w1*hrv_score + w2*rhr_score)`, HRV-dominant (start `w1=0.7, w2=0.3`).

Start HRV-vs-baseline only, validate the trend against reviewed observations,
then tune. Record every formula change.

**Baseline normalization:** each score (`hrv_score`, `rhr_score`) is a z-score
against the configured user's rolling standard deviation when one is available
(see "Baseline spread" above). A value one standard deviation better than
baseline scores `50 + baseline_normalization_points_per_sd` regardless of the
metric's absolute units. It falls back to the ratio scale whenever `spread` is
`None` or too close to zero. Synthetic tests cover both paths; physiological
calibration at scale remains unproven.

### Decision-quality gate

Computation and actionability are separate. The raw recovery proxy remains stored for audit and future recomputation, but user/agent read APIs return `recovery_proxy=null` unless all of these configured conditions are true: coverage ≥75%, detected sleep ≥180 minutes, RMSSD/RHR/recovery inputs exist, and the baseline is non-provisional. Failed conditions are returned as structured confidence reasons. Synthetic MCP tests verify withholding for each failed condition; no owner day is used as the public example.

## Strain proxy (v1)

- TRIMP-style cardiovascular load:
  - An explicit configured HRmax is preferred. Otherwise, a configured age or
    date of birth supplies `HRmax = 208 − 0.7×age`; a date of birth allows
    historical recomputation to use age as of each day. If neither HRmax nor
    age/DOB is configured, the pipeline withholds strain instead of inventing
    a synthetic person. `HRrest` is that day's computed RHR.
  - Per minute: heart-rate-reserve fraction and a Banister-style exponential
    weighting (`frac * 0.64 * e^(1.92*frac)`); deployments must review these
    showcase defaults for their own context.
  - 5-zone (50/60/70/80/90% HRR) time-in-zones breakdown alongside the scalar.
- Reported on a custom scale (typical Banister TRIMP range, not bounded 0-100). Explicitly **not** labeled 0–21 (that implies WHOOP's scale) — see `STRAIN_NOTE` in `mcp_server/queries.py`.
- **Known v1 caveat:** summing across the *whole day* (not just exercise time), per the spec above, means a fully resting day still accumulates a non-trivial number from small above-rest fluctuations during normal waking hours. The number does not yet clearly separate a rest day from an active one on its own; check `time_in_zones_min` alongside it. Deployment-specific validation is required before treating the scale as trustworthy.
- **Actionability gate:** read APIs withhold the strain scalar below 75% capture coverage. The computed value stays in `metrics_daily`; imported workouts and zone context remain separately visible. Independently, computation itself is withheld when HRmax context is missing, as described above.

## Sleep detection (v1 — coarse)

- Contiguous block where `hr_bpm` is **low** (near/below RHR baseline) and **stable** (low rolling variance), at least the configured minimum duration. Motion is not available to this v1 detector.
- **Bridges brief interruptions** up to `sleep_max_interruption_minutes` rather than requiring one perfectly unbroken block. Ordinary arousals and sensor dropouts can fragment an otherwise credible low/stable period. Generated timelines verify that short interruptions join and longer gaps do not.
- Output `start_utc`, `end_utc`, `sleep_minutes` → write a `sessions` row (type `sleep`).
- **Coverage-aware output:** alongside `sleep_minutes`, `metrics_daily.coverage_pct` records what fraction of the day's window had live capture (`coverage_fraction()` in `compute/metrics.py`, from `capture_gaps` overlap; a still-open gap counts as ongoing through "now"). Detected sleep during gappy capture is a **floor, not truth**. `get_sleep`/`get_latest_recovery` append an "at least"/floor caveat when coverage is below `coverage_low_threshold`. Synthetic gap tests verify the coverage math and caveat boundary; the motivating night's measurements remain private.
- Known-crude; sleep staging is out of scope for v1.

## Validation

- **HR:** compare against Apple Watch / chest strap — should track closely.
- **HRV/recovery:** judge the *trend* over a week vs subjective recovery, not absolute numbers.
- Never present these as clinically valid or WHOOP-equivalent.

## Keep configurable

Window thresholds, baseline length, recovery weights, HRmax formula, sleep thresholds — all in one config module so they tune without touching logic. They are implemented in `compute/config.py` (`MetricsConfig`); runtime athlete and HRmax inputs are described in [USING_THIS_CODE.md](USING_THIS_CODE.md).

## Implementation status (showcase freeze)

- `compute/metrics.py`: RR cleaning, RMSSD, SDNN, pNN50, resting HR, rolling baseline, recovery proxy, coarse sleep detection — all implemented and unit-tested against synthetic data.
- `compute/compute_daily.py`: reads `hr_samples`/`rr_intervals` for a local
  noon-to-noon day, writes one idempotent `metrics_daily` row, and replaces that
  day's auto-detected sleep sessions during recomputation.
- **Four RR slots are decoded** — `collector.py` writes RR data
  into `rr_intervals`; the raw-first backfill can re-derive improved decoding.
- **Observed wrinkle:** RR can arrive sparsely and in bursts. The ectopic filter
  (drop a large jump from the previous *accepted* value, per the RR-cleaning
  rule above) does not distinguish a true ectopic beat from normal HR drift
  across a long data gap. A gap can therefore reset the clean-window clock.
  Synthetic series cover the implemented rule; longer, continuous wear is a
  deployment requirement, not a public numerical result.
- Recovery, sleep, and the v1 strain proxy are implemented. Their validation
  limits and actionability gates above remain part of the contract.
