from tests.synthetic_frames import battery_event, unrelated_event
from whoop_local.db.battery_store import (
    backfill_from_raw_packets,
    battery_raw_max,
    latest_battery,
    record_battery_sample,
)
from whoop_local.db.schema import init_db


def test_record_battery_sample_tracks_running_max(tmp_path):
    conn = init_db(tmp_path / "test.db")

    record_battery_sample(conn, 1000, 600, "synthetic-host")
    assert battery_raw_max(conn) == 600

    record_battery_sample(conn, 2000, 400, "synthetic-host")  # max must not drop
    assert battery_raw_max(conn) == 600

    record_battery_sample(conn, 3000, 800, "synthetic-host")  # new synthetic maximum
    assert battery_raw_max(conn) == 800

    assert latest_battery(conn) == (3000, 800)


def test_record_battery_sample_dedupes_on_ts_and_host(tmp_path):
    conn = init_db(tmp_path / "test.db")

    assert record_battery_sample(conn, 1000, 600, "synthetic-host") is True
    assert record_battery_sample(conn, 1000, 600, "synthetic-host") is False

    count = conn.execute("SELECT COUNT(*) FROM battery_samples").fetchone()[0]
    assert count == 1


def test_backfill_from_raw_packets_decodes_synthetic_battery_events(tmp_path):
    conn = init_db(tmp_path / "test.db")
    # Round values and 60-second future spacing are deliberately generated;
    # they are not retained operational observations.
    conn.execute(
        "INSERT INTO raw_packets (ts_utc, source_uuid, source_host, origin, payload_hex) "
        "VALUES (2132615400000, '61080004-8d6d-82b8-614a-1c8cb0f8dcc6', "
        "'synthetic-host', 'synthetic', ?)",
        (battery_event(640).hex(),),
    )
    conn.execute(
        "INSERT INTO raw_packets (ts_utc, source_uuid, source_host, origin, payload_hex) "
        "VALUES (2132615460000, '61080004-8d6d-82b8-614a-1c8cb0f8dcc6', "
        "'synthetic-host', 'synthetic', ?)",
        (unrelated_event().hex(),),
    )
    conn.commit()

    inserted = backfill_from_raw_packets(conn)
    assert inserted == 1
    assert latest_battery(conn) == (2132615400000, 640)

    # Re-running must not duplicate.
    assert backfill_from_raw_packets(conn) == 0
