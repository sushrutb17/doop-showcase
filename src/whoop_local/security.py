"""Owner-only filesystem permissions for Doop's private health data."""

from __future__ import annotations

import os
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def secure_directory(path: Path | str) -> Path:
    """Create *path* if needed and restrict it to the owning user."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, PRIVATE_DIRECTORY_MODE)
    return directory


def secure_file(path: Path | str) -> Path:
    """Restrict an existing private file to the owning user."""
    file_path = Path(path)
    if file_path.exists():
        os.chmod(file_path, PRIVATE_FILE_MODE)
    return file_path


def secure_sqlite_files(path: Path | str) -> None:
    """Restrict a SQLite database and any currently-present WAL sidecars."""
    db_path = Path(path)
    secure_file(db_path)
    secure_file(Path(f"{db_path}-wal"))
    secure_file(Path(f"{db_path}-shm"))
