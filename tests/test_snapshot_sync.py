import sqlite3

import pytest

from whoop_local.db.schema import SCHEMA_VERSION, init_db
from whoop_local.sync.import_snapshot import import_snapshot
from whoop_local.sync.snapshot import create_snapshot


def _seed_pi(path):
    conn = init_db(path)
    conn.execute(
        "INSERT INTO raw_packets "
        "(ts_utc, source_uuid, source_host, origin, payload_hex, notes) "
        "VALUES (1000, 'uuid', 'pi', 'live', 'aabb', 'raw')"
    )
    conn.execute(
        "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) "
        "VALUES (1000, 70, 'pi', 'whoop')"
    )
    conn.execute(
        "INSERT INTO rr_intervals (ts_utc, rr_ms, source_host, source) "
        "VALUES (1000, 800.0, 'pi', 'whoop')"
    )
    conn.execute(
        "INSERT INTO capture_gaps (source_host, gap_start_utc, reason) "
        "VALUES ('pi', 2000, 'disconnected')"
    )
    conn.commit()
    return conn


def test_snapshot_includes_committed_wal_rows(tmp_path):
    live = tmp_path / "pi.db"
    conn = _seed_pi(live)
    snapshot = create_snapshot(live, tmp_path / "snapshot.db")
    conn.close()

    copied = sqlite3.connect(snapshot)
    assert copied.execute("SELECT payload_hex FROM raw_packets").fetchone()[0] == "aabb"
    assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copied.close()


def test_import_is_idempotent_and_defers_open_gaps(tmp_path):
    source_path = tmp_path / "pi.db"
    source = _seed_pi(source_path)
    target_path = tmp_path / "mac.db"

    first = import_snapshot(source_path, target_path, imported_at=3000)
    assert first["raw_packets"] == 1
    assert first["hr_samples"] == 1
    assert first["rr_intervals"] == 1
    assert first["capture_gaps"] == 0
    assert sum(first.values()) == 3
    assert sum(import_snapshot(source_path, target_path).values()) == 0

    source.execute("UPDATE capture_gaps SET gap_end_utc = 2500 WHERE gap_end_utc IS NULL")
    source.execute(
        "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) "
        "VALUES (4000, 72, 'pi', 'whoop')"
    )
    source.commit()
    third = import_snapshot(source_path, target_path)
    source.close()
    assert third["capture_gaps"] == 1
    assert third["hr_samples"] == 1
    assert sum(third.values()) == 2

    target = sqlite3.connect(target_path)
    assert target.execute("SELECT COUNT(*) FROM raw_packets").fetchone()[0] == 1
    assert target.execute("SELECT COUNT(*) FROM hr_samples").fetchone()[0] == 2
    assert target.execute("SELECT COUNT(*) FROM capture_gaps").fetchone()[0] == 1
    assert target.execute("SELECT COUNT(*) FROM sync_ingest_rows").fetchone()[0] == 5
    assert (
        target.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        == SCHEMA_VERSION
    )
    target.close()


def test_import_rejects_wrong_host_and_self_import(tmp_path):
    source_path = tmp_path / "source.db"
    source = init_db(source_path)
    source.execute(
        "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) "
        "VALUES (1000, 70, 'mac', 'whoop')"
    )
    source.commit()
    source.close()

    with pytest.raises(ValueError, match="not tagged 'pi'"):
        import_snapshot(source_path, tmp_path / "target.db")
    with pytest.raises(ValueError, match="must differ"):
        import_snapshot(source_path, source_path)


def test_dataset_id_is_stable_across_init(tmp_path):
    path = tmp_path / "db.sqlite"
    first = init_db(path)
    first_id = first.execute("SELECT value FROM meta WHERE key='dataset_id'").fetchone()[0]
    first.close()
    second = init_db(path)
    second_id = second.execute("SELECT value FROM meta WHERE key='dataset_id'").fetchone()[0]
    second.close()
    assert second_id == first_id
