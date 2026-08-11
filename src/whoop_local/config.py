"""Local runtime configuration, host identity, and day attribution.

Configuration is deliberately local: environment variables override an optional
TOML file, and no personal setting belongs in the repository.  See
``USING_THIS_CODE.md`` for the supported keys and privacy boundary.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import socket
import stat
import tomllib
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_CONFIG_BYTES = 64 * 1024
HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUNTIME_CONFIG_KEYS = {"db_path", "host", "timezone", "device_name"}


class RuntimeConfigError(ValueError):
    """Raised when explicitly supplied local configuration is unsafe or invalid."""


def default_config_path() -> Path:
    """Return the platform-local config path without creating it."""
    if configured := os.environ.get("DOOP_CONFIG"):
        return Path(configured).expanduser()
    if root := os.environ.get("XDG_CONFIG_HOME"):
        return Path(root).expanduser() / "doop" / "config.toml"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Doop" / "config.toml"
    return Path.home() / ".config" / "doop" / "config.toml"


def default_data_path() -> Path:
    """Keep private runtime data outside a source checkout by default."""
    if root := os.environ.get("XDG_DATA_HOME"):
        return Path(root).expanduser() / "doop" / "whoop.db"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Doop" / "whoop.db"
    return Path.home() / ".local" / "share" / "doop" / "whoop.db"


@lru_cache(maxsize=1)
def local_config() -> dict[str, Any]:
    """Load the optional local TOML file.

    An explicitly named ``DOOP_CONFIG`` must exist and parse successfully.  The
    platform-default path is optional, so a clean install can use safe defaults.
    """
    path = default_config_path()
    explicit = bool(os.environ.get("DOOP_CONFIG"))
    if not path.exists():
        if explicit:
            raise RuntimeConfigError(f"DOOP_CONFIG does not exist: {path}")
        return {}
    if not path.is_file():
        raise RuntimeConfigError(f"Doop config is not a regular file: {path}")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeConfigError("Doop config must be owner-only (chmod 600)")
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise RuntimeConfigError("Doop config exceeds the 64 KiB limit")
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeConfigError(f"Unable to read Doop config: {exc}") from exc
    allowed_sections = {"runtime", "athlete", "metrics"}
    unexpected = set(payload) - allowed_sections
    if unexpected:
        raise RuntimeConfigError(f"Unsupported Doop config sections: {sorted(unexpected)}")
    for name, section in payload.items():
        if not isinstance(section, dict):
            raise RuntimeConfigError(f"[{name}] must be a TOML table")
    unexpected_runtime_keys = set(payload.get("runtime", {})) - RUNTIME_CONFIG_KEYS
    if unexpected_runtime_keys:
        raise RuntimeConfigError(f"Unsupported [runtime] keys: {sorted(unexpected_runtime_keys)}")
    return payload


def config_section(name: str) -> Mapping[str, Any]:
    section = local_config().get(name, {})
    if not isinstance(section, dict):  # defensive if a caller replaces the cache in a test
        raise RuntimeConfigError(f"[{name}] must be a TOML table")
    return section


def config_value(env_name: str, section: str, key: str) -> Any | None:
    """Return an environment override or the corresponding TOML value."""
    if env_name in os.environ:
        value = os.environ[env_name]
        if not value.strip():
            raise RuntimeConfigError(f"{env_name} may not be empty")
        return value
    return config_section(section).get(key)


def configured_db_path() -> Path:
    value = config_value("DOOP_DB_PATH", "runtime", "db_path")
    if value is None:
        return default_data_path()
    if not isinstance(value, (str, os.PathLike)):
        raise RuntimeConfigError("runtime.db_path must be a path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        base = Path.cwd() if "DOOP_DB_PATH" in os.environ else default_config_path().parent
        path = base / path
    return path.resolve()


def _configured_host_label() -> str | None:
    value = config_value("DOOP_HOST", "runtime", "host")
    if value is None:
        return None
    if not isinstance(value, str) or not HOST_LABEL_RE.fullmatch(value):
        raise RuntimeConfigError(
            "runtime.host/DOOP_HOST must be 1-64 letters, digits, dots, dashes, or underscores"
        )
    return value


def current_host() -> str:
    """Return the configured source label or a conservative platform label."""
    if configured := _configured_host_label():
        return configured
    name = socket.gethostname().lower()
    if "raspberrypi" in name:
        return "pi"
    if platform.system() == "Darwin":
        return "mac"
    return "linux"


def configured_device_name() -> str:
    value = config_value("DOOP_DEVICE_NAME", "runtime", "device_name")
    if value is None:
        return "WHOOP"
    if not isinstance(value, str) or not value.strip():
        raise RuntimeConfigError("runtime.device_name/DOOP_DEVICE_NAME must be non-empty text")
    return value.strip()


def _system_timezone() -> ZoneInfo:
    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        return ZoneInfo("UTC")
    marker = "zoneinfo/"
    idx = link.find(marker)
    return ZoneInfo(link[idx + len(marker) :]) if idx != -1 else ZoneInfo("UTC")


def local_timezone() -> ZoneInfo:
    """Return the configured IANA timezone or the host timezone.

    Invalid explicit values fail closed instead of silently attributing health
    and workout records to the wrong local day.
    """
    value = config_value("DOOP_TIMEZONE", "runtime", "timezone")
    if value is None:
        return _system_timezone()
    if not isinstance(value, str) or not value.strip():
        raise RuntimeConfigError("runtime.timezone/DOOP_TIMEZONE must be an IANA timezone")
    try:
        return ZoneInfo(value.strip())
    except ZoneInfoNotFoundError as exc:
        raise RuntimeConfigError(f"Unknown IANA timezone: {value}") from exc


DEFAULT_DB_PATH = configured_db_path()
WHOOP_ADVERTISED_NAME = configured_device_name()


def local_day_window_utc_ms(day: date, tz: ZoneInfo | None = None) -> tuple[int, int]:
    """Return local noon on D-1 through local noon on D as UTC milliseconds."""
    tz = tz or local_timezone()
    end_local = datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=tz)
    start_local = end_local - timedelta(days=1)
    return int(start_local.timestamp() * 1000), int(end_local.timestamp() * 1000)


def default_compute_date(tz: ZoneInfo | None = None, now: datetime | None = None) -> date:
    """Return the date of the most recently completed noon-to-noon window."""
    tz = tz or local_timezone()
    now_local = (now or datetime.now(tz)).astimezone(tz)
    return now_local.date() if now_local.hour >= 12 else now_local.date() - timedelta(days=1)


def init_main() -> None:
    """Initialize a private local database for the console entry point."""
    parser = argparse.ArgumentParser(description="Initialize a local Doop SQLite database")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    from whoop_local.db.schema import init_db

    connection = init_db(args.db)
    try:
        version = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
    finally:
        connection.close()
    print(f"Initialized Doop schema v{version} at {args.db}")
