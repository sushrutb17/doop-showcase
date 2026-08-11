"""Validated local athlete context and tunable metric thresholds.

No synthetic person is embedded in the public code.  Strain is withheld when
the user has not supplied a date of birth, explicit age, or explicit HRmax.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from whoop_local.config import RuntimeConfigError, config_section, config_value


def _number(name: str, value: Any, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise RuntimeConfigError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise RuntimeConfigError(f"{name} must be between {minimum:g} and {maximum:g}")
    return parsed


def _whole_number(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    parsed = _number(name, value, minimum=minimum, maximum=maximum)
    if not parsed.is_integer():
        raise RuntimeConfigError(f"{name} must be a whole number")
    return int(parsed)


def _optional_number(
    env_name: str,
    section: str,
    key: str,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    value = config_value(env_name, section, key)
    if value is None:
        return None
    return _number(f"{section}.{key}/{env_name}", value, minimum=minimum, maximum=maximum)


def _load_athlete_context() -> tuple[date | None, int | None]:
    allowed = {"date_of_birth", "age"}
    unexpected = set(config_section("athlete")) - allowed
    if unexpected:
        raise RuntimeConfigError(f"Unsupported [athlete] keys: {sorted(unexpected)}")

    dob_value = config_value("DOOP_ATHLETE_DOB", "athlete", "date_of_birth")
    age_value = config_value("DOOP_ATHLETE_AGE", "athlete", "age")
    if dob_value is not None and age_value is not None:
        raise RuntimeConfigError("Configure athlete.date_of_birth or athlete.age, not both")

    parsed_dob: date | None = None
    if dob_value is not None:
        if isinstance(dob_value, date):
            parsed_dob = dob_value
        elif isinstance(dob_value, str):
            try:
                parsed_dob = date.fromisoformat(dob_value.strip())
            except ValueError as exc:
                raise RuntimeConfigError(
                    "athlete.date_of_birth/DOOP_ATHLETE_DOB must be YYYY-MM-DD"
                ) from exc
        else:
            raise RuntimeConfigError("athlete.date_of_birth must be a TOML date or YYYY-MM-DD")

        today = date.today()
        age_today = (
            today.year
            - parsed_dob.year
            - ((today.month, today.day) < (parsed_dob.month, parsed_dob.day))
        )
        if not 10 <= age_today <= 120:
            raise RuntimeConfigError("athlete.date_of_birth must imply an age from 10 to 120")

    parsed_age = (
        _whole_number("athlete.age/DOOP_ATHLETE_AGE", age_value, minimum=10, maximum=120)
        if age_value is not None
        else None
    )
    return parsed_dob, parsed_age


ATHLETE_DATE_OF_BIRTH, ATHLETE_AGE_YEARS = _load_athlete_context()


@dataclass(frozen=True)
class MetricsConfig:
    # RR cleaning
    rr_min_ms: float = 300.0
    rr_max_ms: float = 2000.0
    rr_ectopic_pct: float = 0.20
    min_clean_rr_window_ms: float = 120_000.0

    # Resting HR
    rhr_rolling_window_minutes: int = 5
    rhr_percentile: float = 5.0

    # Rolling baseline
    baseline_window_days: int = 30
    baseline_min_days: int = 7
    baseline_provisional_min_days: int = 3

    # Recovery proxy
    recovery_w_hrv: float = 0.7
    recovery_w_rhr: float = 0.3
    baseline_normalization_points_per_sd: float = 15.0
    baseline_normalization_min_spread: float = 1e-6

    # Sleep detection
    sleep_min_duration_hours: float = 3.0
    sleep_rolling_window_minutes: int = 5
    sleep_hr_stability_std_bpm: float = 5.0
    sleep_hr_threshold_factor: float = 1.15
    sleep_max_interruption_minutes: float = 20.0

    # Decision-quality gates
    coverage_low_threshold: float = 0.95
    recovery_actionable_min_coverage: float = 0.75
    recovery_actionable_min_sleep_minutes: int = 180
    strain_actionable_min_coverage: float = 0.75

    # Strain proxy. Age is provided separately so a full birth date is optional.
    strain_trimp_coefficient: float = 0.64
    strain_trimp_exponent: float = 1.92
    strain_hrmax_bpm: float | None = None
    strain_hrmax_intercept: float = 208.0
    strain_hrmax_age_coefficient: float = 0.7
    strain_zone_boundaries: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9)


_ALLOWED_METRIC_KEYS = {
    "strain_hrmax_bpm",
    "strain_hrmax_intercept",
    "strain_hrmax_age_coefficient",
    "strain_trimp_coefficient",
    "strain_trimp_exponent",
}
_unexpected_metric_keys = set(config_section("metrics")) - _ALLOWED_METRIC_KEYS
if _unexpected_metric_keys:
    raise RuntimeConfigError(f"Unsupported [metrics] keys: {sorted(_unexpected_metric_keys)}")

EXPLICIT_STRAIN_HRMAX_BPM = _optional_number(
    "DOOP_STRAIN_HRMAX_BPM", "metrics", "strain_hrmax_bpm", minimum=80, maximum=260
)
_hrmax_intercept = _optional_number(
    "DOOP_STRAIN_HRMAX_INTERCEPT",
    "metrics",
    "strain_hrmax_intercept",
    minimum=80,
    maximum=300,
)
_hrmax_age_coefficient = _optional_number(
    "DOOP_STRAIN_HRMAX_AGE_COEFFICIENT",
    "metrics",
    "strain_hrmax_age_coefficient",
    minimum=0,
    maximum=3,
)
_trimp_coefficient = _optional_number(
    "DOOP_STRAIN_TRIMP_COEFFICIENT",
    "metrics",
    "strain_trimp_coefficient",
    minimum=0,
    maximum=10,
)
_trimp_exponent = _optional_number(
    "DOOP_STRAIN_TRIMP_EXPONENT",
    "metrics",
    "strain_trimp_exponent",
    minimum=0,
    maximum=10,
)

DEFAULT_CONFIG = MetricsConfig(
    strain_hrmax_bpm=EXPLICIT_STRAIN_HRMAX_BPM,
    strain_hrmax_intercept=_hrmax_intercept if _hrmax_intercept is not None else 208.0,
    strain_hrmax_age_coefficient=(
        _hrmax_age_coefficient if _hrmax_age_coefficient is not None else 0.7
    ),
    strain_trimp_coefficient=(_trimp_coefficient if _trimp_coefficient is not None else 0.64),
    strain_trimp_exponent=_trimp_exponent if _trimp_exponent is not None else 1.92,
)


def athlete_age_years(as_of: date | None = None) -> int | None:
    """Return configured age, or ``None`` when athlete context is absent.

    An explicit age is intentionally treated as a fixed approximation.  Supply
    a local date of birth when historically recomputing age-dependent strain.
    """
    if ATHLETE_AGE_YEARS is not None:
        return ATHLETE_AGE_YEARS
    if ATHLETE_DATE_OF_BIRTH is None:
        return None
    as_of = as_of or date.today()
    age = as_of.year - ATHLETE_DATE_OF_BIRTH.year
    had_birthday = (as_of.month, as_of.day) >= (
        ATHLETE_DATE_OF_BIRTH.month,
        ATHLETE_DATE_OF_BIRTH.day,
    )
    return age if had_birthday else age - 1


def strain_hrmax_bpm(as_of: date, config: MetricsConfig = DEFAULT_CONFIG) -> float | None:
    """Resolve HRmax without inventing personal context.

    An explicit HRmax on the supplied config wins. A zero age coefficient is
    also independently usable. Otherwise age or DOB is required; missing
    context withholds strain rather than computing it for a synthetic person.

    Keeping explicit HRmax inside ``MetricsConfig`` makes callers that supply a
    complete config deterministic; ambient local configuration only shapes
    ``DEFAULT_CONFIG``.
    """
    if config.strain_hrmax_bpm is not None:
        return config.strain_hrmax_bpm
    if config.strain_hrmax_age_coefficient == 0:
        return config.strain_hrmax_intercept
    age = athlete_age_years(as_of)
    if age is None:
        return None
    value = config.strain_hrmax_intercept - config.strain_hrmax_age_coefficient * age
    return value if 80 <= value <= 260 else None


def owner_age_years(as_of: date | None = None) -> int | None:
    """Backward-compatible alias; new code should use :func:`athlete_age_years`."""
    return athlete_age_years(as_of)
