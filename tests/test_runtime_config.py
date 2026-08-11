import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(code: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("DOOP_")}
    environment.update(overrides)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_local_toml_config_drives_runtime_and_strain(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[runtime]
db_path = "private/reference.db"
host = "test-laptop"
timezone = "Etc/UTC"
device_name = "TEST-BAND"

[athlete]
age = 35

[metrics]
strain_hrmax_intercept = 210.0
strain_hrmax_age_coefficient = 0.5
""".strip()
    )
    config_path.chmod(0o600)
    result = _run(
        """
import json
from datetime import date
from whoop_local.config import DEFAULT_DB_PATH, WHOOP_ADVERTISED_NAME, current_host, local_timezone
from whoop_local.compute.config import athlete_age_years, strain_hrmax_bpm
print(json.dumps({
    "db": str(DEFAULT_DB_PATH),
    "host": current_host(),
    "timezone": str(local_timezone()),
    "device": WHOOP_ADVERTISED_NAME,
    "age": athlete_age_years(date(2042, 1, 1)),
    "hrmax": strain_hrmax_bpm(date(2042, 1, 1)),
}))
""",
        DOOP_CONFIG=str(config_path),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "db": str(tmp_path / "private" / "reference.db"),
        "host": "test-laptop",
        "timezone": "Etc/UTC",
        "device": "TEST-BAND",
        "age": 35,
        "hrmax": 192.5,
    }


def test_missing_athlete_context_withholds_age_dependent_strain():
    result = _run(
        """
from datetime import date
from whoop_local.compute.config import athlete_age_years, strain_hrmax_bpm
assert athlete_age_years(date(2042, 1, 1)) is None
assert strain_hrmax_bpm(date(2042, 1, 1)) is None
"""
    )
    assert result.returncode == 0, result.stderr


def test_environment_can_supply_dob_or_explicit_hrmax():
    # Derive a deliberately generic fixture so the repository never normalizes
    # a plausible person's exact date of birth.
    synthetic_dob = f"{date.today().year - 25}-01-01"
    dob_result = _run(
        """
from datetime import date
from whoop_local.compute.config import athlete_age_years
dob_year = date.today().year - 25
assert athlete_age_years(date(dob_year + 24, 12, 31)) == 24
assert athlete_age_years(date(dob_year + 25, 1, 1)) == 25
""",
        DOOP_ATHLETE_DOB=synthetic_dob,
    )
    assert dob_result.returncode == 0, dob_result.stderr

    hrmax_result = _run(
        """
from datetime import date
from whoop_local.compute.config import athlete_age_years, strain_hrmax_bpm
assert athlete_age_years(date(2042, 1, 1)) is None
assert strain_hrmax_bpm(date(2042, 1, 1)) == 187
""",
        DOOP_STRAIN_HRMAX_BPM="187",
    )
    assert hrmax_result.returncode == 0, hrmax_result.stderr


def test_invalid_explicit_timezone_fails_closed():
    result = _run(
        "from whoop_local.config import local_timezone; local_timezone()",
        DOOP_TIMEZONE="Not/A_Real_Zone",
    )
    assert result.returncode != 0
    assert "Unknown IANA timezone" in result.stderr


def test_unknown_runtime_key_fails_closed(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[runtime]\ndatabse_path = "private/reference.db"\n')
    config_path.chmod(0o600)

    result = _run("from whoop_local.config import DEFAULT_DB_PATH", DOOP_CONFIG=str(config_path))

    assert result.returncode != 0
    assert "Unsupported [runtime] keys" in result.stderr


def test_group_or_world_readable_config_fails_closed(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[runtime]\ntimezone = "Etc/UTC"\n')
    config_path.chmod(0o644)

    result = _run("from whoop_local.config import DEFAULT_DB_PATH", DOOP_CONFIG=str(config_path))

    assert result.returncode != 0
    assert "owner-only" in result.stderr
