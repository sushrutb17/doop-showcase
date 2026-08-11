"""Bound private Doop logs with copy-truncate gzip rotation."""

from __future__ import annotations

import argparse
import gzip
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from whoop_local.security import secure_directory, secure_file

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP = 7


@dataclass(frozen=True)
class RotationResult:
    path: str
    rotated: bool
    original_bytes: int
    archive_path: str | None


def _archive_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}.gz")


def rotate_file(
    path: Path | str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
) -> RotationResult:
    """Rotate one regular log in place so an open O_APPEND descriptor survives."""
    log_path = Path(path)
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if keep < 1:
        raise ValueError("keep must be positive")
    if log_path.is_symlink():
        raise ValueError(f"refusing to rotate symlink: {log_path}")
    if not log_path.is_file():
        raise FileNotFoundError(log_path)

    original_size = log_path.stat().st_size
    if original_size < max_bytes:
        return RotationResult(str(log_path), False, original_size, None)

    secure_directory(log_path.parent)
    oldest = _archive_path(log_path, keep)
    oldest.unlink(missing_ok=True)
    for index in range(keep - 1, 0, -1):
        archive_source = _archive_path(log_path, index)
        if archive_source.exists():
            archive_source.replace(_archive_path(log_path, index + 1))

    destination = _archive_path(log_path, 1)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=log_path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as raw_output:
            with gzip.GzipFile(fileobj=raw_output, mode="wb", mtime=0) as output:
                with log_path.open("rb") as read_source:
                    remaining = original_size
                    while remaining:
                        chunk = read_source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        output.write(chunk)
                        remaining -= len(chunk)
                output.flush()
            raw_output.flush()
            os.fsync(raw_output.fileno())
        secure_file(temp_path)
        os.replace(temp_path, destination)
        secure_file(destination)
        # Install the durable archive before truncating. If compression or the
        # replace fails, the live log remains untouched. Copy-truncate keeps
        # launchd's open O_APPEND descriptor usable without restarting a
        # collector; a very small concurrent-write race is accepted for
        # diagnostic logs, never for canonical data.
        with log_path.open("rb+") as live_log:
            live_log.truncate(0)
            live_log.flush()
            os.fsync(live_log.fileno())
        secure_file(log_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    return RotationResult(str(log_path), True, original_size, str(destination))


def rotate_logs(
    log_dir: Path | str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
) -> list[RotationResult]:
    directory = secure_directory(log_dir)
    return [
        rotate_file(path, max_bytes=max_bytes, keep=keep)
        for path in sorted(directory.glob("*.log"))
        if path.is_file() and not path.is_symlink()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    args = parser.parse_args()
    for result in rotate_logs(args.log_dir, max_bytes=args.max_bytes, keep=args.keep):
        print(asdict(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
