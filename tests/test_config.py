import stat
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from whoop_local.config import default_compute_date, local_day_window_utc_ms
from whoop_local.db.schema import init_db


def test_local_day_window_is_noon_to_noon_utc():
    start_ms, end_ms = local_day_window_utc_ms(date(2037, 7, 2), tz=ZoneInfo("UTC"))
    start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    assert start == datetime(2037, 7, 1, 12, tzinfo=UTC)
    assert end == datetime(2037, 7, 2, 12, tzinfo=UTC)


def test_local_day_window_tracks_spring_dst_transition():
    # America/New_York switches to DST on the synthetic 2037-03-08 fixture.
    # Local noon-to-noon therefore spans 23 real hours for this window.
    tz = ZoneInfo("America/New_York")
    start_ms, end_ms = local_day_window_utc_ms(date(2037, 3, 8), tz=tz)
    assert end_ms - start_ms == 23 * 60 * 60 * 1000


def test_local_day_window_tracks_fall_dst_transition():
    # The synthetic 2037-11-01 window repeats an hour when daylight time ends.
    tz = ZoneInfo("America/New_York")
    start_ms, end_ms = local_day_window_utc_ms(date(2037, 11, 1), tz=tz)
    assert end_ms - start_ms == 25 * 60 * 60 * 1000


def test_default_compute_date_before_noon_is_yesterday():
    tz = ZoneInfo("UTC")
    now = datetime(2037, 7, 3, 8, 0, tzinfo=tz)
    assert default_compute_date(tz, now=now) == date(2037, 7, 2)


def test_default_compute_date_after_noon_is_today():
    tz = ZoneInfo("UTC")
    now = datetime(2037, 7, 3, 14, 0, tzinfo=tz)
    assert default_compute_date(tz, now=now) == date(2037, 7, 3)


def test_init_db_secures_a_new_private_parent(tmp_path):
    parent = tmp_path / "new-private-data"
    connection = init_db(parent / "whoop.db")
    connection.close()

    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
