"""Preview, apply, list, and audit private monthly Excel report imports."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from whoop_local.config import DEFAULT_DB_PATH, REPO_ROOT
from whoop_local.db.schema import init_db
from whoop_local.report_ingestion.service import (
    apply_report,
    audit_import,
    list_imports,
    preview_report,
)

DEFAULT_REPORT_STORE = REPO_ROOT / "data" / "monthly_reports"


def _print_json(payload: Any, *, output: Any | None = None) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True), file=output or sys.stdout)


def _read_only_connection(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--store-dir", type=Path, default=DEFAULT_REPORT_STORE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser("preview", help="validate without writing")
    preview.add_argument("--xlsx", type=Path, required=True)
    preview.add_argument("--mapping", type=Path, required=True)
    preview.add_argument("--period", required=True, metavar="YYYY-MM")
    preview.add_argument(
        "--show-rows",
        action="store_true",
        help="include normalized private row values in terminal output",
    )

    apply = subparsers.add_parser("apply", help="apply hash-confirmed valid content")
    apply.add_argument("--xlsx", type=Path, required=True)
    apply.add_argument("--mapping", type=Path, required=True)
    apply.add_argument("--period", required=True, metavar="YYYY-MM")
    apply.add_argument("--expected-sha256", required=True)
    apply.add_argument(
        "--supersedes",
        help="active import ID to replace when applying a correction",
    )

    list_parser = subparsers.add_parser("list", help="list immutable import history")
    list_parser.add_argument("--report-type")
    list_parser.add_argument("--period", metavar="YYYY-MM")
    list_parser.add_argument("--limit", type=int, default=50)

    audit = subparsers.add_parser("audit", help="reconcile stored evidence and metadata")
    audit.add_argument("import_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    output: Any
    try:
        if args.command == "preview":
            conn = _read_only_connection(args.db)
            preview = preview_report(conn, args.xlsx, args.mapping, args.period)
            _print_json(preview.to_dict(include_rows=args.show_rows))
            return 0 if preview.valid else 2

        if args.command == "apply":
            conn = _read_only_connection(args.db)
            preview = preview_report(conn, args.xlsx, args.mapping, args.period)
            if conn is not None:
                conn.close()
            conn = None
            if args.expected_sha256 != preview.content_sha256:
                raise ValueError(
                    "workbook SHA-256 does not match the preview; preview the current file again"
                )
            if not preview.valid:
                raise ValueError(
                    "workbook validation failed: "
                    + "; ".join(issue.message for issue in preview.issues[:10])
                )
            conn = init_db(args.db)
            output = apply_report(
                conn,
                args.xlsx,
                args.mapping,
                args.period,
                expected_sha256=args.expected_sha256,
                store_dir=args.store_dir,
                supersedes_import_id=args.supersedes,
            )
        elif args.command == "list":
            conn = _read_only_connection(args.db)
            if conn is None:
                raise ValueError(f"database does not exist: {args.db}")
            output = {
                "imports": list_imports(
                    conn,
                    report_type=args.report_type,
                    report_period=args.period,
                    limit=args.limit,
                )
            }
        else:
            conn = _read_only_connection(args.db)
            if conn is None:
                raise ValueError(f"database does not exist: {args.db}")
            output = audit_import(conn, args.import_id, store_dir=args.store_dir)
        _print_json(output)
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        _print_json(
            {"status": "error", "error": str(exc), "command": args.command},
            output=sys.stderr,
        )
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
