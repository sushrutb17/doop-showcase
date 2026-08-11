import json

import pytest

from whoop_local.db.schema import SCHEMA_VERSION, init_db
from whoop_local.exercise_library.service import (
    get_entry,
    import_dataset,
    read_dataset,
    review_entry,
    search_entries,
)
from whoop_local.history_import.strong import normalize_exercise_name

RECORDS = [
    {
        "id": "0001",
        "name": "barbell bench press",
        "body_part": "chest",
        "equipment": "barbell",
        "target": "pectorals",
        "muscle_group": "triceps",
        "secondary_muscles": ["triceps", "delts"],
        "instructions": {"en": "Brace, lower under control, and press."},
        "instruction_steps": {"en": ["Brace.", "Lower under control.", "Press."]},
        "image": "images/restricted.jpg",
        "gif_url": "videos/restricted.gif",
    },
    {
        "id": "0002",
        "name": "cable neutral grip row",
        "body_part": "back",
        "equipment": "cable",
        "target": "upper back",
        "muscle_group": "lats",
        "secondary_muscles": ["biceps"],
        "instructions": {"en": "Keep the torso stable and row smoothly."},
        "instruction_steps": {"en": ["Set a neutral grip.", "Row to the torso."]},
        "image": "images/restricted2.jpg",
        "gif_url": "videos/restricted2.gif",
    },
]


def _dataset(tmp_path, records=None):
    path = tmp_path / "exercises.json"
    path.write_text(json.dumps(RECORDS if records is None else records), encoding="utf-8")
    return path


def _conn(tmp_path):
    return init_db(tmp_path / "test.db")


def _add_catalog(conn, name):
    conn.execute(
        "INSERT INTO exercise_catalog (canonical_name, normalized_name, created_at) VALUES (?, ?, 1)",
        (name, normalize_exercise_name(name)),
    )
    conn.commit()


def test_import_is_metadata_only_and_idempotent(tmp_path):
    conn = _conn(tmp_path)
    _add_catalog(conn, "Barbell Bench Press")

    first = import_dataset(conn, _dataset(tmp_path))
    second = import_dataset(conn, _dataset(tmp_path))

    assert first == {
        "records": 2,
        "inserted": 2,
        "updated": 0,
        "unchanged": 0,
        "exact_catalog_candidates": 1,
    }
    assert second["inserted"] == second["updated"] == 0
    assert second["unchanged"] == 2
    assert conn.execute("SELECT COUNT(*) FROM external_exercises").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM exercise_catalog").fetchone()[0] == 1
    columns = {row[1] for row in conn.execute("PRAGMA table_info(external_exercises)")}
    assert "image" not in columns
    assert "gif_url" not in columns
    assert (
        conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        == SCHEMA_VERSION
    )


def test_import_validates_all_records_before_writing(tmp_path):
    conn = _conn(tmp_path)
    invalid = RECORDS + [{"id": "0003", "name": ""}]

    with pytest.raises(ValueError, match="record 3.name"):
        import_dataset(conn, _dataset(tmp_path, invalid))

    assert conn.execute("SELECT COUNT(*) FROM external_exercises").fetchone()[0] == 0


def test_duplicate_external_ids_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="duplicate external exercise id"):
        read_dataset(_dataset(tmp_path, [RECORDS[0], {**RECORDS[1], "id": "0001"}]))


def test_search_and_show_expose_provenance_and_filters(tmp_path):
    conn = _conn(tmp_path)
    import_dataset(conn, _dataset(tmp_path))

    results = search_entries(conn, query="neutral grip", equipment="CABLE", body_part="back")
    assert [row["source_external_id"] for row in results] == ["0002"]
    assert results[0]["review_status"] == "unreviewed"
    assert results[0]["secondary_muscles"] == ["biceps"]
    shown = get_entry(conn, "0002")
    assert shown["instruction_steps"] == ["Set a neutral grip.", "Row to the torso."]
    assert shown["source_license"].startswith("MIT")


def test_review_can_link_existing_without_changing_catalog(tmp_path):
    conn = _conn(tmp_path)
    _add_catalog(conn, "Bench Press (Barbell)")
    import_dataset(conn, _dataset(tmp_path))

    result = review_entry(
        conn,
        "0001",
        "approve_existing",
        canonical_name="Bench Press (Barbell)",
        notes="Reviewed alias",
    )

    assert result["status"] == "approved_existing"
    assert result["canonical_name"] == "Bench Press (Barbell)"
    assert conn.execute("SELECT COUNT(*) FROM exercise_catalog").fetchone()[0] == 1
    assert get_entry(conn, "0001")["review_status"] == "approved_existing"


def test_review_can_deliberately_create_new_canonical_exercise(tmp_path):
    conn = _conn(tmp_path)
    import_dataset(conn, _dataset(tmp_path))

    result = review_entry(
        conn,
        "0002",
        "approve_new",
        canonical_name="Neutral-Grip Cable Row",
        notes="Intentional new joint-aware plan candidate",
    )

    assert result["status"] == "approved_new"
    row = conn.execute(
        "SELECT canonical_name FROM exercise_catalog WHERE id=?", (result["canonical_exercise_id"],)
    ).fetchone()
    assert row[0] == "Neutral-Grip Cable Row"
    approved = search_entries(conn, review_status="approved")
    assert [entry["source_external_id"] for entry in approved] == ["0002"]


def test_review_is_explicit_immutable_and_transactional(tmp_path):
    conn = _conn(tmp_path)
    _add_catalog(conn, "Neutral-Grip Cable Row")
    import_dataset(conn, _dataset(tmp_path))

    with pytest.raises(ValueError, match="already exists; use approve_existing"):
        review_entry(conn, "0002", "approve_new", canonical_name="Neutral-Grip Cable Row")
    assert conn.execute("SELECT COUNT(*) FROM external_exercise_reviews").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM exercise_catalog").fetchone()[0] == 1

    review_entry(conn, "0002", "reject", notes="Not suitable for this phase")
    assert search_entries(conn, review_status="rejected")[0]["source_external_id"] == "0002"
    with pytest.raises(ValueError, match="already been reviewed"):
        review_entry(conn, "0002", "approve_existing", canonical_name="Neutral-Grip Cable Row")


def test_search_validation(tmp_path):
    conn = _conn(tmp_path)
    import_dataset(conn, _dataset(tmp_path))
    with pytest.raises(ValueError, match="limit"):
        search_entries(conn, limit=0)
    with pytest.raises(ValueError, match="review_status"):
        search_entries(conn, review_status="maybe")
