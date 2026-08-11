"""Preview, apply, revise, and audit private monthly Excel reports."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from whoop_local.report_ingestion.xlsx import MAX_FILE_BYTES, XlsxCell, read_sheet
from whoop_local.security import secure_directory, secure_file

MAPPING_SCHEMA_VERSION = 1
PARSER_VERSION = 1
MAX_MAPPING_BYTES = 1024 * 1024
SUPPORTED_FIELD_TYPES = {"text", "integer", "number", "boolean", "date", "datetime"}
SAFE_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PERIOD_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
ROOT_MAPPING_KEYS = {
    "schema_version",
    "mapping_id",
    "mapping_version",
    "report_type",
    "sheet",
    "header_row",
    "fields",
    "key_fields",
    "period_field",
    "ignored_columns",
    "timezone",
}
FIELD_MAPPING_KEYS = {"name", "header", "type", "required"}


@dataclass(frozen=True)
class FieldMapping:
    name: str
    header: str
    field_type: str
    required: bool


@dataclass(frozen=True)
class ReportMapping:
    mapping_id: str
    mapping_version: int
    report_type: str
    sheet: str
    header_row: int
    fields: tuple[FieldMapping, ...]
    key_fields: tuple[str, ...]
    period_field: str | None
    ignored_columns: tuple[str, ...]
    timezone: str | None
    canonical_json: str
    mapping_hash: str


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    row: int | None = None
    column: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "code": self.code,
                "message": self.message,
                "row": self.row,
                "column": self.column,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class ParsedReportRow:
    source_row_number: int
    row_key: str
    row_hash: str
    data: dict[str, Any]


@dataclass(frozen=True)
class ReportPreview:
    source_path: Path
    source_filename: str
    content_sha256: str
    size_bytes: int
    report_period: str
    mapping: ReportMapping
    headers: tuple[str, ...]
    rows: tuple[ParsedReportRow, ...]
    rows_digest: str
    issues: tuple[ValidationIssue, ...]
    duplicate_import_id: str | None
    active_import: dict[str, Any] | None

    @property
    def valid(self) -> bool:
        return not self.issues

    def to_dict(self, *, include_rows: bool = False) -> dict[str, Any]:
        output: dict[str, Any] = {
            "status": "valid" if self.valid else "invalid",
            "source_filename": self.source_filename,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "report_type": self.mapping.report_type,
            "report_period": self.report_period,
            "mapping_id": self.mapping.mapping_id,
            "mapping_version": self.mapping.mapping_version,
            "mapping_hash": self.mapping.mapping_hash,
            "parser_version": PARSER_VERSION,
            "sheet": self.mapping.sheet,
            "headers": list(self.headers),
            "row_count": len(self.rows),
            "rows_digest": self.rows_digest,
            "issues": [issue.to_dict() for issue in self.issues],
            "duplicate_import_id": self.duplicate_import_id,
            "active_import": self.active_import,
            "apply_requires_expected_sha256": True,
        }
        if include_rows:
            output["rows"] = [
                {
                    "source_row_number": row.source_row_number,
                    "row_key": row.row_key,
                    "row_hash": row.row_hash,
                    "data": row.data,
                }
                for row in self.rows
            ]
        return output


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return " ".join(value.split())


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _validate_identifier(value: Any, field: str) -> str:
    text = _required_text(value, field)
    if SAFE_IDENTIFIER_RE.fullmatch(text) is None:
        raise ValueError(f"{field} must match {SAFE_IDENTIFIER_RE.pattern}")
    return text


def load_mapping(path: Path | str) -> ReportMapping:
    """Load one explicit mapping and reject unknown configuration keys."""
    mapping_path = Path(path)
    if mapping_path.is_symlink():
        raise ValueError("refusing to load a symlinked mapping")
    if not mapping_path.is_file():
        raise FileNotFoundError(mapping_path)
    if mapping_path.stat().st_size > MAX_MAPPING_BYTES:
        raise ValueError("mapping exceeds the 1 MiB input limit")
    try:
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"mapping is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("mapping must be a JSON object")
    unexpected = set(payload) - ROOT_MAPPING_KEYS
    if unexpected:
        raise ValueError(f"mapping has unsupported keys: {sorted(unexpected)}")
    if payload.get("schema_version") != MAPPING_SCHEMA_VERSION:
        raise ValueError(f"mapping.schema_version must be {MAPPING_SCHEMA_VERSION}")

    mapping_id = _validate_identifier(payload.get("mapping_id"), "mapping.mapping_id")
    report_type = _validate_identifier(payload.get("report_type"), "mapping.report_type")
    mapping_version = _positive_int(payload.get("mapping_version"), "mapping.mapping_version")
    sheet = _required_text(payload.get("sheet"), "mapping.sheet")
    header_row = _positive_int(payload.get("header_row"), "mapping.header_row")
    if header_row > 50:
        raise ValueError("mapping.header_row must be at most 50")

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError("mapping.fields must be a non-empty array")
    fields: list[FieldMapping] = []
    names: set[str] = set()
    headers: set[str] = set()
    for index, raw in enumerate(raw_fields, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"mapping.fields[{index}] must be an object")
        extra = set(raw) - FIELD_MAPPING_KEYS
        if extra:
            raise ValueError(f"mapping.fields[{index}] has unsupported keys: {sorted(extra)}")
        name = _required_text(raw.get("name"), f"mapping.fields[{index}].name")
        if FIELD_NAME_RE.fullmatch(name) is None:
            raise ValueError(f"mapping.fields[{index}].name must match {FIELD_NAME_RE.pattern}")
        header = _required_text(raw.get("header"), f"mapping.fields[{index}].header")
        field_type = _required_text(raw.get("type"), f"mapping.fields[{index}].type")
        if field_type not in SUPPORTED_FIELD_TYPES:
            raise ValueError(
                f"mapping.fields[{index}].type must be one of {sorted(SUPPORTED_FIELD_TYPES)}"
            )
        required = raw.get("required")
        if not isinstance(required, bool):
            raise ValueError(f"mapping.fields[{index}].required must be boolean")
        if name in names:
            raise ValueError(f"duplicate mapped field name {name!r}")
        if header in headers:
            raise ValueError(f"duplicate mapped header {header!r}")
        names.add(name)
        headers.add(header)
        fields.append(FieldMapping(name, header, field_type, required))

    raw_keys = payload.get("key_fields")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise ValueError("mapping.key_fields must be a non-empty array")
    key_fields: list[str] = []
    field_by_name = {field.name: field for field in fields}
    for key in raw_keys:
        if not isinstance(key, str) or key not in field_by_name:
            raise ValueError(f"mapping.key_fields contains unknown field {key!r}")
        if not field_by_name[key].required:
            raise ValueError(f"key field {key!r} must be required")
        if key in key_fields:
            raise ValueError(f"duplicate key field {key!r}")
        key_fields.append(key)

    period_field = payload.get("period_field")
    if period_field is not None:
        if not isinstance(period_field, str) or period_field not in field_by_name:
            raise ValueError("mapping.period_field must name a mapped field")
        if field_by_name[period_field].field_type != "date":
            raise ValueError("mapping.period_field must use type 'date'")
        if not field_by_name[period_field].required:
            raise ValueError("mapping.period_field must be required")

    ignored = payload.get("ignored_columns", [])
    if not isinstance(ignored, list) or any(
        not isinstance(item, str) or not item.strip() for item in ignored
    ):
        raise ValueError("mapping.ignored_columns must be an array of non-empty strings")
    ignored_columns = tuple(" ".join(item.split()) for item in ignored)
    if len(set(ignored_columns)) != len(ignored_columns):
        raise ValueError("mapping.ignored_columns contains duplicates")
    overlap = set(ignored_columns) & headers
    if overlap:
        raise ValueError(f"ignored columns overlap mapped headers: {sorted(overlap)}")

    timezone = payload.get("timezone")
    if timezone is not None:
        timezone = _required_text(timezone, "mapping.timezone")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"mapping.timezone is not an available IANA zone: {timezone}") from exc
    if any(field.field_type == "datetime" for field in fields) and timezone is None:
        raise ValueError("mapping.timezone is required when a datetime field is mapped")

    canonical_payload = {
        "schema_version": MAPPING_SCHEMA_VERSION,
        "mapping_id": mapping_id,
        "mapping_version": mapping_version,
        "report_type": report_type,
        "sheet": sheet,
        "header_row": header_row,
        "fields": [
            {
                "name": field.name,
                "header": field.header,
                "type": field.field_type,
                "required": field.required,
            }
            for field in fields
        ],
        "key_fields": key_fields,
        "period_field": period_field,
        "ignored_columns": list(ignored_columns),
        "timezone": timezone,
    }
    canonical_json = json.dumps(
        canonical_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    mapping_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return ReportMapping(
        mapping_id=mapping_id,
        mapping_version=mapping_version,
        report_type=report_type,
        sheet=sheet,
        header_row=header_row,
        fields=tuple(fields),
        key_fields=tuple(key_fields),
        period_field=period_field,
        ignored_columns=ignored_columns,
        timezone=timezone,
        canonical_json=canonical_json,
        mapping_hash=mapping_hash,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _excel_datetime(value: int | float, *, date_1904: bool) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be an Excel date serial or ISO text")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("must be a finite non-negative Excel date serial")
    if not date_1904 and number == 60:
        raise ValueError("Excel's fictional 1900-02-29 date is not supported")
    if date_1904:
        base = datetime(1904, 1, 1)
    else:
        base = datetime(1899, 12, 31) if number < 60 else datetime(1899, 12, 30)
    return base + timedelta(days=number)


def _normalize_value(
    value: Any,
    field: FieldMapping,
    *,
    date_1904: bool,
    timezone: str | None,
) -> Any:
    if _is_empty(value):
        if field.required:
            raise ValueError("is required")
        return None
    if field.field_type == "text":
        if not isinstance(value, str):
            raise ValueError("must be stored as text in Excel")
        cleaned = " ".join(value.split())
        if not cleaned and field.required:
            raise ValueError("is required")
        return cleaned or None
    if field.field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("must be finite")
        return int(number) if number.is_integer() else number
    if field.field_type == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be an integer")
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError("must be an integer")
        return int(number)
    if field.field_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError("must be a native Excel boolean")
        return value
    if field.field_type == "date":
        if isinstance(value, str):
            try:
                parsed_date = date.fromisoformat(value.strip())
            except ValueError as exc:
                raise ValueError("must be ISO date YYYY-MM-DD or an Excel date") from exc
        else:
            parsed = _excel_datetime(value, date_1904=date_1904)
            if parsed.time() != datetime_time.min:
                raise ValueError("date field contains a time fraction")
            parsed_date = parsed.date()
        return parsed_date.isoformat()
    if field.field_type == "datetime":
        if isinstance(value, str):
            try:
                parsed_datetime = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("must be ISO datetime or an Excel datetime") from exc
        else:
            parsed_datetime = _excel_datetime(value, date_1904=date_1904)
        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(tzinfo=ZoneInfo(str(timezone)))
        return parsed_datetime.astimezone(UTC).isoformat().replace("+00:00", "Z")
    raise AssertionError(f"unsupported field type {field.field_type}")


def _header_text(cell: XlsxCell) -> str | None:
    if _is_empty(cell.value):
        return None
    if not isinstance(cell.value, str):
        raise ValueError("header cells must contain text")
    return " ".join(cell.value.split())


def _active_import(
    conn: sqlite3.Connection | None,
    report_type: str,
    report_period: str,
) -> dict[str, Any] | None:
    if conn is None or not _report_tables_exist(conn):
        return None
    row = conn.execute(
        "SELECT id, revision, content_sha256, mapping_id, mapping_version "
        "FROM monthly_report_imports "
        "WHERE report_type=? AND report_period=? AND status='active'",
        (report_type, report_period),
    ).fetchone()
    if row is None:
        return None
    return {
        "import_id": row[0],
        "revision": row[1],
        "content_sha256": row[2],
        "mapping_id": row[3],
        "mapping_version": row[4],
    }


def _report_tables_exist(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monthly_report_imports'"
    ).fetchone()
    return row is not None


def preview_report(
    conn: sqlite3.Connection | None,
    xlsx_path: Path | str,
    mapping_path: Path | str,
    report_period: str,
) -> ReportPreview:
    """Parse and validate one workbook without writing files or database rows."""
    if PERIOD_RE.fullmatch(report_period) is None:
        raise ValueError("report_period must be YYYY-MM")
    source = Path(xlsx_path)
    if source.is_symlink():
        raise ValueError("refusing to ingest a symlinked workbook")
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.casefold() != ".xlsx":
        raise ValueError("only .xlsx workbooks are supported")
    mapping = load_mapping(mapping_path)
    initial_size = source.stat().st_size
    if initial_size > MAX_FILE_BYTES:
        raise ValueError("workbook exceeds the 25 MiB input limit")
    content_sha256 = _file_sha256(source)
    sheet = read_sheet(source, mapping.sheet)
    if source.stat().st_size != initial_size or _file_sha256(source) != content_sha256:
        raise ValueError("workbook changed while it was being previewed; preview it again")
    issues: list[ValidationIssue] = []
    for row_number, cells in sorted(sheet.rows.items()):
        for cell in cells.values():
            if cell.has_formula:
                issues.append(
                    ValidationIssue(
                        "formula_not_allowed",
                        "formulas are not supported; paste a static value",
                        row_number,
                        cell.coordinate,
                    )
                )

    header_cells = sheet.rows.get(mapping.header_row, {})
    header_by_column: dict[int, str] = {}
    seen_headers: set[str] = set()
    for column, cell in sorted(header_cells.items()):
        if cell.has_formula:
            continue
        try:
            header = _header_text(cell)
        except ValueError as exc:
            issues.append(
                ValidationIssue(
                    "invalid_header",
                    str(exc),
                    mapping.header_row,
                    cell.coordinate,
                )
            )
            continue
        if header is None:
            continue
        if header in seen_headers:
            issues.append(
                ValidationIssue(
                    "duplicate_header",
                    f"header {header!r} appears more than once",
                    mapping.header_row,
                    cell.coordinate,
                )
            )
            continue
        seen_headers.add(header)
        header_by_column[column] = header

    expected_headers = {field.header for field in mapping.fields}
    allowed_headers = expected_headers | set(mapping.ignored_columns)
    for missing in sorted(expected_headers - seen_headers):
        issues.append(
            ValidationIssue("missing_header", f"required mapped header {missing!r} is missing")
        )
    for unexpected in sorted(seen_headers - allowed_headers):
        issues.append(
            ValidationIssue(
                "unexpected_header",
                f"undeclared column {unexpected!r}; map it or list it in ignored_columns",
            )
        )
    unlabeled_columns: set[int] = set()
    for row_number, cells in sorted(sheet.rows.items()):
        if row_number <= mapping.header_row:
            continue
        for column, cell in sorted(cells.items()):
            if (
                column not in header_by_column
                and column not in unlabeled_columns
                and (cell.has_formula or not _is_empty(cell.value))
            ):
                unlabeled_columns.add(column)
                issues.append(
                    ValidationIssue(
                        "unlabeled_column",
                        "data appears below a blank or invalid header; declare the column",
                        row_number,
                        cell.coordinate,
                    )
                )

    column_for_header = {header: column for column, header in header_by_column.items()}
    rows: list[ParsedReportRow] = []
    seen_row_keys: dict[str, int] = {}
    if not any(
        issue.code in {"missing_header", "duplicate_header", "invalid_header"} for issue in issues
    ):
        max_row = max(sheet.rows, default=mapping.header_row)
        for row_number in range(mapping.header_row + 1, max_row + 1):
            cells = sheet.rows.get(row_number, {})
            if not any(not _is_empty(cell.value) for cell in cells.values()):
                continue
            if any(cell.has_formula for cell in cells.values()):
                continue
            normalized: dict[str, Any] = {}
            row_issue_count = len(issues)
            for field in mapping.fields:
                column = column_for_header[field.header]
                data_cell = cells.get(column)
                value = data_cell.value if data_cell is not None else None
                try:
                    normalized[field.name] = _normalize_value(
                        value,
                        field,
                        date_1904=sheet.date_1904,
                        timezone=mapping.timezone,
                    )
                except ValueError as exc:
                    issues.append(
                        ValidationIssue(
                            "invalid_value",
                            f"{field.header!r} {exc}",
                            row_number,
                            data_cell.coordinate if data_cell is not None else field.header,
                        )
                    )
            if len(issues) != row_issue_count:
                continue
            if mapping.period_field is not None:
                actual_period = str(normalized[mapping.period_field])[:7]
                if actual_period != report_period:
                    issues.append(
                        ValidationIssue(
                            "period_mismatch",
                            f"row belongs to {actual_period}, expected {report_period}",
                            row_number,
                            field_by_name_header(mapping, mapping.period_field),
                        )
                    )
                    continue
            key_payload = {key: normalized[key] for key in mapping.key_fields}
            row_key = hashlib.sha256(
                json.dumps(
                    key_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ).encode("utf-8")
            ).hexdigest()
            if row_key in seen_row_keys:
                issues.append(
                    ValidationIssue(
                        "duplicate_row_key",
                        f"row key duplicates source row {seen_row_keys[row_key]}",
                        row_number,
                    )
                )
                continue
            seen_row_keys[row_key] = row_number
            data_json = json.dumps(
                normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            rows.append(
                ParsedReportRow(
                    source_row_number=row_number,
                    row_key=row_key,
                    row_hash=hashlib.sha256(data_json.encode("utf-8")).hexdigest(),
                    data=normalized,
                )
            )
    if not rows and not issues:
        issues.append(ValidationIssue("no_rows", "workbook has no non-empty data rows"))

    rows_digest = hashlib.sha256(
        "\n".join(f"{row.source_row_number}:{row.row_key}:{row.row_hash}" for row in rows).encode(
            "ascii"
        )
    ).hexdigest()
    duplicate = None
    if conn is not None and _report_tables_exist(conn):
        mapping_conflict = conn.execute(
            "SELECT id FROM monthly_report_imports "
            "WHERE mapping_id=? AND mapping_version=? AND mapping_hash<>? LIMIT 1",
            (mapping.mapping_id, mapping.mapping_version, mapping.mapping_hash),
        ).fetchone()
        if mapping_conflict is not None:
            issues.append(
                ValidationIssue(
                    "mapping_version_conflict",
                    "mapping content changed without incrementing mapping_version; "
                    f"existing import is {mapping_conflict[0]}",
                )
            )
        duplicate = conn.execute(
            "SELECT id FROM monthly_report_imports "
            "WHERE report_type=? AND report_period=? AND content_sha256=? AND mapping_hash=?",
            (
                mapping.report_type,
                report_period,
                content_sha256,
                mapping.mapping_hash,
            ),
        ).fetchone()
    return ReportPreview(
        source_path=source,
        source_filename=source.name,
        content_sha256=content_sha256,
        size_bytes=initial_size,
        report_period=report_period,
        mapping=mapping,
        headers=tuple(header_by_column[column] for column in sorted(header_by_column)),
        rows=tuple(rows),
        rows_digest=rows_digest,
        issues=tuple(issues),
        duplicate_import_id=duplicate[0] if duplicate else None,
        active_import=_active_import(conn, mapping.report_type, report_period),
    )


def field_by_name_header(mapping: ReportMapping, field_name: str) -> str:
    return next(field.header for field in mapping.fields if field.name == field_name)


def _copy_private(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite stored report {destination}")
    secure_directory(destination.parent.parent)
    secure_directory(destination.parent)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temp_path = Path(temp_name)
    try:
        with source.open("rb") as input_handle, os.fdopen(fd, "wb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        secure_file(temp_path)
        if temp_path.stat().st_size != expected_size or _file_sha256(temp_path) != expected_sha256:
            raise ValueError("workbook changed while it was being copied; preview it again")
        os.replace(temp_path, destination)
        secure_file(destination)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def apply_report(
    conn: sqlite3.Connection,
    xlsx_path: Path | str,
    mapping_path: Path | str,
    report_period: str,
    *,
    expected_sha256: str,
    store_dir: Path | str,
    supersedes_import_id: str | None = None,
) -> dict[str, Any]:
    """Hash-confirm and atomically apply a validated report or explicit correction."""
    preview = preview_report(conn, xlsx_path, mapping_path, report_period)
    if expected_sha256 != preview.content_sha256:
        raise ValueError(
            "workbook SHA-256 does not match the preview; preview the current file again"
        )
    if not preview.valid:
        raise ValueError(
            "workbook validation failed: "
            + "; ".join(issue.message for issue in preview.issues[:10])
        )
    if preview.duplicate_import_id is not None:
        row = conn.execute(
            "SELECT revision, status FROM monthly_report_imports WHERE id=?",
            (preview.duplicate_import_id,),
        ).fetchone()
        return {
            "status": "duplicate",
            "import_id": preview.duplicate_import_id,
            "revision": row[0],
            "import_status": row[1],
            "content_sha256": preview.content_sha256,
            "rows": len(preview.rows),
            "database_changed": False,
            "file_copied": False,
        }

    active = preview.active_import
    if active is None and supersedes_import_id is not None:
        raise ValueError("no active report exists to supersede for this report type/period")
    if active is not None and supersedes_import_id != active["import_id"]:
        raise ValueError(
            "a different report is already active for this type/period; "
            f"pass --supersedes {active['import_id']} to apply an explicit correction"
        )

    revision = int(active["revision"]) + 1 if active is not None else 1
    import_id = str(uuid.uuid4())
    store_root = secure_directory(store_dir)
    relative_path = (
        Path(preview.mapping.report_type)
        / preview.report_period
        / f"revision-{revision}-{preview.content_sha256[:16]}.xlsx"
    )
    destination = store_root / relative_path
    _copy_private(
        preview.source_path,
        destination,
        expected_sha256=preview.content_sha256,
        expected_size=preview.size_bytes,
    )
    imported_at = int(time.time() * 1000)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if active is not None:
            updated = conn.execute(
                "UPDATE monthly_report_imports SET status='superseded' "
                "WHERE id=? AND status='active'",
                (active["import_id"],),
            )
            if updated.rowcount != 1:
                raise RuntimeError("active report revision changed during apply")
        conn.execute(
            "INSERT INTO monthly_report_imports "
            "(id, report_type, report_period, revision, status, source_filename, "
            "stored_relpath, content_sha256, size_bytes, mapping_id, mapping_version, "
            "mapping_hash, mapping_json, sheet_name, row_count, rows_digest, "
            "parser_version, supersedes_import_id, imported_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                import_id,
                preview.mapping.report_type,
                preview.report_period,
                revision,
                preview.source_filename,
                relative_path.as_posix(),
                preview.content_sha256,
                preview.size_bytes,
                preview.mapping.mapping_id,
                preview.mapping.mapping_version,
                preview.mapping.mapping_hash,
                preview.mapping.canonical_json,
                preview.mapping.sheet,
                len(preview.rows),
                preview.rows_digest,
                PARSER_VERSION,
                active["import_id"] if active is not None else None,
                imported_at,
            ),
        )
        conn.executemany(
            "INSERT INTO monthly_report_rows "
            "(import_id, source_row_number, row_key, row_hash, data_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    import_id,
                    row.source_row_number,
                    row.row_key,
                    row.row_hash,
                    json.dumps(
                        row.data,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
                for row in preview.rows
            ],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        destination.unlink(missing_ok=True)
        raise
    return {
        "status": "applied",
        "import_id": import_id,
        "revision": revision,
        "report_type": preview.mapping.report_type,
        "report_period": preview.report_period,
        "content_sha256": preview.content_sha256,
        "mapping_hash": preview.mapping.mapping_hash,
        "rows": len(preview.rows),
        "supersedes_import_id": active["import_id"] if active is not None else None,
        "stored_relpath": relative_path.as_posix(),
        "database_changed": True,
        "file_copied": True,
    }


def list_imports(
    conn: sqlite3.Connection,
    *,
    report_type: str | None = None,
    report_period: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    where = ["1=1"]
    params: list[Any] = []
    if report_type is not None:
        where.append("report_type=?")
        params.append(report_type)
    if report_period is not None:
        if PERIOD_RE.fullmatch(report_period) is None:
            raise ValueError("report_period must be YYYY-MM")
        where.append("report_period=?")
        params.append(report_period)
    params.append(limit)
    rows = conn.execute(
        "SELECT id, report_type, report_period, revision, status, source_filename, "
        "content_sha256, mapping_id, mapping_version, row_count, supersedes_import_id, "
        "parser_version, imported_at FROM monthly_report_imports WHERE "
        + " AND ".join(where)
        + " ORDER BY report_type, report_period DESC, revision DESC LIMIT ?",
        params,
    ).fetchall()
    keys = (
        "import_id",
        "report_type",
        "report_period",
        "revision",
        "status",
        "source_filename",
        "content_sha256",
        "mapping_id",
        "mapping_version",
        "row_count",
        "supersedes_import_id",
        "parser_version",
        "imported_at",
    )
    return [dict(zip(keys, row)) for row in rows]


def audit_import(
    conn: sqlite3.Connection,
    import_id: str,
    *,
    store_dir: Path | str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT report_type, report_period, revision, status, source_filename, "
        "stored_relpath, content_sha256, size_bytes, mapping_id, mapping_version, "
        "mapping_hash, mapping_json, sheet_name, row_count, rows_digest, "
        "parser_version, supersedes_import_id, imported_at "
        "FROM monthly_report_imports WHERE id=?",
        (import_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"monthly report import {import_id!r} was not found")
    keys = (
        "report_type",
        "report_period",
        "revision",
        "status",
        "source_filename",
        "stored_relpath",
        "content_sha256",
        "size_bytes",
        "mapping_id",
        "mapping_version",
        "mapping_hash",
        "mapping_json",
        "sheet_name",
        "row_count",
        "rows_digest",
        "parser_version",
        "supersedes_import_id",
        "imported_at",
    )
    metadata = dict(zip(keys, row))
    store_root = Path(store_dir).resolve()
    stored_file = (store_root / metadata["stored_relpath"]).resolve()
    try:
        stored_file.relative_to(store_root)
    except ValueError as exc:
        raise ValueError("stored report path escapes the configured private store") from exc

    stored_exists = stored_file.is_file() and not stored_file.is_symlink()
    file_size_matches = stored_exists and stored_file.stat().st_size == metadata["size_bytes"]
    file_hash_matches = stored_exists and _file_sha256(stored_file) == metadata["content_sha256"]
    mapping_hash_matches = (
        hashlib.sha256(metadata["mapping_json"].encode("utf-8")).hexdigest()
        == metadata["mapping_hash"]
    )
    stored_rows = conn.execute(
        "SELECT source_row_number, row_key, row_hash, data_json "
        "FROM monthly_report_rows WHERE import_id=? ORDER BY source_row_number",
        (import_id,),
    ).fetchall()
    calculated_rows_digest = hashlib.sha256(
        "\n".join(f"{item[0]}:{item[1]}:{item[2]}" for item in stored_rows).encode("ascii")
    ).hexdigest()
    row_count_matches = len(stored_rows) == metadata["row_count"]
    rows_digest_matches = calculated_rows_digest == metadata["rows_digest"]
    row_hashes_match = all(
        hashlib.sha256(item[3].encode("utf-8")).hexdigest() == item[2] for item in stored_rows
    )
    row_keys_match = False
    try:
        mapping_payload = json.loads(metadata["mapping_json"])
        key_fields = mapping_payload["key_fields"]
        row_keys_match = all(
            hashlib.sha256(
                json.dumps(
                    {key: json.loads(item[3])[key] for key in key_fields},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            == item[1]
            for item in stored_rows
        )
    except (KeyError, TypeError, json.JSONDecodeError):
        row_keys_match = False
    successor = conn.execute(
        "SELECT id FROM monthly_report_imports WHERE supersedes_import_id=?",
        (import_id,),
    ).fetchone()
    checks = {
        "stored_file_exists": stored_exists,
        "file_size_matches": file_size_matches,
        "file_hash_matches": file_hash_matches,
        "mapping_hash_matches": mapping_hash_matches,
        "parser_version_supported": metadata["parser_version"] == PARSER_VERSION,
        "row_count_matches": row_count_matches,
        "rows_digest_matches": rows_digest_matches,
        "row_hashes_match": row_hashes_match,
        "row_keys_match": row_keys_match,
    }
    return {
        "import_id": import_id,
        "ok": all(checks.values()),
        "checks": checks,
        "metadata": {key: value for key, value in metadata.items() if key != "mapping_json"},
        "lineage": {
            "supersedes_import_id": metadata["supersedes_import_id"],
            "superseded_by_import_id": successor[0] if successor else None,
        },
    }
