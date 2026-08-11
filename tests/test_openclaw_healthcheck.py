import sqlite3

from whoop_local.db.schema import SCHEMA_VERSION, init_db
from whoop_local.integrations.openclaw_healthcheck import check_readiness


def test_healthcheck_accepts_initialized_read_only_database(tmp_path):
    path = tmp_path / "doop.db"
    connection = init_db(path)
    connection.close()
    assert check_readiness(path) == []


def test_healthcheck_does_not_create_missing_database(tmp_path):
    path = tmp_path / "missing.db"
    assert check_readiness(path) == ["database is missing"]
    assert not path.exists()


def test_healthcheck_rejects_wrong_schema_and_missing_tables(tmp_path):
    path = tmp_path / "bad.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    connection.commit()
    connection.close()

    errors = check_readiness(path)
    assert f"schema version must be {SCHEMA_VERSION}" in errors
    assert any("required tables are missing" in error for error in errors)
