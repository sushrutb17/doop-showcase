"""Read/write the per-host device identity (see `device` table in SCHEMA.md)."""

import sqlite3
import time


def save_device(conn: sqlite3.Connection, host: str, identifier: str, name: str | None) -> None:
    conn.execute(
        "INSERT INTO device (host, identifier, name, last_seen_utc) VALUES (?, ?, ?, ?)",
        (host, identifier, name, int(time.time() * 1000)),
    )
    conn.commit()


def latest_identifier(conn: sqlite3.Connection, host: str) -> str | None:
    row = conn.execute(
        "SELECT identifier FROM device WHERE host = ? ORDER BY last_seen_utc DESC LIMIT 1",
        (host,),
    ).fetchone()
    return row[0] if row else None
