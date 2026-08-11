from __future__ import annotations

import gzip
import json
import os
from collections import namedtuple
from datetime import UTC, datetime, timedelta

import pytest

from whoop_local.db import battery_store
from whoop_local.db.schema import init_db
from whoop_local.ops.backup import create_verified_backup
from whoop_local.ops.health import (
    CORE_SERVICE_LABELS,
    PI_SYNC_LABEL,
    HealthThresholds,
    ServiceStatus,
    collect_health,
    notify_health_transition,
    parse_launchctl_output,
)
from whoop_local.ops.logs import rotate_file

DiskUsage = namedtuple("DiskUsage", "total used free")


def _service(
    label: str,
    *,
    configured: bool = True,
    state: str | None = None,
    exit_code: int | None = 0,
) -> ServiceStatus:
    if state is None:
        state = (
            "running" if label == CORE_SERVICE_LABELS["collector"] and configured else "not running"
        )
    if label == CORE_SERVICE_LABELS["collector"] and state == "running":
        exit_code = None
    return ServiceStatus(
        label=label,
        configured=configured,
        state=state,
        last_exit_code=exit_code,
        runs=3 if configured else None,
        umask="77" if configured else None,
    )


def _services(overrides=None):
    overrides = overrides or {}

    def provider(label):
        if label in overrides:
            return overrides[label]
        if label == PI_SYNC_LABEL:
            return _service(label, configured=False, state="not_configured")
        return _service(label)

    return provider


def _seed_healthy(tmp_path, now):
    db_path = tmp_path / "data" / "whoop.db"
    connection = init_db(db_path)
    timestamp_ms = int((now - timedelta(seconds=30)).timestamp() * 1000)
    connection.execute(
        "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) VALUES (?, 60, 'mac', 'hrm')",
        (timestamp_ms,),
    )
    connection.execute(
        "INSERT INTO metrics_daily (date, computed_at, timezone) VALUES ('2037-07-23', ?, 'UTC')",
        (timestamp_ms,),
    )
    battery_store.record_battery_sample(connection, timestamp_ms, 500, "mac")
    connection.commit()

    backup_dir = tmp_path / "data" / "backups"
    backup, _ = create_verified_backup(
        db_path,
        backup_dir,
        now=now - timedelta(hours=1),
    )
    backup_timestamp = (now - timedelta(hours=1)).timestamp()
    os.utime(backup.path, (backup_timestamp, backup_timestamp))
    return connection, db_path, backup_dir


def _collect(tmp_path, now, db_path, backup_dir, **kwargs):
    return collect_health(
        db_path=db_path,
        backup_dir=backup_dir,
        log_dir=tmp_path / "logs",
        now=now,
        service_provider=kwargs.pop("service_provider", _services()),
        disk_usage_provider=kwargs.pop(
            "disk_usage_provider",
            lambda _path: DiskUsage(
                100 * 1024**3,
                50 * 1024**3,
                50 * 1024**3,
            ),
        ),
        **kwargs,
    )


def test_parse_launchctl_output_extracts_bounded_fields():
    report = parse_launchctl_output(
        "com.doop.example",
        """
        state = running
        umask = 77
        runs = 4
        last exit code = 0
        """,
        0,
    )
    assert report.configured is True
    assert report.state == "running"
    assert report.umask == "77"
    assert report.runs == 4
    assert report.last_exit_code == 0


def test_healthy_report_covers_capture_compute_backup_disk_and_services(tmp_path):
    now = datetime(2037, 7, 24, 9, 0, tzinfo=UTC)
    connection, db_path, backup_dir = _seed_healthy(tmp_path, now)
    report = _collect(tmp_path, now, db_path, backup_dir)
    connection.close()

    assert report["status"] == "healthy"
    assert report["alerts"] == []
    assert report["database"]["latest_hr_age_seconds"] == 30
    assert report["database"]["latest_metric_date"] == "2037-07-23"
    assert report["backups"]["manifest_ok"] is True
    assert report["pi_sync"]["configured"] is False


def test_stale_input_is_distinct_from_dead_collector(tmp_path):
    now = datetime(2037, 7, 24, 9, 0, tzinfo=UTC)
    connection, db_path, backup_dir = _seed_healthy(tmp_path, now)
    stale_ms = int((now - timedelta(hours=1)).timestamp() * 1000)
    connection.execute("UPDATE hr_samples SET ts_utc = ?", (stale_ms,))
    connection.commit()

    report = _collect(tmp_path, now, db_path, backup_dir)
    connection.close()
    codes = {item["code"] for item in report["alerts"]}

    assert report["status"] == "warning"
    assert "capture_input_stale" in codes
    assert "collector_service_down" not in codes


def test_stopped_collector_is_critical(tmp_path):
    now = datetime(2037, 7, 24, 9, 0, tzinfo=UTC)
    connection, db_path, backup_dir = _seed_healthy(tmp_path, now)
    collector = CORE_SERVICE_LABELS["collector"]
    provider = _services({collector: _service(collector, state="not running", exit_code=1)})

    report = _collect(
        tmp_path,
        now,
        db_path,
        backup_dir,
        service_provider=provider,
    )
    connection.close()

    assert report["status"] == "critical"
    assert "collector_service_down" in {item["code"] for item in report["alerts"]}


@pytest.mark.parametrize("name", ["compute", "backup", "health", "logrotate"])
def test_failed_scheduled_service_is_critical(tmp_path, name):
    now = datetime(2037, 7, 24, 9, 0, tzinfo=UTC)
    connection, db_path, backup_dir = _seed_healthy(tmp_path, now)
    label = CORE_SERVICE_LABELS[name]
    provider = _services({label: _service(label, state="not running", exit_code=1)})

    report = _collect(
        tmp_path,
        now,
        db_path,
        backup_dir,
        service_provider=provider,
    )
    connection.close()

    assert report["status"] == "critical"
    assert f"{name}_service_failed" in {item["code"] for item in report["alerts"]}


def test_stale_backup_and_low_disk_are_explicit(tmp_path):
    now = datetime(2037, 7, 24, 9, 0, tzinfo=UTC)
    connection, db_path, backup_dir = _seed_healthy(tmp_path, now)
    for path in backup_dir.glob("*"):
        old = (now - timedelta(days=3)).timestamp()
        os.utime(path, (old, old))
    thresholds = HealthThresholds(
        disk_free_warning_bytes=20,
        disk_free_critical_bytes=5,
    )

    report = _collect(
        tmp_path,
        now,
        db_path,
        backup_dir,
        thresholds=thresholds,
        disk_usage_provider=lambda _path: DiskUsage(100, 90, 10),
    )
    connection.close()
    codes = {item["code"] for item in report["alerts"]}

    assert report["status"] == "critical"
    assert "backup_stale" in codes
    assert "disk_space_low" in codes


def test_recent_history_failure_is_a_warning_while_live_capture_can_continue(tmp_path):
    now = datetime(2037, 7, 24, 9, 0, tzinfo=UTC)
    connection, db_path, backup_dir = _seed_healthy(tmp_path, now)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "collector.error.log").write_text(
        "2037-07-24 08:45:00,000 ERROR whoop_local.collector: "
        "History sync failed; continuing with live capture: disconnected\n"
    )

    report = collect_health(
        db_path=db_path,
        backup_dir=backup_dir,
        log_dir=log_dir,
        now=now,
        service_provider=_services(),
        disk_usage_provider=lambda _path: DiskUsage(
            100 * 1024**3,
            50 * 1024**3,
            50 * 1024**3,
        ),
    )
    connection.close()

    assert report["status"] == "warning"
    assert "history_sync_recent_failure" in {item["code"] for item in report["alerts"]}


def test_health_notifications_are_deduplicated_and_recovery_is_visible(tmp_path):
    state = tmp_path / "ops" / "health-state.json"
    sent = []
    warning = {
        "status": "warning",
        "generated_at_utc": "2037-07-24T09:00:00Z",
        "alerts": [
            {
                "severity": "warning",
                "code": "capture_input_stale",
                "message": "stale",
            }
        ],
    }
    healthy = {
        "status": "healthy",
        "generated_at_utc": "2037-07-24T09:15:00Z",
        "alerts": [],
    }

    def notifier(title, message):
        sent.append((title, message))

    assert notify_health_transition(warning, state, notifier=notifier) is True
    assert notify_health_transition(warning, state, notifier=notifier) is False
    assert notify_health_transition(healthy, state, notifier=notifier) is True
    assert len(sent) == 2
    assert "warning" in sent[0][0]
    assert "recovered" in sent[1][0]
    assert json.loads(state.read_text())["status"] == "healthy"
    assert os.stat(state).st_mode & 0o777 == 0o600


def test_log_rotation_copy_truncates_and_bounds_private_archives(tmp_path):
    log_path = tmp_path / "collector.error.log"
    log_path.write_bytes(b"first payload\n")
    first = rotate_file(log_path, max_bytes=1, keep=2)
    assert first.rotated is True
    assert log_path.read_bytes() == b""
    assert (tmp_path / "collector.error.log.1.gz").is_file()

    log_path.write_bytes(b"second payload\n")
    rotate_file(log_path, max_bytes=1, keep=2)
    log_path.write_bytes(b"third payload\n")
    rotate_file(log_path, max_bytes=1, keep=2)

    assert (tmp_path / "collector.error.log.1.gz").is_file()
    assert (tmp_path / "collector.error.log.2.gz").is_file()
    assert not (tmp_path / "collector.error.log.3.gz").exists()
    assert os.stat(log_path).st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / "collector.error.log.1.gz").st_mode & 0o777 == 0o600


def test_log_rotation_preserves_open_append_descriptor(tmp_path):
    log_path = tmp_path / "collector.error.log"
    before = b"before rotation\n"
    after = b"after rotation\n"

    with log_path.open("ab", buffering=0) as launchd_descriptor:
        launchd_descriptor.write(before)
        result = rotate_file(log_path, max_bytes=1, keep=2)
        launchd_descriptor.write(after)

    assert result.rotated is True
    assert log_path.read_bytes() == after
    with gzip.open(tmp_path / "collector.error.log.1.gz", "rb") as archive:
        assert archive.read() == before


def test_log_rotation_does_not_truncate_before_archive_is_installed(tmp_path, monkeypatch):
    log_path = tmp_path / "collector.error.log"
    payload = b"must survive failed archive installation\n"
    log_path.write_bytes(payload)

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        rotate_file(log_path, max_bytes=1, keep=2)

    assert log_path.read_bytes() == payload
