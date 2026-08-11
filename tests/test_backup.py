from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from whoop_local.db.schema import SCHEMA_VERSION, init_db
from whoop_local.ops.backup import create_verified_backup, verify_backup


def _seed_database(path, value="first"):
    connection = init_db(path)
    connection.execute(
        "INSERT INTO raw_packets "
        "(ts_utc, source_uuid, source_host, origin, payload_hex, notes) "
        "VALUES (1000, 'uuid', 'mac', 'live', 'aabb', ?)",
        (value,),
    )
    connection.commit()
    return connection


def _mode(path):
    return os.stat(path).st_mode & 0o777


def test_init_db_restricts_database_and_wal_files(tmp_path):
    path = tmp_path / "private" / "whoop.db"
    connection = _seed_database(path)
    assert _mode(path) == 0o600
    if path.with_name("whoop.db-wal").exists():
        assert _mode(path.with_name("whoop.db-wal")) == 0o600
    if path.with_name("whoop.db-shm").exists():
        assert _mode(path.with_name("whoop.db-shm")) == 0o600
    connection.close()


def test_create_verified_backup_includes_wal_and_writes_manifest(tmp_path):
    source_path = tmp_path / "source.db"
    source = _seed_database(source_path)
    source.execute(
        "INSERT INTO raw_packets "
        "(ts_utc, source_uuid, source_host, origin, payload_hex, notes) "
        "VALUES (2000, 'uuid', 'mac', 'live', 'ccdd', 'wal row')"
    )
    source.commit()

    instant = datetime(2037, 7, 22, 12, 0, tzinfo=UTC)
    backup_dir = tmp_path / "backups"
    report, removed = create_verified_backup(source_path, backup_dir, keep=3, now=instant)
    source.close()

    backup_path = backup_dir / "whoop-20370722T120000Z.db"
    manifest_path = backup_path.with_name(f"{backup_path.name}.manifest.json")
    assert removed == []
    assert report.path == str(backup_path)
    assert report.schema_version == SCHEMA_VERSION
    assert report.integrity == "ok"
    assert report.foreign_key_violations == 0
    assert _mode(backup_dir) == 0o700
    assert _mode(backup_path) == 0o600
    assert _mode(manifest_path) == 0o600
    assert not backup_path.with_name(f"{backup_path.name}-wal").exists()
    assert not backup_path.with_name(f"{backup_path.name}-shm").exists()

    copied = sqlite3.connect(backup_path)
    assert copied.execute("SELECT COUNT(*) FROM raw_packets").fetchone()[0] == 2
    copied.close()

    manifest = json.loads(manifest_path.read_text())
    expected_hash = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    assert manifest["sha256"] == expected_hash
    assert verify_backup(backup_path).sha256 == expected_hash


def test_backup_retention_runs_only_after_new_backup_is_verified(tmp_path):
    source_path = tmp_path / "source.db"
    source = _seed_database(source_path)
    backup_dir = tmp_path / "backups"
    start = datetime(2037, 7, 20, 12, 0, tzinfo=UTC)

    paths = []
    for offset in range(3):
        report, _ = create_verified_backup(
            source_path,
            backup_dir,
            keep=2,
            now=start + timedelta(days=offset),
        )
        paths.append(report.path)
    source.close()

    assert not os.path.exists(paths[0])
    assert os.path.exists(paths[1])
    assert os.path.exists(paths[2])
    assert len(list(backup_dir.glob("whoop-*.db"))) == 2


def test_verify_backup_rejects_manifest_tampering(tmp_path):
    source_path = tmp_path / "source.db"
    source = _seed_database(source_path)
    report, _ = create_verified_backup(
        source_path,
        tmp_path / "backups",
        now=datetime(2037, 7, 22, 12, 0, tzinfo=UTC),
    )
    source.close()

    manifest_path = os.fspath(report.path) + ".manifest.json"
    manifest = json.loads(open(manifest_path, encoding="utf-8").read())
    manifest["sha256"] = "0" * 64
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)

    with pytest.raises(ValueError, match="sha256"):
        verify_backup(report.path)


def test_backup_requires_positive_retention(tmp_path):
    source_path = tmp_path / "source.db"
    source = _seed_database(source_path)
    with pytest.raises(ValueError, match="keep"):
        create_verified_backup(source_path, tmp_path / "backups", keep=0)
    source.close()
