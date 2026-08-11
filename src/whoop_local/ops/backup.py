"""Create and verify atomic owner-only backups of the canonical Doop DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from whoop_local.db.schema import SCHEMA_VERSION
from whoop_local.security import secure_directory, secure_file
from whoop_local.sync.snapshot import create_snapshot

BACKUP_PREFIX = "whoop-"
BACKUP_SUFFIX = ".db"
MANIFEST_SUFFIX = ".manifest.json"
DEFAULT_KEEP = 7
MIN_FREE_HEADROOM_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class BackupReport:
    path: str
    created_at_utc: str
    size_bytes: int
    sha256: str
    schema_version: str
    dataset_id: str
    integrity: str
    foreign_key_violations: int


def _connect_read_only(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    suffix = "&immutable=1" if immutable else ""
    return sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro{suffix}", uri=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_report(
    path: Path,
    *,
    created_at_utc: str | None = None,
    immutable: bool = False,
) -> BackupReport:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = _connect_read_only(path, immutable=immutable)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        version_row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        dataset_row = connection.execute(
            "SELECT value FROM meta WHERE key = 'dataset_id'"
        ).fetchone()
    finally:
        connection.close()

    schema_version = str(version_row[0]) if version_row else ""
    dataset_id = str(dataset_row[0]) if dataset_row else ""
    if integrity != "ok":
        raise ValueError(f"database integrity check failed: {integrity}")
    if foreign_key_violations:
        raise ValueError(f"database has {foreign_key_violations} foreign-key violation(s)")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"database schema version must be {SCHEMA_VERSION}; got {schema_version or 'missing'}"
        )
    if not dataset_id:
        raise ValueError("database is missing meta.dataset_id")

    timestamp = created_at_utc or datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    return BackupReport(
        path=str(path),
        created_at_utc=timestamp,
        size_bytes=path.stat().st_size,
        sha256=_sha256(path),
        schema_version=schema_version,
        dataset_id=dataset_id,
        integrity=integrity,
        foreign_key_violations=foreign_key_violations,
    )


def _manifest_path(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}{MANIFEST_SUFFIX}")


def _write_manifest(report: BackupReport) -> Path:
    manifest_path = _manifest_path(Path(report.path))
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{manifest_path.name}.", suffix=".tmp", dir=manifest_path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(report), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        secure_file(temp_path)
        os.replace(temp_path, manifest_path)
        secure_file(manifest_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return manifest_path


def verify_backup(path: Path | str) -> BackupReport:
    """Verify SQLite integrity, identity, schema, and the adjacent hash manifest."""
    backup_path = Path(path)
    # Backups are closed artifacts. immutable mode prevents SQLite from
    # creating -wal/-shm sidecars merely to verify a WAL-mode database.
    report = _database_report(backup_path, immutable=True)
    manifest_path = _manifest_path(backup_path)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"backup manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field in ("sha256", "size_bytes", "schema_version", "dataset_id"):
        if manifest.get(field) != getattr(report, field):
            raise ValueError(f"backup manifest {field} does not match {backup_path}")
    return BackupReport(
        **{
            **asdict(report),
            "created_at_utc": str(manifest.get("created_at_utc", report.created_at_utc)),
        }
    )


def _backup_files(backup_dir: Path) -> list[Path]:
    return sorted(
        path for path in backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if path.is_file()
    )


def _prune_verified_backups(backup_dir: Path, keep: int) -> list[str]:
    removed: list[str] = []
    backups = _backup_files(backup_dir)
    for backup in backups[:-keep]:
        manifest = _manifest_path(backup)
        backup.unlink()
        manifest.unlink(missing_ok=True)
        Path(f"{backup}-wal").unlink(missing_ok=True)
        Path(f"{backup}-shm").unlink(missing_ok=True)
        removed.append(str(backup))
    return removed


def create_verified_backup(
    source: Path | str,
    backup_dir: Path | str,
    *,
    keep: int = DEFAULT_KEEP,
    now: datetime | None = None,
) -> tuple[BackupReport, list[str]]:
    """Atomically create, verify, manifest, and then retention-prune a backup."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    source_path = Path(source)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    output_dir = secure_directory(backup_dir)
    required_free = source_path.stat().st_size + MIN_FREE_HEADROOM_BYTES
    available = shutil.disk_usage(output_dir).free
    if available < required_free:
        raise OSError(
            f"insufficient free space for backup: need {required_free} bytes, have {available}"
        )

    instant = now or datetime.now(tz=UTC)
    if instant.tzinfo is None:
        raise ValueError("backup timestamp must be timezone-aware")
    instant = instant.astimezone(UTC)
    stamp = instant.strftime("%Y%m%dT%H%M%SZ")
    backup_path = output_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    if backup_path.exists() or _manifest_path(backup_path).exists():
        raise FileExistsError(f"backup already exists for timestamp: {backup_path}")

    source_report = _database_report(source_path)
    try:
        create_snapshot(source_path, backup_path)
        secure_file(backup_path)
        report = _database_report(
            backup_path,
            created_at_utc=instant.isoformat(),
            immutable=True,
        )
        if report.dataset_id != source_report.dataset_id:
            raise ValueError("backup dataset_id does not match source")
        if report.schema_version != source_report.schema_version:
            raise ValueError("backup schema version does not match source")
        _write_manifest(report)
        verify_backup(backup_path)
    except BaseException:
        backup_path.unlink(missing_ok=True)
        _manifest_path(backup_path).unlink(missing_ok=True)
        raise

    removed = _prune_verified_backups(output_dir, keep)
    return report, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/whoop.db"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/backups"))
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument("--verify", type=Path, help="verify one existing backup instead")
    args = parser.parse_args()

    if args.verify:
        report = verify_backup(args.verify)
        print(json.dumps(asdict(report), sort_keys=True))
        return 0

    report, removed = create_verified_backup(args.source, args.output_dir, keep=args.keep)
    print(json.dumps({"backup": asdict(report), "removed": removed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
