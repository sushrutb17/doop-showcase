from tests.synthetic_frames import realtime_frame
from whoop_local.capture.backfill_rr import backfill_rr_slots
from whoop_local.capture.proprietary_protocol import CHAR_DATA_FROM_STRAP
from whoop_local.db.schema import init_db


# Decimal hundred-steps make slot position visible at a glance. These
# intentionally nonphysiological sequences are generated contract fixtures,
# not retained RR observations.
def _generated_rr(count):
    return tuple(100 * slot for slot in range(1, count + 1))


FRAME_COUNT2 = realtime_frame(60, _generated_rr(2)).hex()
FRAME_COUNT3 = realtime_frame(60, _generated_rr(3)).hex()
FRAME_COUNT4 = realtime_frame(60, _generated_rr(4)).hex()


def _insert_packet(conn, ts_utc, payload_hex, host="synthetic-host"):
    conn.execute(
        "INSERT INTO raw_packets (ts_utc, source_uuid, source_host, payload_hex) VALUES (?, ?, ?, ?)",
        (ts_utc, CHAR_DATA_FROM_STRAP, host, payload_hex),
    )


def _insert_rr(conn, ts_utc, rr_ms, host="synthetic-host"):
    conn.execute(
        "INSERT INTO rr_intervals (ts_utc, rr_ms, source_host, source) VALUES (?, ?, ?, 'whoop')",
        (ts_utc, rr_ms, host),
    )


def _rr_rows(conn, ts_utc):
    return [
        row[0]
        for row in conn.execute(
            "SELECT rr_ms FROM rr_intervals WHERE ts_utc = ? ORDER BY id", (ts_utc,)
        )
    ]


def test_backfill_inserts_only_dropped_slots(tmp_path):
    conn = init_db(tmp_path / "test.db")
    # Simulate the pre-D54 collector: only the first two RR slots were stored.
    _insert_packet(conn, 1000, FRAME_COUNT2)
    _insert_rr(conn, 1000, 100.0)
    _insert_rr(conn, 1000, 200.0)
    _insert_packet(conn, 2000, FRAME_COUNT3)
    _insert_rr(conn, 2000, 100.0)
    _insert_rr(conn, 2000, 200.0)
    _insert_packet(conn, 3000, FRAME_COUNT4)
    _insert_rr(conn, 3000, 100.0)
    _insert_rr(conn, 3000, 200.0)
    conn.commit()

    stats = backfill_rr_slots(conn)

    assert stats["packets_with_missing_rr"] == 2
    assert stats["rr_inserted"] == 3  # slot 3 of count=3, slots 3-4 of count=4
    assert stats["ambiguous_skipped"] == 0
    assert _rr_rows(conn, 1000) == [100.0, 200.0]
    assert _rr_rows(conn, 2000) == [100.0, 200.0, 300.0]
    assert _rr_rows(conn, 3000) == [100.0, 200.0, 300.0, 400.0]


def test_backfill_is_idempotent(tmp_path):
    conn = init_db(tmp_path / "test.db")
    _insert_packet(conn, 2000, FRAME_COUNT3)
    _insert_rr(conn, 2000, 100.0)
    _insert_rr(conn, 2000, 200.0)
    conn.commit()

    first = backfill_rr_slots(conn)
    second = backfill_rr_slots(conn)

    assert first["rr_inserted"] == 1
    assert second["rr_inserted"] == 0
    assert _rr_rows(conn, 2000) == [100.0, 200.0, 300.0]


def test_backfill_dry_run_writes_nothing(tmp_path):
    conn = init_db(tmp_path / "test.db")
    _insert_packet(conn, 2000, FRAME_COUNT3)
    _insert_rr(conn, 2000, 100.0)
    _insert_rr(conn, 2000, 200.0)
    conn.commit()

    stats = backfill_rr_slots(conn, dry_run=True)

    assert stats["rr_inserted"] == 1
    assert _rr_rows(conn, 2000) == [100.0, 200.0]


def test_backfill_skips_ambiguous_shared_timestamp(tmp_path):
    conn = init_db(tmp_path / "test.db")
    # Two RR-bearing packets at the same millisecond make per-timestamp row
    # accounting ambiguous — the backfill must refuse to guess.
    _insert_packet(conn, 2000, FRAME_COUNT3)
    _insert_packet(conn, 2000, FRAME_COUNT4)
    _insert_rr(conn, 2000, 100.0)
    _insert_rr(conn, 2000, 200.0)
    _insert_rr(conn, 2000, 100.0)
    _insert_rr(conn, 2000, 200.0)
    conn.commit()

    stats = backfill_rr_slots(conn)

    assert stats["ambiguous_skipped"] == 2
    assert stats["rr_inserted"] == 0
    assert len(_rr_rows(conn, 2000)) == 4
