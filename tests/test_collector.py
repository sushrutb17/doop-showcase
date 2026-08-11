from dataclasses import dataclass

from tests.synthetic_frames import battery_event
from whoop_local.capture.collector import WATCHDOG_TIMEOUT_S, Collector, is_stale
from whoop_local.capture.proprietary_protocol import CHAR_EVENTS_FROM_STRAP
from whoop_local.db.schema import init_db


@dataclass
class _FakeCharacteristic:
    uuid: str


def test_is_stale_false_when_recent():
    assert not is_stale(last_notification_monotonic=100.0, now_monotonic=101.0, timeout_s=60.0)


def test_is_stale_true_when_past_timeout():
    assert is_stale(last_notification_monotonic=100.0, now_monotonic=200.0, timeout_s=60.0)


def test_is_stale_boundary_is_exclusive():
    # exactly at the timeout should not yet be considered stale
    assert not is_stale(last_notification_monotonic=100.0, now_monotonic=160.0, timeout_s=60.0)
    assert is_stale(last_notification_monotonic=100.0, now_monotonic=160.001, timeout_s=60.0)


def test_default_timeout_is_used_when_not_specified():
    now = 1000.0
    assert not is_stale(now - (WATCHDOG_TIMEOUT_S - 1), now)
    assert is_stale(now - (WATCHDOG_TIMEOUT_S + 1), now)


def test_reconcile_closes_orphaned_gap_at_first_capture_after_it(tmp_path):
    conn = init_db(tmp_path / "test.db")
    collector = Collector(conn, identifier="fake", source_host="mac")

    conn.execute(
        "INSERT INTO capture_gaps (source_host, gap_start_utc, reason) VALUES ('mac', 1000, 'disconnected')"
    )
    conn.execute(
        "INSERT INTO hr_samples (ts_utc, hr_bpm, source_host, source) VALUES (5000, 60, 'mac', 'whoop')"
    )
    conn.commit()

    collector._reconcile_open_gaps()

    row = conn.execute("SELECT gap_end_utc FROM capture_gaps").fetchone()
    assert row[0] == 5000
    assert collector._gap_open_id is None


def test_reconcile_adopts_open_gap_with_no_capture_after_it(tmp_path):
    conn = init_db(tmp_path / "test.db")
    collector = Collector(conn, identifier="fake", source_host="mac")

    cur = conn.execute(
        "INSERT INTO capture_gaps (source_host, gap_start_utc, reason) VALUES ('mac', 1000, 'disconnected')"
    )
    conn.commit()
    gap_id = cur.lastrowid

    collector._reconcile_open_gaps()

    assert collector._gap_open_id == gap_id
    row = conn.execute("SELECT gap_end_utc FROM capture_gaps WHERE id = ?", (gap_id,)).fetchone()
    assert row[0] is None  # stays open until the next successful connect closes it

    # the adopted gap closes through the normal path
    collector._close_gap()
    row = conn.execute("SELECT gap_end_utc FROM capture_gaps WHERE id = ?", (gap_id,)).fetchone()
    assert row[0] is not None


def test_reconcile_ignores_other_hosts_and_closed_gaps(tmp_path):
    conn = init_db(tmp_path / "test.db")
    collector = Collector(conn, identifier="fake", source_host="mac")

    conn.execute(
        "INSERT INTO capture_gaps (source_host, gap_start_utc, reason) VALUES ('pi', 1000, 'disconnected')"
    )
    conn.execute(
        "INSERT INTO capture_gaps (source_host, gap_start_utc, gap_end_utc, reason) "
        "VALUES ('mac', 2000, 3000, 'disconnected')"
    )
    conn.commit()

    collector._reconcile_open_gaps()

    assert collector._gap_open_id is None
    rows = conn.execute("SELECT source_host, gap_end_utc FROM capture_gaps ORDER BY id").fetchall()
    assert rows[0] == ("pi", None)  # untouched: belongs to another host
    assert rows[1] == ("mac", 3000)  # untouched: already closed


def test_on_notification_decodes_battery_event_from_events_channel(tmp_path):
    conn = init_db(tmp_path / "test.db")
    collector = Collector(conn, identifier="fake", source_host="mac")

    frame = battery_event(480)
    collector._on_notification(_FakeCharacteristic(uuid=CHAR_EVENTS_FROM_STRAP), bytearray(frame))

    row = conn.execute("SELECT raw_value FROM battery_samples").fetchone()
    assert row is not None
    assert row[0] == 480
