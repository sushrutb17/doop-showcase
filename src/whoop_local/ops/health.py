"""Read-only operational health report and local owner notification for Doop."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from whoop_local.config import DEFAULT_DB_PATH, default_compute_date, local_timezone
from whoop_local.db import battery_store
from whoop_local.mcp_server.queries import BATTERY_LOW_RAW_THRESHOLD
from whoop_local.security import secure_directory, secure_file

CORE_SERVICE_LABELS = {
    "collector": "com.doop.whoop-collector",
    "compute": "com.doop.whoop-compute",
    "backup": "com.doop.whoop-backup",
    "health": "com.doop.whoop-health",
    "logrotate": "com.doop.whoop-logrotate",
}
PI_SYNC_LABEL = "com.doop.whoop-pi-sync"
SEVERITY_ORDER = {"healthy": 0, "warning": 1, "critical": 2}
LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")


@dataclass(frozen=True)
class HealthThresholds:
    capture_stale_seconds: int = 5 * 60
    backup_warning_seconds: int = 36 * 60 * 60
    backup_critical_seconds: int = 48 * 60 * 60
    pi_sync_stale_seconds: int = 45 * 60
    disk_free_warning_bytes: int = 10 * 1024 * 1024 * 1024
    disk_free_critical_bytes: int = 5 * 1024 * 1024 * 1024
    disk_free_warning_fraction: float = 0.10
    log_rotation_bytes: int = 5 * 1024 * 1024
    recent_log_window_minutes: int = 60


@dataclass(frozen=True)
class ServiceStatus:
    label: str
    configured: bool
    state: str
    last_exit_code: int | None
    runs: int | None
    umask: str | None
    detail: str | None = None


class DiskUsage(Protocol):
    @property
    def total(self) -> int: ...

    @property
    def used(self) -> int: ...

    @property
    def free(self) -> int: ...


def filesystem_disk_usage(path: Path) -> DiskUsage:
    return shutil.disk_usage(path)


def _utc_iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp_iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return _utc_iso(datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC))


def _age_seconds(now: datetime, timestamp_ms: int | None) -> float | None:
    if timestamp_ms is None:
        return None
    return max(0.0, now.timestamp() - timestamp_ms / 1000)


def _alert(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def parse_launchctl_output(label: str, output: str, returncode: int) -> ServiceStatus:
    """Parse only stable, bounded launchctl fields; never shell-evaluate output."""
    if returncode != 0:
        missing = "Could not find service" in output or "not found" in output.lower()
        return ServiceStatus(
            label=label,
            configured=not missing,
            state="not_configured" if missing else "unavailable",
            last_exit_code=None,
            runs=None,
            umask=None,
            detail=None if missing else output.strip()[:240] or "launchctl failed",
        )

    state_match = re.search(r"^\s*state = ([^\n]+)", output, re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code = (-?\d+)", output, re.MULTILINE)
    runs_match = re.search(r"^\s*runs = (\d+)", output, re.MULTILINE)
    umask_match = re.search(r"^\s*umask = ([^\n]+)", output, re.MULTILINE)
    return ServiceStatus(
        label=label,
        configured=True,
        state=state_match.group(1).strip() if state_match else "unknown",
        last_exit_code=int(exit_match.group(1)) if exit_match else None,
        runs=int(runs_match.group(1)) if runs_match else None,
        umask=umask_match.group(1).strip() if umask_match else None,
    )


def launchd_service_status(label: str) -> ServiceStatus:
    if sys.platform != "darwin":
        return ServiceStatus(
            label=label,
            configured=False,
            state="unsupported_platform",
            last_exit_code=None,
            runs=None,
            umask=None,
        )
    command = ["launchctl", "print", f"gui/{os.getuid()}/{label}"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ServiceStatus(
            label=label,
            configured=True,
            state="unavailable",
            last_exit_code=None,
            runs=None,
            umask=None,
            detail=str(exc)[:240],
        )
    return parse_launchctl_output(label, f"{result.stdout}\n{result.stderr}", result.returncode)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{quote(str(path.resolve()))}?mode=ro",
        uri=True,
        timeout=5,
    )


def _database_report(path: Path, now: datetime) -> tuple[dict, str | None]:
    sizes = {
        "db_bytes": path.stat().st_size if path.is_file() else None,
        "wal_bytes": Path(f"{path}-wal").stat().st_size if Path(f"{path}-wal").is_file() else 0,
        "shm_bytes": Path(f"{path}-shm").stat().st_size if Path(f"{path}-shm").is_file() else 0,
    }
    if not path.is_file():
        return {
            "path": str(path),
            "exists": False,
            **sizes,
        }, None

    connection = _connect_read_only(path)
    try:
        schema_row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        dataset_row = connection.execute(
            "SELECT value FROM meta WHERE key = 'dataset_id'"
        ).fetchone()
        latest_hr = connection.execute("SELECT MAX(ts_utc) FROM hr_samples").fetchone()[0]
        latest_metric = connection.execute(
            "SELECT date, computed_at FROM metrics_daily ORDER BY date DESC LIMIT 1"
        ).fetchone()
        open_gap = connection.execute(
            "SELECT COUNT(*), MIN(gap_start_utc) FROM capture_gaps WHERE gap_end_utc IS NULL"
        ).fetchone()
        recent_gap_count = connection.execute(
            "SELECT COUNT(*) FROM capture_gaps WHERE gap_start_utc >= ?",
            (int((now - timedelta(minutes=15)).timestamp() * 1000),),
        ).fetchone()[0]
        latest_battery = battery_store.latest_battery(connection)
        battery_max = battery_store.battery_raw_max(connection)
    finally:
        connection.close()

    battery_timestamp = latest_battery[0] if latest_battery else None
    battery_raw = latest_battery[1] if latest_battery else None
    battery_pct = None
    if battery_raw is not None and battery_max:
        battery_pct = round(100 * min(battery_raw, battery_max) / battery_max)

    return {
        "path": str(path),
        "exists": True,
        "schema_version": str(schema_row[0]) if schema_row else None,
        "dataset_identity_present": bool(dataset_row and dataset_row[0]),
        "latest_hr_utc": _timestamp_iso(latest_hr),
        "latest_hr_age_seconds": round(_age_seconds(now, latest_hr) or 0.0, 1)
        if latest_hr is not None
        else None,
        "latest_metric_date": latest_metric[0] if latest_metric else None,
        "latest_metric_computed_utc": _timestamp_iso(latest_metric[1]) if latest_metric else None,
        "open_gap_count": int(open_gap[0]),
        "oldest_open_gap_utc": _timestamp_iso(open_gap[1]),
        "recent_gap_count_15m": int(recent_gap_count),
        "battery": {
            "latest_utc": _timestamp_iso(battery_timestamp),
            "raw": battery_raw,
            "pct_estimate": battery_pct,
            "low": battery_raw is not None and battery_raw <= BATTERY_LOW_RAW_THRESHOLD,
        },
        **sizes,
    }, str(dataset_row[0]) if dataset_row else None


def _backup_report(
    backup_dir: Path,
    now: datetime,
    live_dataset_id: str | None,
    live_schema_version: str | None,
) -> dict:
    backups = sorted(
        (path for path in backup_dir.glob("whoop-*.db") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )
    if not backups:
        return {
            "directory": str(backup_dir),
            "count": 0,
            "latest_path": None,
            "latest_age_seconds": None,
            "manifest_ok": False,
        }

    latest = backups[-1]
    manifest_path = latest.with_name(f"{latest.name}.manifest.json")
    manifest_ok = False
    manifest_error = None
    if not manifest_path.is_file():
        manifest_error = "missing manifest"
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_ok = (
                manifest.get("size_bytes") == latest.stat().st_size
                and manifest.get("dataset_id") == live_dataset_id
                and str(manifest.get("schema_version")) == live_schema_version
                and isinstance(manifest.get("sha256"), str)
                and len(manifest["sha256"]) == 64
            )
            if not manifest_ok:
                manifest_error = "size/schema/dataset/hash metadata mismatch"
        except (OSError, ValueError, TypeError) as exc:
            manifest_error = str(exc)[:240]

    latest_age = max(0.0, now.timestamp() - latest.stat().st_mtime)
    return {
        "directory": str(backup_dir),
        "count": len(backups),
        "latest_path": str(latest),
        "latest_created_utc": _utc_iso(datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)),
        "latest_age_seconds": round(latest_age, 1),
        "manifest_ok": manifest_ok,
        "manifest_error": manifest_error,
    }


def _recent_log_signals(
    collector_log: Path,
    now: datetime,
    window_minutes: int,
) -> dict:
    signals = {
        "history_failures": 0,
        "connection_errors": 0,
        "watchdog_reconnects": 0,
    }
    if not collector_log.is_file():
        return signals

    max_tail_bytes = 1024 * 1024
    with collector_log.open("rb") as handle:
        handle.seek(max(0, collector_log.stat().st_size - max_tail_bytes))
        payload = handle.read().decode("utf-8", errors="replace")
    cutoff = now.astimezone(local_timezone()) - timedelta(minutes=window_minutes)
    for line in payload.splitlines():
        match = LOG_TIMESTAMP_RE.match(line)
        if not match:
            continue
        timestamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f").replace(
            tzinfo=local_timezone()
        )
        if timestamp < cutoff:
            continue
        if "History sync failed" in line or "History transfer failed" in line:
            signals["history_failures"] += 1
        if "Connection error:" in line:
            signals["connection_errors"] += 1
        if "No HR notification in over 60s" in line:
            signals["watchdog_reconnects"] += 1
    return signals


def _logs_report(log_dir: Path, rotation_bytes: int) -> dict:
    files = []
    if log_dir.is_dir():
        for path in sorted(log_dir.glob("*.log")):
            if path.is_file() and not path.is_symlink():
                size = path.stat().st_size
                files.append(
                    {
                        "path": str(path),
                        "size_bytes": size,
                        "rotation_due": size >= rotation_bytes,
                    }
                )
    return {
        "directory": str(log_dir),
        "files": files,
        "total_bytes": sum(item["size_bytes"] for item in files),
        "rotation_threshold_bytes": rotation_bytes,
    }


def _service_alerts(services: dict[str, ServiceStatus]) -> list[dict]:
    alerts: list[dict] = []
    collector = services["collector"]
    if not collector.configured or collector.state != "running":
        alerts.append(
            _alert(
                "collector_service_down",
                "critical",
                "Collector LaunchAgent is not running.",
            )
        )
    for name in ("compute", "backup", "health", "logrotate"):
        service = services[name]
        if not service.configured:
            alerts.append(
                _alert(
                    f"{name}_service_missing",
                    "critical",
                    f"{name.capitalize()} LaunchAgent is not configured.",
                )
            )
        elif service.last_exit_code not in (None, 0):
            alerts.append(
                _alert(
                    f"{name}_service_failed",
                    "critical",
                    f"{name.capitalize()} LaunchAgent last exited non-zero.",
                )
            )
    for name, service in services.items():
        if service.configured and service.umask not in (None, "77", "077"):
            alerts.append(
                _alert(
                    f"{name}_umask_unsafe",
                    "critical",
                    f"{name.capitalize()} LaunchAgent is not using umask 0077.",
                )
            )
    return alerts


def collect_health(
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    backup_dir: Path | str | None = None,
    log_dir: Path | str | None = None,
    pi_snapshot: Path | str | None = None,
    now: datetime | None = None,
    thresholds: HealthThresholds | None = None,
    service_provider: Callable[[str], ServiceStatus] = launchd_service_status,
    disk_usage_provider: Callable[[Path], DiskUsage] = filesystem_disk_usage,
) -> dict:
    """Collect a bounded, read-only operational report with explicit alerts."""
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    instant = instant.astimezone(UTC)
    thresholds = thresholds or HealthThresholds()
    database_path = Path(db_path)
    backup_path = Path(backup_dir or database_path.parent / "backups")
    logs_path = Path(log_dir or database_path.parent.parent / "logs")
    pi_path = Path(pi_snapshot or database_path.parent / "pi-latest.db")

    services = {name: service_provider(label) for name, label in CORE_SERVICE_LABELS.items()}
    pi_service = service_provider(PI_SYNC_LABEL)
    services["pi_sync"] = pi_service
    alerts = _service_alerts(services)

    try:
        database, dataset_id = _database_report(database_path, instant)
    except (OSError, sqlite3.Error, ValueError) as exc:
        database = {
            "path": str(database_path),
            "exists": database_path.is_file(),
            "error": str(exc)[:240],
        }
        dataset_id = None
        alerts.append(
            _alert(
                "database_unreadable",
                "critical",
                "Canonical database could not be read safely.",
            )
        )

    if not database.get("exists"):
        alerts.append(
            _alert(
                "database_missing",
                "critical",
                "Canonical database file is missing.",
            )
        )
    elif "error" not in database:
        latest_age = database.get("latest_hr_age_seconds")
        collector_running = services["collector"].state == "running"
        if latest_age is None:
            alerts.append(
                _alert(
                    "capture_missing",
                    "critical",
                    "No captured heart-rate row exists.",
                )
            )
        elif latest_age > thresholds.capture_stale_seconds:
            if collector_running:
                alerts.append(
                    _alert(
                        "capture_input_stale",
                        "warning",
                        "Collector is running but band input is stale; the band may be off-wrist, charging, away, or disconnected.",
                    )
                )
            else:
                alerts.append(
                    _alert(
                        "capture_stale",
                        "critical",
                        "Capture is stale and the collector is not running.",
                    )
                )
        if database.get("open_gap_count", 0):
            alerts.append(
                _alert(
                    "capture_gap_open",
                    "warning",
                    "A capture gap is currently open.",
                )
            )
        if database.get("battery", {}).get("low"):
            alerts.append(
                _alert(
                    "battery_low",
                    "warning",
                    "Band battery is near the empirically observed shutdown floor.",
                )
            )

        expected_metric = default_compute_date(tz=local_timezone(), now=instant)
        local_now = instant.astimezone(local_timezone())
        if local_now.hour == 12 and local_now.minute < 45:
            expected_metric -= timedelta(days=1)
        latest_metric_text = database.get("latest_metric_date")
        latest_metric = date.fromisoformat(latest_metric_text) if latest_metric_text else None
        database["expected_metric_date"] = expected_metric.isoformat()
        if latest_metric is None or latest_metric < expected_metric:
            alerts.append(
                _alert(
                    "metrics_stale",
                    "critical",
                    f"Latest daily metric does not cover expected day {expected_metric.isoformat()}.",
                )
            )

    backups = _backup_report(
        backup_path,
        instant,
        dataset_id,
        database.get("schema_version"),
    )
    if not backups["count"]:
        alerts.append(_alert("backup_missing", "critical", "No canonical backup exists."))
    else:
        age = backups["latest_age_seconds"]
        if not backups["manifest_ok"]:
            alerts.append(
                _alert(
                    "backup_manifest_invalid",
                    "critical",
                    "Newest backup manifest does not match the live dataset metadata.",
                )
            )
        if age > thresholds.backup_critical_seconds:
            alerts.append(
                _alert(
                    "backup_stale",
                    "critical",
                    "Newest canonical backup is more than 48 hours old.",
                )
            )
        elif age > thresholds.backup_warning_seconds:
            alerts.append(
                _alert(
                    "backup_aging",
                    "warning",
                    "Newest canonical backup is more than 36 hours old.",
                )
            )

    disk = disk_usage_provider(database_path.parent)
    disk_free_fraction = disk.free / disk.total if disk.total else 0.0
    disk_report = {
        "total_bytes": disk.total,
        "used_bytes": disk.used,
        "free_bytes": disk.free,
        "free_fraction": round(disk_free_fraction, 4),
    }
    if disk.free < thresholds.disk_free_critical_bytes:
        alerts.append(
            _alert(
                "disk_space_critical",
                "critical",
                "Fewer than 5 GiB remain on the canonical data volume.",
            )
        )
    elif (
        disk.free < thresholds.disk_free_warning_bytes
        or disk_free_fraction < thresholds.disk_free_warning_fraction
    ):
        alerts.append(
            _alert(
                "disk_space_low",
                "warning",
                "Canonical data volume has low free space.",
            )
        )

    snapshot_age_seconds = (
        round(max(0.0, instant.timestamp() - pi_path.stat().st_mtime), 1)
        if pi_path.is_file()
        else None
    )
    pi_report = {
        "configured": pi_service.configured or pi_path.exists(),
        "service": asdict(pi_service),
        "snapshot_path": str(pi_path),
        "snapshot_exists": pi_path.is_file(),
        "snapshot_age_seconds": snapshot_age_seconds,
    }
    if pi_report["configured"]:
        if pi_service.configured and pi_service.last_exit_code not in (None, 0):
            alerts.append(
                _alert(
                    "pi_sync_failed",
                    "critical",
                    "Pi snapshot pull last exited non-zero.",
                )
            )
        if not pi_path.is_file():
            alerts.append(
                _alert(
                    "pi_snapshot_missing",
                    "critical",
                    "Pi sync is configured but no local Pi snapshot exists.",
                )
            )
        elif (
            snapshot_age_seconds is not None
            and snapshot_age_seconds > thresholds.pi_sync_stale_seconds
        ):
            alerts.append(
                _alert(
                    "pi_snapshot_stale",
                    "warning",
                    "Local Pi snapshot is older than 45 minutes.",
                )
            )

    log_signals = _recent_log_signals(
        logs_path / "collector.error.log",
        instant,
        thresholds.recent_log_window_minutes,
    )
    if log_signals["history_failures"]:
        alerts.append(
            _alert(
                "history_sync_recent_failure",
                "warning",
                "History sync failed recently; live capture may still be running.",
            )
        )
    if (
        log_signals["watchdog_reconnects"] >= 5
        and database.get("latest_hr_age_seconds", 0) > thresholds.capture_stale_seconds
    ):
        alerts.append(
            _alert(
                "capture_reconnect_churn",
                "warning",
                "Repeated watchdog reconnects accompany stale input; this commonly means the band is off-wrist or charging rather than a dead collector.",
            )
        )

    logs = _logs_report(logs_path, thresholds.log_rotation_bytes)
    severity = "healthy"
    for item in alerts:
        if SEVERITY_ORDER[item["severity"]] > SEVERITY_ORDER[severity]:
            severity = item["severity"]

    return {
        "status": severity,
        "generated_at_utc": _utc_iso(instant),
        "alerts": alerts,
        "database": database,
        "backups": backups,
        "disk": disk_report,
        "pi_sync": pi_report,
        "services": {name: asdict(service) for name, service in services.items()},
        "log_signals_last_hour": log_signals,
        "logs": logs,
    }


def format_health_text(report: dict) -> str:
    lines = [f"Doop health: {report['status'].upper()}"]
    if report["alerts"]:
        lines.extend(
            f"- [{item['severity']}] {item['code']}: {item['message']}" for item in report["alerts"]
        )
    else:
        lines.append("- No operational alerts.")
    database = report["database"]
    lines.append(
        f"- Capture age: {database.get('latest_hr_age_seconds')}s; "
        f"metric day: {database.get('latest_metric_date')}; "
        f"open gaps: {database.get('open_gap_count')}"
    )
    lines.append(
        f"- Backup age: {report['backups'].get('latest_age_seconds')}s; "
        f"disk free: {report['disk']['free_bytes']} bytes"
    )
    return "\n".join(lines)


def _notification_fingerprint(report: dict) -> str:
    return "|".join(sorted(f"{item['severity']}:{item['code']}" for item in report["alerts"]))


def _mac_notification(title: str, message: str) -> None:
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )


def notify_health_transition(
    report: dict,
    state_path: Path | str,
    *,
    notifier: Callable[[str, str], None] = _mac_notification,
) -> bool:
    """Notify once per alert-state transition, including recovery to healthy."""
    path = Path(state_path)
    previous = {}
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            previous = {}

    fingerprint = _notification_fingerprint(report)
    if previous.get("fingerprint") == fingerprint:
        return False

    prior_fingerprint = str(previous.get("fingerprint", ""))
    if report["status"] == "healthy":
        if prior_fingerprint:
            notifier("Doop health recovered", "All operational checks are healthy.")
    else:
        codes = [item["code"] for item in report["alerts"][:3]]
        remainder = max(0, len(report["alerts"]) - len(codes))
        summary = ", ".join(codes)
        if remainder:
            summary += f" (+{remainder} more)"
        notifier(f"Doop health {report['status']}", summary)

    secure_directory(path.parent)
    path.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "status": report["status"],
                "updated_at_utc": report["generated_at_utc"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    secure_file(path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--pi-snapshot", type=Path)
    parser.add_argument("--text", action="store_true", help="print concise text instead of JSON")
    parser.add_argument(
        "--notify",
        action="store_true",
        help="send a local macOS notification only when alert state changes",
    )
    parser.add_argument(
        "--notification-state",
        type=Path,
        help="private deduplication state for --notify",
    )
    args = parser.parse_args()

    report = collect_health(
        db_path=args.db,
        backup_dir=args.backup_dir,
        log_dir=args.log_dir,
        pi_snapshot=args.pi_snapshot,
    )
    print(format_health_text(report) if args.text else json.dumps(report, sort_keys=True))

    if args.notify:
        state_path = args.notification_state or args.db.parent / "ops" / "health-state.json"
        try:
            notify_health_transition(report, state_path)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"health notification failed: {exc}", file=sys.stderr)
            return 2
    return 0 if report["status"] != "critical" else 2


if __name__ == "__main__":
    raise SystemExit(main())
