"""Create a transactionally consistent SQLite snapshot for offline transfer."""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

from whoop_local.security import secure_directory, secure_file


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)


def create_snapshot(source: Path | str, output: Path | str) -> Path:
    """Back up *source* atomically to *output*, including committed WAL data."""
    source_path = Path(source)
    output_path = Path(output)
    if source_path.resolve() == output_path.resolve():
        raise ValueError("snapshot output must differ from the source database")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    secure_directory(output_path.parent)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        source_conn = _connect_read_only(source_path)
        destination_conn = sqlite3.connect(temp_path)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
            source_conn.close()
        secure_file(temp_path)
        os.replace(temp_path, output_path)
        secure_file(output_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="live Pi SQLite database")
    parser.add_argument("--output", required=True, help="snapshot path to replace atomically")
    args = parser.parse_args()
    print(create_snapshot(args.source, args.output))


if __name__ == "__main__":
    main()
