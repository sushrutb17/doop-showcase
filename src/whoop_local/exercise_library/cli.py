"""Import, search, inspect, and explicitly review prospective exercises.

Importing metadata never creates canonical exercises. Review an entry first,
then use a separate approve-existing, approve-new, or reject command.
"""

from __future__ import annotations

import argparse
import json

from whoop_local.config import DEFAULT_DB_PATH
from whoop_local.db.schema import init_db
from whoop_local.exercise_library import service


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    commands = parser.add_subparsers(dest="command", required=True)
    import_cmd = commands.add_parser("import", help="Import non-media dataset metadata")
    import_cmd.add_argument("--json-file", required=True)
    search = commands.add_parser("search", help="Search prospective entries")
    search.add_argument("--query")
    search.add_argument("--equipment")
    search.add_argument("--target")
    search.add_argument("--body-part")
    search.add_argument(
        "--review-status", default="all", choices=("all", "unreviewed", "approved", "rejected")
    )
    search.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show", help="Show one entry and its review state")
    show.add_argument("external_id")
    existing = commands.add_parser(
        "approve-existing", help="Link to an exact existing canonical exercise"
    )
    existing.add_argument("external_id")
    existing.add_argument("--canonical-name", required=True)
    existing.add_argument("--notes")
    new = commands.add_parser(
        "approve-new", help="Explicitly create and link a new canonical exercise"
    )
    new.add_argument("external_id")
    new.add_argument("--canonical-name", required=True)
    new.add_argument("--notes")
    reject = commands.add_parser("reject", help="Reject an external candidate")
    reject.add_argument("external_id")
    reject.add_argument("--notes")
    args = parser.parse_args()

    conn = init_db(args.db)
    try:
        result: object
        if args.command == "import":
            result = service.import_dataset(conn, args.json_file)
        elif args.command == "search":
            result = service.search_entries(
                conn,
                query=args.query,
                equipment=args.equipment,
                target=args.target,
                body_part=args.body_part,
                review_status=args.review_status,
                limit=args.limit,
            )
        elif args.command == "show":
            result = service.get_entry(conn, args.external_id)
        elif args.command == "approve-existing":
            result = service.review_entry(
                conn,
                args.external_id,
                "approve_existing",
                canonical_name=args.canonical_name,
                notes=args.notes,
            )
        elif args.command == "approve-new":
            result = service.review_entry(
                conn,
                args.external_id,
                "approve_new",
                canonical_name=args.canonical_name,
                notes=args.notes,
            )
        else:
            result = service.review_entry(conn, args.external_id, "reject", notes=args.notes)
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
