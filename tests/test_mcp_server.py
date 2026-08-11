"""MCP server tests using invented, future-epoch data only.

Every date, timestamp, and physiological value in this module is synthetic.
"""

import sqlite3
import sys

import pytest

from whoop_local.mcp_server import server


def test_daily_summary_no_data(tmp_path):
    db_path = tmp_path / "test.db"
    from whoop_local.db.schema import init_db

    init_db(db_path).close()  # creates empty synthetic schema
    server.configure(str(db_path))

    summary = server.daily_summary()
    assert "No data yet" in summary


def test_daily_summary_with_data(tmp_path):
    db_path = tmp_path / "test.db"
    from whoop_local.db.schema import init_db

    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO metrics_daily (date, recovery_proxy, rmssd_ms, hrv_baseline_ms, rhr_bpm, "
        "sleep_minutes, provisional, coverage_pct) "
        "VALUES ('2042-03-17', 72.4, 53.6, 50.7, 61.3, 401, 0, 0.97)"
    )
    conn.execute(
        "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) "
        "VALUES (2278663200000, 61, 'synthetic-laptop', 'whoop')"
    )
    conn.commit()
    conn.close()
    server.configure(str(db_path))

    summary = server.daily_summary()
    assert "2042-03-17" in summary
    assert "72.4" in summary
    assert "not a WHOOP recovery score" in summary
    assert "Strain" not in summary


def test_configure_refuses_missing_database_without_creating_it(tmp_path):
    missing_path = tmp_path / "synthetic-missing.db"

    with pytest.raises(FileNotFoundError, match="must already exist"):
        server.configure(str(missing_path))

    assert not missing_path.exists()


def test_configure_enforces_sqlite_read_only_and_query_only(tmp_path):
    db_path = tmp_path / "synthetic-read-only.db"
    from whoop_local.db.schema import init_db

    init_db(db_path).close()
    server.configure(str(db_path))

    conn = server._conn()
    assert conn.execute("PRAGMA query_only").fetchone() == (1,)
    with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
        conn.execute("CREATE TABLE forbidden_write (id INTEGER)")


def test_healthcheck_cli_mode_validates_without_starting_server(tmp_path, monkeypatch):
    db_path = tmp_path / "synthetic-healthcheck.db"
    from whoop_local.db.schema import init_db

    init_db(db_path).close()
    monkeypatch.setattr(sys, "argv", ["doop-mcp", "--db", str(db_path), "--healthcheck"])
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("server must not start"))

    assert server.main() == 0


def test_healthcheck_cli_mode_fails_closed_for_missing_database(tmp_path, monkeypatch):
    missing = tmp_path / "synthetic-missing.db"
    monkeypatch.setattr(sys, "argv", ["doop-mcp", "--db", str(missing), "--healthcheck"])

    assert server.main() == 1
    assert not missing.exists()


def test_tools_require_configure_first():
    server._state["conn"] = None
    try:
        server.get_status()
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_telegram_relay_tools_are_minimum_disclosure(tmp_path):
    db_path = tmp_path / "test.db"
    from whoop_local.db.schema import init_db

    init_db(db_path).close()
    server.configure(str(db_path))
    today = server.get_telegram_training_guidance("2042-04-07")
    weekly = server.get_telegram_weekly_review("2042-04-07")
    assert set(today) == {"date", "text", "relay_contract"}
    assert set(weekly) == {"week_start", "text", "relay_contract"}
    assert "raw_packets" not in str(today)
