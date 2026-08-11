"""Metadata-only prospective exercise library with reviewed promotion (D60/D61).

External entries are discovery/reference material. Importing one never creates a
canonical exercise. A separate explicit review may link an existing canonical
exercise, deliberately create a new one, or reject the candidate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from whoop_local.history_import.strong import normalize_exercise_name

SOURCE = "hasaneyldrm_exercises_dataset"
SOURCE_URL = "https://github.com/hasaneyldrm/exercises-dataset"
SOURCE_LICENSE = "MIT (non-media data and instruction text only)"
ALLOWED_REVIEW_ACTIONS = {"approve_existing", "approve_new", "reject"}


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return " ".join(value.split())


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text when provided")
    cleaned = " ".join(value.split())
    return cleaned or None


def read_dataset(json_path: str | Path) -> list[dict[str, Any]]:
    """Read and validate the supported dataset before performing any writes."""
    with open(json_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("Exercise dataset must be a JSON array")

    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(payload, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"record {index} must be an object")
        external_id = _required_text(raw.get("id"), f"record {index}.id")
        if external_id in seen_ids:
            raise ValueError(f"duplicate external exercise id {external_id!r}")
        seen_ids.add(external_id)
        name = _required_text(raw.get("name"), f"record {index}.name")

        secondary = raw.get("secondary_muscles", [])
        if not isinstance(secondary, list) or any(
            not isinstance(item, str) or not item.strip() for item in secondary
        ):
            raise ValueError(f"record {index}.secondary_muscles must be a list of strings")

        instructions = raw.get("instructions", {})
        steps = raw.get("instruction_steps", {})
        if not isinstance(instructions, dict) or not isinstance(steps, dict):
            raise ValueError(f"record {index} instructions must be language-keyed objects")
        instructions_en = _optional_text(instructions.get("en"), f"record {index}.instructions.en")
        steps_en = steps.get("en", [])
        if not isinstance(steps_en, list) or any(
            not isinstance(item, str) or not item.strip() for item in steps_en
        ):
            raise ValueError(f"record {index}.instruction_steps.en must be a list of strings")

        record = {
            "source_external_id": external_id,
            "name": name,
            "normalized_name": normalize_exercise_name(name),
            "body_part": _optional_text(raw.get("body_part"), f"record {index}.body_part"),
            "equipment": _optional_text(raw.get("equipment"), f"record {index}.equipment"),
            "target": _optional_text(raw.get("target"), f"record {index}.target"),
            "muscle_group": _optional_text(raw.get("muscle_group"), f"record {index}.muscle_group"),
            "secondary_muscles": [" ".join(item.split()) for item in secondary],
            "instructions_en": instructions_en,
            "instruction_steps_en": [" ".join(item.split()) for item in steps_en],
        }
        record["content_hash"] = hashlib.sha256(
            json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        records.append(record)
    return records


def import_dataset(conn: sqlite3.Connection, json_path: str | Path) -> dict[str, int]:
    """Idempotently import non-media metadata without altering review decisions."""
    records = read_dataset(json_path)
    imported_at = int(time.time() * 1000)
    counts = {
        "records": len(records),
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "exact_catalog_candidates": 0,
    }
    try:
        conn.execute("BEGIN")
        for record in records:
            existing = conn.execute(
                "SELECT content_hash FROM external_exercises WHERE source=? AND source_external_id=?",
                (SOURCE, record["source_external_id"]),
            ).fetchone()
            if existing is None:
                counts["inserted"] += 1
            elif existing[0] == record["content_hash"]:
                counts["unchanged"] += 1
            else:
                counts["updated"] += 1
            if conn.execute(
                "SELECT 1 FROM exercise_catalog WHERE normalized_name=?",
                (record["normalized_name"],),
            ).fetchone():
                counts["exact_catalog_candidates"] += 1
            conn.execute(
                "INSERT INTO external_exercises "
                "(source, source_external_id, name, normalized_name, body_part, equipment, target, "
                "muscle_group, secondary_muscles_json, instructions_en, instruction_steps_json, "
                "source_url, source_license, content_hash, imported_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source, source_external_id) DO UPDATE SET "
                "name=excluded.name, normalized_name=excluded.normalized_name, body_part=excluded.body_part, "
                "equipment=excluded.equipment, target=excluded.target, muscle_group=excluded.muscle_group, "
                "secondary_muscles_json=excluded.secondary_muscles_json, "
                "instructions_en=excluded.instructions_en, instruction_steps_json=excluded.instruction_steps_json, "
                "source_url=excluded.source_url, source_license=excluded.source_license, "
                "content_hash=excluded.content_hash, imported_at=excluded.imported_at",
                (
                    SOURCE,
                    record["source_external_id"],
                    record["name"],
                    record["normalized_name"],
                    record["body_part"],
                    record["equipment"],
                    record["target"],
                    record["muscle_group"],
                    json.dumps(record["secondary_muscles"], ensure_ascii=False),
                    record["instructions_en"],
                    json.dumps(record["instruction_steps_en"], ensure_ascii=False),
                    SOURCE_URL,
                    SOURCE_LICENSE,
                    record["content_hash"],
                    imported_at,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return counts


def _entry_dict(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    keys = (
        "id",
        "source_external_id",
        "name",
        "body_part",
        "equipment",
        "target",
        "muscle_group",
        "secondary_muscles_json",
        "instructions_en",
        "instruction_steps_json",
        "source_url",
        "source_license",
        "review_status",
        "canonical_exercise_id",
        "canonical_name",
        "review_notes",
    )
    result = dict(zip(keys, row))
    result["secondary_muscles"] = json.loads(result.pop("secondary_muscles_json"))
    result["instruction_steps"] = json.loads(result.pop("instruction_steps_json"))
    result["review_status"] = result["review_status"] or "unreviewed"
    return result


ENTRY_SELECT = (
    "SELECT e.id, e.source_external_id, e.name, e.body_part, e.equipment, e.target, e.muscle_group, "
    "e.secondary_muscles_json, e.instructions_en, e.instruction_steps_json, e.source_url, e.source_license, "
    "r.status, r.exercise_id, c.canonical_name, r.review_notes "
    "FROM external_exercises e "
    "LEFT JOIN external_exercise_reviews r ON r.external_exercise_id=e.id "
    "LEFT JOIN exercise_catalog c ON c.id=r.exercise_id "
)


def get_entry(conn: sqlite3.Connection, source_external_id: str) -> dict[str, Any]:
    row = conn.execute(
        ENTRY_SELECT + "WHERE e.source=? AND e.source_external_id=?",
        (SOURCE, source_external_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"External exercise {source_external_id!r} was not found")
    return _entry_dict(row)


def search_entries(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    equipment: str | None = None,
    target: str | None = None,
    body_part: str | None = None,
    review_status: str = "all",
    limit: int = 20,
) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if review_status not in {"all", "unreviewed", "approved", "rejected"}:
        raise ValueError("review_status must be all, unreviewed, approved, or rejected")
    where = ["e.source=?"]
    params: list[Any] = [SOURCE]
    if query and query.strip():
        where.append("e.normalized_name LIKE ?")
        params.append(f"%{normalize_exercise_name(query)}%")
    for column, value in (("equipment", equipment), ("target", target), ("body_part", body_part)):
        if value and value.strip():
            where.append(f"LOWER(e.{column})=LOWER(?)")
            params.append(value.strip())
    if review_status == "unreviewed":
        where.append("r.status IS NULL")
    elif review_status == "approved":
        where.append("r.status IN ('approved_existing', 'approved_new')")
    elif review_status == "rejected":
        where.append("r.status='rejected'")
    params.append(limit)
    rows = conn.execute(
        ENTRY_SELECT
        + "WHERE "
        + " AND ".join(where)
        + " ORDER BY e.name, e.source_external_id LIMIT ?",
        params,
    ).fetchall()
    return [_entry_dict(row) for row in rows]


def review_entry(
    conn: sqlite3.Connection,
    source_external_id: str,
    action: str,
    *,
    canonical_name: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record one immutable explicit review and optionally promote/link it."""
    if action not in ALLOWED_REVIEW_ACTIONS:
        raise ValueError(f"action must be one of {sorted(ALLOWED_REVIEW_ACTIONS)}")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("notes must be text when provided")
    if canonical_name is not None and not isinstance(canonical_name, str):
        raise ValueError("canonical_name must be text when provided")
    entry = conn.execute(
        "SELECT id, name FROM external_exercises WHERE source=? AND source_external_id=?",
        (SOURCE, source_external_id),
    ).fetchone()
    if entry is None:
        raise ValueError(f"External exercise {source_external_id!r} was not found")
    entry_id, external_name = entry
    if conn.execute(
        "SELECT 1 FROM external_exercise_reviews WHERE external_exercise_id=?", (entry_id,)
    ).fetchone():
        raise ValueError(f"External exercise {source_external_id!r} has already been reviewed")

    now = int(time.time() * 1000)
    exercise_id: int | None = None
    resolved_name: str | None = None
    review_status = {
        "approve_existing": "approved_existing",
        "approve_new": "approved_new",
        "reject": "rejected",
    }[action]
    try:
        conn.execute("BEGIN")
        if action == "approve_existing":
            if not canonical_name:
                raise ValueError("approve_existing requires canonical_name")
            row = conn.execute(
                "SELECT id, canonical_name FROM exercise_catalog WHERE normalized_name=?",
                (normalize_exercise_name(canonical_name),),
            ).fetchone()
            if row is None:
                raise ValueError(f"Canonical exercise {canonical_name!r} was not found")
            exercise_id, resolved_name = row
        elif action == "approve_new":
            if not canonical_name:
                raise ValueError("approve_new requires an explicit canonical_name")
            resolved_name = " ".join(canonical_name.split())
            normalized = normalize_exercise_name(resolved_name)
            if conn.execute(
                "SELECT 1 FROM exercise_catalog WHERE normalized_name=?", (normalized,)
            ).fetchone():
                raise ValueError(
                    f"Canonical exercise {resolved_name!r} already exists; use approve_existing"
                )
            exercise_id = conn.execute(
                "INSERT INTO exercise_catalog (canonical_name, normalized_name, created_at) VALUES (?, ?, ?)",
                (resolved_name, normalized, now),
            ).lastrowid
        elif canonical_name is not None:
            raise ValueError("reject does not accept canonical_name")

        conn.execute(
            "INSERT INTO external_exercise_reviews "
            "(external_exercise_id, status, exercise_id, review_notes, reviewed_at) VALUES (?, ?, ?, ?, ?)",
            (entry_id, review_status, exercise_id, notes, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "source_external_id": source_external_id,
        "external_name": external_name,
        "status": review_status,
        "canonical_exercise_id": exercise_id,
        "canonical_name": resolved_name,
    }
