import sqlite3
from datetime import date

from tools.create_film_seed import (
    DEFAULT_FILM_DATE,
    SYNTHETIC_METRICS_CONFIG,
    create_film_seed,
)
from whoop_local.compute.config import strain_hrmax_bpm


def test_film_seed_is_isolated_computed_and_relay_ready(tmp_path):
    output = tmp_path / "film-seed.db"
    summary = create_film_seed(output, DEFAULT_FILM_DATE)

    assert output.exists()
    assert summary["raw_packets"] == 8 * 49
    assert summary["metrics_days"] == 8
    assert summary["confirmed_sessions"] == 1
    assert summary["recovery_computed"] is True
    assert summary["strain_proxy"] > 0
    assert summary["coverage_pct"] == 1.0
    assert summary["sleep_minutes"] >= 180
    assert summary["provisional"] is False
    assert "Foundation A" in summary["guidance_relay"]["text"]
    assert "Fictional template" in summary["guidance_relay"]["text"]
    assert "Aerobic base" in summary["weekly_relay"]["text"]

    conn = sqlite3.connect(output)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_film_seed_refuses_to_overwrite(tmp_path):
    output = tmp_path / "film-seed.db"
    output.write_bytes(b"existing")
    try:
        create_film_seed(output, DEFAULT_FILM_DATE)
    except FileExistsError:
        pass
    else:
        raise AssertionError("seed creation must refuse existing output")


def test_film_seed_summary_is_repeatable_and_uses_age_independent_metrics(tmp_path):
    first = create_film_seed(tmp_path / "first.db", DEFAULT_FILM_DATE)
    second = create_film_seed(tmp_path / "second.db", DEFAULT_FILM_DATE)

    assert first == second
    assert DEFAULT_FILM_DATE == date(2037, 7, 6)
    assert SYNTHETIC_METRICS_CONFIG.strain_hrmax_age_coefficient == 0
    assert SYNTHETIC_METRICS_CONFIG.strain_hrmax_intercept == 190
    assert strain_hrmax_bpm(DEFAULT_FILM_DATE, SYNTHETIC_METRICS_CONFIG) == 190
