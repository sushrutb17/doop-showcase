import json
import stat
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pytest

from whoop_local.db.schema import SCHEMA_VERSION, init_db
from whoop_local.report_ingestion.cli import main as report_cli
from whoop_local.report_ingestion.service import (
    apply_report,
    audit_import,
    list_imports,
    load_mapping,
    preview_report,
)
from whoop_local.report_ingestion.xlsx import XlsxFormatError

HEADERS = ["Record ID", "Report Date", "Value", "Notes"]
ROWS = [
    ["row-001", "2037-07-03", 12.5, "synthetic"],
    ["row-002", "2037-07-19", 18, None],
]


def _column_name(number):
    output = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        output = chr(ord("A") + remainder) + output
    return output


def _cell_xml(reference, value):
    if isinstance(value, dict) and "formula" in value:
        cached = value.get("cached", 0)
        return f"<c r={quoteattr(reference)}><f>{escape(value['formula'])}</f><v>{cached}</v></c>"
    if value is None:
        return ""
    if isinstance(value, bool):
        return f'<c r={quoteattr(reference)} t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        return f"<c r={quoteattr(reference)}><v>{value}</v></c>"
    return f'<c r={quoteattr(reference)} t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def _workbook(path: Path, rows, *, sheet="Monthly Report", headers=HEADERS):
    all_rows = [headers, *rows]
    sheet_rows = []
    for row_number, values in enumerate(all_rows, start=1):
        cells = "".join(
            _cell_xml(f"{_column_name(column)}{row_number}", value)
            for column, value in enumerate(values, start=1)
        )
        sheet_rows.append(f'<row r="{row_number}">{cells}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name={quoteattr(sheet)} sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.worksheet+xml"/></Types>'
    )
    root_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="xl/workbook.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return path


def _mapping(tmp_path, **updates):
    payload = {
        "schema_version": 1,
        "mapping_id": "synthetic_monthly_measurements",
        "mapping_version": 1,
        "report_type": "synthetic_measurements",
        "sheet": "Monthly Report",
        "header_row": 1,
        "fields": [
            {"name": "record_id", "header": "Record ID", "type": "text", "required": True},
            {
                "name": "report_date",
                "header": "Report Date",
                "type": "date",
                "required": True,
            },
            {"name": "value", "header": "Value", "type": "number", "required": True},
            {"name": "notes", "header": "Notes", "type": "text", "required": False},
        ],
        "key_fields": ["record_id"],
        "period_field": "report_date",
        "ignored_columns": [],
    }
    payload.update(updates)
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _prepared(tmp_path, rows=ROWS):
    workbook = _workbook(tmp_path / "report.xlsx", rows)
    mapping = _mapping(tmp_path)
    conn = init_db(tmp_path / "doop.db")
    return conn, workbook, mapping


def test_preview_is_read_only_and_hides_rows_by_default(tmp_path):
    workbook = _workbook(tmp_path / "report.xlsx", ROWS)
    mapping = _mapping(tmp_path)
    preview = preview_report(None, workbook, mapping, "2037-07")

    assert preview.valid
    assert preview.to_dict()["row_count"] == 2
    assert "rows" not in preview.to_dict()
    assert preview.to_dict(include_rows=True)["rows"][0]["data"] == {
        "notes": "synthetic",
        "record_id": "row-001",
        "report_date": "2037-07-03",
        "value": 12.5,
    }


def test_cli_preview_does_not_create_database(tmp_path, capsys):
    workbook = _workbook(tmp_path / "report.xlsx", ROWS)
    mapping = _mapping(tmp_path)
    db_path = tmp_path / "absent.db"

    exit_code = report_cli(
        [
            "--db",
            str(db_path),
            "preview",
            "--xlsx",
            str(workbook),
            "--mapping",
            str(mapping),
            "--period",
            "2037-07",
        ]
    )

    assert exit_code == 0
    assert not db_path.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "valid"


def test_cli_rejected_apply_does_not_initialize_database(tmp_path, capsys):
    workbook = _workbook(tmp_path / "report.xlsx", ROWS)
    mapping = _mapping(tmp_path)
    db_path = tmp_path / "absent.db"

    exit_code = report_cli(
        [
            "--db",
            str(db_path),
            "--store-dir",
            str(tmp_path / "reports"),
            "apply",
            "--xlsx",
            str(workbook),
            "--mapping",
            str(mapping),
            "--period",
            "2037-07",
            "--expected-sha256",
            "0" * 64,
        ]
    )

    assert exit_code == 2
    assert not db_path.exists()
    assert not (tmp_path / "reports").exists()
    assert "does not match the preview" in json.loads(capsys.readouterr().err)["error"]


def test_apply_stores_owner_only_evidence_and_audits(tmp_path):
    conn, workbook, mapping = _prepared(tmp_path)
    store = tmp_path / "private-reports"
    preview = preview_report(conn, workbook, mapping, "2037-07")

    result = apply_report(
        conn,
        workbook,
        mapping,
        "2037-07",
        expected_sha256=preview.content_sha256,
        store_dir=store,
    )
    audit = audit_import(conn, result["import_id"], store_dir=store)
    stored = store / result["stored_relpath"]

    assert result["status"] == "applied"
    assert audit["ok"]
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert stat.S_IMODE(stored.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert conn.execute("SELECT COUNT(*) FROM monthly_report_rows").fetchone()[0] == 2
    assert (
        conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        == SCHEMA_VERSION
        == "14"
    )


def test_duplicate_is_noop_and_correction_preserves_revision_lineage(tmp_path):
    conn, workbook, mapping = _prepared(tmp_path)
    store = tmp_path / "reports"
    preview = preview_report(conn, workbook, mapping, "2037-07")
    first = apply_report(
        conn,
        workbook,
        mapping,
        "2037-07",
        expected_sha256=preview.content_sha256,
        store_dir=store,
    )
    duplicate = apply_report(
        conn,
        workbook,
        mapping,
        "2037-07",
        expected_sha256=preview.content_sha256,
        store_dir=store,
    )
    assert duplicate["status"] == "duplicate"
    assert not duplicate["database_changed"]
    assert conn.execute("SELECT COUNT(*) FROM monthly_report_imports").fetchone()[0] == 1

    corrected = _workbook(
        tmp_path / "corrected.xlsx",
        [ROWS[0], ["row-002", "2037-07-19", 19, "corrected"]],
    )
    corrected_preview = preview_report(conn, corrected, mapping, "2037-07")
    assert corrected_preview.active_import["import_id"] == first["import_id"]
    with pytest.raises(ValueError, match="--supersedes"):
        apply_report(
            conn,
            corrected,
            mapping,
            "2037-07",
            expected_sha256=corrected_preview.content_sha256,
            store_dir=store,
        )

    second = apply_report(
        conn,
        corrected,
        mapping,
        "2037-07",
        expected_sha256=corrected_preview.content_sha256,
        store_dir=store,
        supersedes_import_id=first["import_id"],
    )
    imports = list_imports(conn, report_type="synthetic_measurements", report_period="2037-07")
    first_audit = audit_import(conn, first["import_id"], store_dir=store)
    second_audit = audit_import(conn, second["import_id"], store_dir=store)

    assert second["revision"] == 2
    assert [item["status"] for item in imports] == ["active", "superseded"]
    assert first_audit["ok"] and second_audit["ok"]
    assert first_audit["lineage"]["superseded_by_import_id"] == second["import_id"]
    assert second_audit["lineage"]["supersedes_import_id"] == first["import_id"]
    assert conn.execute("SELECT COUNT(*) FROM monthly_report_rows").fetchone()[0] == 4
    assert len(list(store.rglob("*.xlsx"))) == 2


def test_audit_detects_normalized_row_tampering(tmp_path):
    conn, workbook, mapping = _prepared(tmp_path)
    store = tmp_path / "reports"
    preview = preview_report(conn, workbook, mapping, "2037-07")
    applied = apply_report(
        conn,
        workbook,
        mapping,
        "2037-07",
        expected_sha256=preview.content_sha256,
        store_dir=store,
    )
    conn.execute(
        "UPDATE monthly_report_rows SET data_json=? WHERE import_id=? AND source_row_number=2",
        ('{"record_id":"tampered"}', applied["import_id"]),
    )
    conn.commit()

    audit = audit_import(conn, applied["import_id"], store_dir=store)

    assert not audit["ok"]
    assert not audit["checks"]["row_hashes_match"]
    assert not audit["checks"]["row_keys_match"]


def test_apply_rejects_file_changed_after_preview_without_writes(tmp_path):
    conn, workbook, mapping = _prepared(tmp_path)
    preview = preview_report(conn, workbook, mapping, "2037-07")
    _workbook(workbook, [["row-003", "2037-07-21", 5, None]])

    with pytest.raises(ValueError, match="does not match the preview"):
        apply_report(
            conn,
            workbook,
            mapping,
            "2037-07",
            expected_sha256=preview.content_sha256,
            store_dir=tmp_path / "reports",
        )

    assert conn.execute("SELECT COUNT(*) FROM monthly_report_imports").fetchone()[0] == 0
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    [
        ([ROWS[0], ROWS[0]], "duplicate_row_key"),
        ([["row-001", "2037-06-30", 1, None]], "period_mismatch"),
        ([["row-001", "2037-07-01", {"formula": "1+1", "cached": 2}, None]], "formula_not_allowed"),
    ],
)
def test_actionable_row_validation_errors(tmp_path, rows, expected_code):
    conn, workbook, mapping = _prepared(tmp_path, rows)
    preview = preview_report(conn, workbook, mapping, "2037-07")

    assert not preview.valid
    assert expected_code in {issue.code for issue in preview.issues}
    assert conn.execute("SELECT COUNT(*) FROM monthly_report_imports").fetchone()[0] == 0


def test_undeclared_columns_fail_until_explicitly_ignored(tmp_path):
    workbook = _workbook(
        tmp_path / "report.xlsx",
        [[*ROWS[0], {"formula": "1+1", "cached": 2}]],
        headers=[*HEADERS, "Known Unused"],
    )
    conn = init_db(tmp_path / "doop.db")
    mapping = _mapping(tmp_path)
    undeclared = preview_report(conn, workbook, mapping, "2037-07")
    ignored_mapping = _mapping(tmp_path, ignored_columns=["Known Unused"])
    ignored = preview_report(conn, workbook, ignored_mapping, "2037-07")

    assert "unexpected_header" in {issue.code for issue in undeclared.issues}
    assert "formula_not_allowed" in {issue.code for issue in ignored.issues}


def test_data_below_blank_header_is_rejected(tmp_path):
    workbook = _workbook(
        tmp_path / "report.xlsx",
        [[*ROWS[0], "undeclared"]],
        headers=[*HEADERS, None],
    )
    preview = preview_report(None, workbook, _mapping(tmp_path), "2037-07")

    assert "unlabeled_column" in {issue.code for issue in preview.issues}


def test_missing_header_and_malformed_workbook_are_rejected(tmp_path):
    mapping = _mapping(tmp_path)
    malformed = tmp_path / "malformed.xlsx"
    malformed.write_bytes(b"not a zip")

    with pytest.raises(XlsxFormatError, match="valid XLSX"):
        preview_report(None, malformed, mapping, "2037-07")

    missing = _workbook(
        tmp_path / "missing.xlsx",
        [["row-1", "2037-07-01", None]],
        headers=["Record ID", "Report Date", "Notes"],
    )
    preview = preview_report(None, missing, mapping, "2037-07")
    assert "missing_header" in {issue.code for issue in preview.issues}


def test_mapping_rejects_unknown_configuration_instead_of_guessing(tmp_path):
    mapping = _mapping(tmp_path, team_specific_magic=True)
    with pytest.raises(ValueError, match="unsupported keys"):
        load_mapping(mapping)


def test_mapping_content_change_requires_a_version_increment(tmp_path):
    conn, workbook, mapping = _prepared(tmp_path)
    store = tmp_path / "reports"
    preview = preview_report(conn, workbook, mapping, "2037-07")
    apply_report(
        conn,
        workbook,
        mapping,
        "2037-07",
        expected_sha256=preview.content_sha256,
        store_dir=store,
    )

    changed_mapping = _mapping(tmp_path, timezone="UTC")
    changed = preview_report(conn, workbook, changed_mapping, "2037-08")

    assert "mapping_version_conflict" in {issue.code for issue in changed.issues}
