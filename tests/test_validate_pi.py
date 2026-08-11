import os

from whoop_local.db.schema import init_db
from whoop_local.sync.snapshot import create_snapshot
from whoop_local.sync.validate_pi import validate_pi_deployment

NOW_MS = 1_800_000_000_000


def _healthy_pi(path, *, latest_ms=NOW_MS - 60_000):
    connection = init_db(path)
    connection.execute(
        "INSERT INTO device (host, identifier, name) VALUES ('pi', 'AA:BB', 'WHOOP')"
    )
    connection.execute(
        "INSERT INTO raw_packets "
        "(ts_utc, source_uuid, source_host, origin, payload_hex) "
        "VALUES (?, 'AA:BB', 'pi', 'live', 'aabb')",
        (latest_ms,),
    )
    connection.execute(
        "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) "
        "VALUES (?, 70, 'pi', 'whoop')",
        (latest_ms,),
    )
    connection.commit()
    connection.close()


def _snapshot(live, snapshot):
    create_snapshot(live, snapshot)
    os.utime(snapshot, (NOW_MS / 1000, NOW_MS / 1000))


def test_pi_acceptance_passes_fresh_capture_and_matching_snapshot(tmp_path):
    live = tmp_path / "whoop.db"
    snapshot = tmp_path / "pi-latest.db"
    _healthy_pi(live)
    _snapshot(live, snapshot)

    report = validate_pi_deployment(live, snapshot, now_ms=NOW_MS)
    assert report["passed"] is True
    assert report["errors"] == []
    assert report["live"]["unexpected_capture_hosts"] == {}
    assert report["live"]["mac_owned_rows"] == 0


def test_pi_acceptance_rejects_stale_or_pre_restart_capture(tmp_path):
    live = tmp_path / "whoop.db"
    snapshot = tmp_path / "pi-latest.db"
    _healthy_pi(live, latest_ms=NOW_MS - 60 * 60_000)
    _snapshot(live, snapshot)

    report = validate_pi_deployment(
        live,
        snapshot,
        now_ms=NOW_MS,
        require_capture_after_ms=NOW_MS - 30 * 60_000,
    )
    assert report["passed"] is False
    assert "live Pi capture is stale" in report["errors"]
    assert any("restart/cutover" in error for error in report["errors"])


def test_pi_acceptance_rejects_wrong_host_private_rows_and_dataset_mismatch(tmp_path):
    live = tmp_path / "whoop.db"
    snapshot = tmp_path / "pi-latest.db"
    _healthy_pi(live)
    _healthy_pi(snapshot)
    os.utime(snapshot, (NOW_MS / 1000, NOW_MS / 1000))

    connection = init_db(live)
    connection.execute(
        "INSERT INTO battery_samples (ts_utc, raw_value, source_host) VALUES (?, 10, 'mac')",
        (NOW_MS - 30_000,),
    )
    connection.execute("INSERT INTO metrics_daily (date) VALUES ('2037-07-12')")
    connection.commit()
    connection.close()

    report = validate_pi_deployment(live, snapshot, now_ms=NOW_MS)
    assert report["passed"] is False
    assert any("exclusively tagged 'pi'" in error for error in report["errors"])
    assert any("Mac-owned/private" in error for error in report["errors"])
    assert any("different dataset IDs" in error for error in report["errors"])


def test_pi_acceptance_rejects_orphaned_open_gap(tmp_path):
    live = tmp_path / "whoop.db"
    snapshot = tmp_path / "pi-latest.db"
    _healthy_pi(live)
    connection = init_db(live)
    connection.execute(
        "INSERT INTO capture_gaps (source_host, gap_start_utc, reason) "
        "VALUES ('pi', ?, 'disconnected')",
        (NOW_MS - 120_000,),
    )
    connection.commit()
    connection.close()
    _snapshot(live, snapshot)

    report = validate_pi_deployment(live, snapshot, now_ms=NOW_MS)
    assert report["passed"] is False
    assert any("open gap" in error for error in report["errors"])
