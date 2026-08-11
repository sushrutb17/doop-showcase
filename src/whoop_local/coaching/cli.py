"""Evidence-backed proposal workflow; acceptance is a separate explicit action."""

from __future__ import annotations

import argparse
import json

from whoop_local.coaching import service
from whoop_local.config import DEFAULT_DB_PATH
from whoop_local.db.schema import init_db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    commands = parser.add_subparsers(dest="command", required=True)
    propose = commands.add_parser(
        "propose-weight", help="Create a conservative pending progression proposal"
    )
    propose.add_argument("workout_name")
    propose.add_argument("exercise_name")
    for name, help_text in (
        ("show", "Review a proposal"),
        ("accept", "Explicitly accept and create a new plan version"),
        ("reject", "Reject a pending proposal"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("proposal_id")
    args = parser.parse_args()
    conn = init_db(args.db)
    try:
        if args.command == "propose-weight":
            result = service.propose_weight_progression(conn, args.workout_name, args.exercise_name)
        elif args.command == "show":
            result = service.get_proposal(conn, args.proposal_id)
        elif args.command == "accept":
            result = service.accept_proposal(conn, args.proposal_id)
        else:
            result = service.reject_proposal(conn, args.proposal_id)
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
