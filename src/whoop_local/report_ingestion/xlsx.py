"""Bounded dependency-minimal reader for the tabular subset of `.xlsx` files."""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_ZIP_ENTRIES = 200
MAX_ENTRY_BYTES = 50 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ROWS = 20_000
MAX_COLUMNS = 256

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CELL_REFERENCE_RE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")


class XlsxFormatError(ValueError):
    """The workbook is malformed, unsafe, or outside the supported tabular subset."""


@dataclass(frozen=True)
class XlsxCell:
    coordinate: str
    value: Any
    has_formula: bool


@dataclass(frozen=True)
class XlsxSheet:
    name: str
    rows: dict[int, dict[int, XlsxCell]]
    date_1904: bool


def column_number(reference: str) -> tuple[int, int]:
    match = CELL_REFERENCE_RE.fullmatch(reference)
    if match is None:
        raise XlsxFormatError(f"invalid cell reference {reference!r}")
    letters, row_text = match.groups()
    column = 0
    for char in letters:
        column = column * 26 + ord(char) - ord("A") + 1
    return column, int(row_text)


def _safe_xml_root(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    try:
        payload = archive.read(name)
    except KeyError as exc:
        raise XlsxFormatError(f"workbook is missing required part {name!r}") from exc
    upper = payload[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise XlsxFormatError(f"XML entities are not supported in {name!r}")
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise XlsxFormatError(f"malformed XML in {name!r}: {exc}") from exc


def _validate_archive(archive: zipfile.ZipFile) -> None:
    entries = archive.infolist()
    if len(entries) > MAX_ZIP_ENTRIES:
        raise XlsxFormatError(f"workbook has too many ZIP entries ({len(entries)})")
    expanded = 0
    seen_names: set[str] = set()
    for entry in entries:
        name = entry.filename.replace("\\", "/")
        normalized = posixpath.normpath(name)
        if normalized in seen_names:
            raise XlsxFormatError(f"workbook repeats ZIP part {normalized!r}")
        seen_names.add(normalized)
        if name.startswith("/") or normalized == ".." or normalized.startswith("../"):
            raise XlsxFormatError(f"unsafe ZIP path {name!r}")
        if entry.flag_bits & 0x1:
            raise XlsxFormatError("encrypted XLSX parts are not supported")
        if entry.file_size > MAX_ENTRY_BYTES:
            raise XlsxFormatError(f"workbook part {name!r} is too large")
        expanded += entry.file_size
        lowered = name.casefold()
        if lowered.endswith("vbaproject.bin") or lowered.startswith("xl/externallinks/"):
            raise XlsxFormatError("macros and external workbook links are not supported")
    if expanded > MAX_EXPANDED_BYTES:
        raise XlsxFormatError("expanded workbook content exceeds the safety limit")


def _relationship_target(target: str) -> str:
    normalized = posixpath.normpath(posixpath.join("xl", target))
    if normalized == ".." or normalized.startswith("../") or not normalized.startswith("xl/"):
        raise XlsxFormatError(f"unsafe workbook relationship target {target!r}")
    return normalized


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _safe_xml_root(archive, "xl/sharedStrings.xml")
    strings: list[str] = []
    for item in root.findall(f"{{{MAIN_NS}}}si"):
        strings.append("".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t")))
    return strings


def _cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> Any:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(f"{{{MAIN_NS}}}is")
        if inline is None:
            return ""
        return "".join(node.text or "" for node in inline.iter(f"{{{MAIN_NS}}}t"))

    value_node = cell.find(f"{{{MAIN_NS}}}v")
    if value_node is None or value_node.text is None:
        return None
    raw = value_node.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError) as exc:
            raise XlsxFormatError(f"invalid shared-string index {raw!r}") from exc
    if cell_type in {"str", "d"}:
        return raw
    if cell_type == "b":
        if raw not in {"0", "1"}:
            raise XlsxFormatError(f"invalid boolean cell value {raw!r}")
        return raw == "1"
    if cell_type == "e":
        raise XlsxFormatError(f"workbook contains an Excel error value {raw!r}")
    try:
        number = float(raw)
    except ValueError as exc:
        raise XlsxFormatError(f"invalid numeric cell value {raw!r}") from exc
    return int(number) if number.is_integer() else number


def read_sheet(path: Path | str, sheet_name: str) -> XlsxSheet:
    """Read one exact worksheet while enforcing file, ZIP, XML, and size bounds."""
    source = Path(path)
    if source.suffix.casefold() != ".xlsx":
        raise XlsxFormatError("only .xlsx workbooks are supported")
    if source.is_symlink():
        raise XlsxFormatError("refusing to ingest a symlinked workbook")
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.stat().st_size > MAX_FILE_BYTES:
        raise XlsxFormatError("workbook exceeds the 25 MiB input limit")

    try:
        archive_context = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise XlsxFormatError("file is not a valid XLSX ZIP package") from exc

    with archive_context as archive:
        _validate_archive(archive)
        workbook = _safe_xml_root(archive, "xl/workbook.xml")
        relationships = _safe_xml_root(archive, "xl/_rels/workbook.xml.rels")
        relationship_targets = {
            rel.get("Id"): _relationship_target(rel.get("Target", ""))
            for rel in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
            if rel.get("Id")
        }
        workbook_properties = workbook.find(f"{{{MAIN_NS}}}workbookPr")
        date_1904 = bool(
            workbook_properties is not None
            and workbook_properties.get("date1904", "").casefold() in {"1", "true"}
        )

        selected = None
        for sheet in workbook.findall(f".//{{{MAIN_NS}}}sheet"):
            if sheet.get("name") == sheet_name:
                selected = sheet
                break
        if selected is None:
            available = [sheet.get("name") for sheet in workbook.findall(f".//{{{MAIN_NS}}}sheet")]
            raise XlsxFormatError(
                f"worksheet {sheet_name!r} was not found; available sheets: {available}"
            )
        if selected.get("state", "visible") != "visible":
            raise XlsxFormatError(f"worksheet {sheet_name!r} is hidden")
        relationship_id = selected.get(f"{{{REL_NS}}}id")
        sheet_part = relationship_targets.get(relationship_id)
        if not sheet_part or not sheet_part.startswith("xl/worksheets/"):
            raise XlsxFormatError(f"worksheet {sheet_name!r} has an invalid relationship")

        shared_strings = _shared_strings(archive)
        for part in archive.namelist():
            normalized_part = part.replace("\\", "/")
            if (
                normalized_part.startswith("xl/worksheets/")
                and normalized_part.endswith(".xml")
                and normalized_part != sheet_part
            ):
                other_sheet = _safe_xml_root(archive, part)
                if other_sheet.find(f".//{{{MAIN_NS}}}f") is not None:
                    raise XlsxFormatError(
                        f"worksheet part {normalized_part!r} contains formulas; "
                        "paste static values before import"
                    )
        sheet_root = _safe_xml_root(archive, sheet_part)
        rows: dict[int, dict[int, XlsxCell]] = {}
        for cell in sheet_root.findall(f".//{{{MAIN_NS}}}c"):
            reference = cell.get("r")
            if not reference:
                raise XlsxFormatError("worksheet cell is missing its coordinate")
            column, row_number = column_number(reference)
            if row_number > MAX_ROWS or column > MAX_COLUMNS:
                raise XlsxFormatError(
                    f"cell {reference} exceeds the {MAX_ROWS}-row/{MAX_COLUMNS}-column limit"
                )
            row = rows.setdefault(row_number, {})
            if column in row:
                raise XlsxFormatError(f"worksheet repeats cell coordinate {reference!r}")
            row[column] = XlsxCell(
                coordinate=reference,
                value=_cell_value(cell, shared_strings),
                has_formula=cell.find(f"{{{MAIN_NS}}}f") is not None,
            )
        return XlsxSheet(name=sheet_name, rows=rows, date_1904=date_1904)
