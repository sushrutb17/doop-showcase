"""Local command-line manual logging with an explicit confirm step.

Usage:
  python -m whoop_local.manual_logging.cli prepare --json-file workout.json
  python -m whoop_local.manual_logging.cli show DRAFT_ID
  python -m whoop_local.manual_logging.cli confirm DRAFT_ID
  python -m whoop_local.manual_logging.cli cancel DRAFT_ID
"""

from __future__ import annotations

import argparse
import json

from whoop_local.config import DEFAULT_DB_PATH
from whoop_local.db.schema import init_db
from whoop_local.manual_logging import service


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser(
        "prepare", help="Validate and save a pending draft; does not log a workout"
    )
    prepare.add_argument("--json-file", required=True)
    show = subparsers.add_parser("show", help="Inspect a draft before confirmation")
    show.add_argument("draft_id")
    confirm = subparsers.add_parser(
        "confirm", help="Explicitly write a pending draft to the logbook"
    )
    confirm.add_argument("draft_id")
    cancel = subparsers.add_parser("cancel", help="Cancel a pending draft")
    cancel.add_argument("draft_id")
    args = parser.parse_args()

    conn = init_db(args.db)
    try:
        if args.command == "prepare":
            with open(args.json_file, encoding="utf-8") as handle:
                result = service.prepare_draft(conn, json.load(handle))
        elif args.command == "show":
            result = service.get_draft(conn, args.draft_id)
        elif args.command == "confirm":
            result = service.confirm_draft(conn, args.draft_id)
        else:
            result = service.cancel_draft(conn, args.draft_id)
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
