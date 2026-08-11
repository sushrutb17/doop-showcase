#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=${DOOP_REPO_ROOT:-$(CDPATH= cd -- "$script_dir/../.." && pwd)}
openclaw=${OPENCLAW_BIN:-openclaw}
python=${DOOP_PYTHON:-$repo_root/.venv/bin/python}
profile=${DOOP_OPENCLAW_PROFILE:?set DOOP_OPENCLAW_PROFILE to an isolated profile}
db=${DOOP_DB_PATH:?set DOOP_DB_PATH to an isolated acceptance database}
export_root=${DOOP_ACCEPTANCE_EXPORT_ROOT:-$repo_root/tmp}
week_start=${1:?usage: run-weekly-acceptance.sh YYYY-MM-DD}
session_suffix=$(date +%s)
session_key="agent:doop:acceptance-weekly-$session_suffix"
export_name="acceptance-weekly-$session_suffix"

prompt="Review my training week beginning $week_start. Call get_telegram_weekly_review exactly once. Return its text field verbatim as the visible final answer. Never call another tool, add labels, return NO_REPLY, or turn a missing log into proof I missed training."

test -f "$db" || { echo "isolated acceptance database does not exist" >&2; exit 2; }
"$openclaw" --profile "$profile" config get mcp.servers.doop.args --json |
    "$python" -c 'import json, pathlib, sys
args = json.load(sys.stdin)
expected = pathlib.Path(sys.argv[1]).expanduser().resolve()
matches = any(
    arg == "--db" and index + 1 < len(args)
    and pathlib.Path(args[index + 1]).expanduser().resolve() == expected
    for index, arg in enumerate(args)
)
if not matches:
    raise SystemExit("isolated OpenClaw profile must bind Doop MCP to DOOP_DB_PATH via --db")' "$db"

"$openclaw" --profile "$profile" agent --agent doop --session-key "$session_key" \
    --message "$prompt" --thinking off --timeout 300 --json >/dev/null
"$openclaw" --profile "$profile" sessions export-trajectory --agent doop --session-key "$session_key" \
    --workspace "$export_root" --output "$export_name" --json >/dev/null
"$python" -m whoop_local.integrations.openclaw_acceptance \
    --scenario weekly \
    --trajectory "$export_root/.openclaw/trajectory-exports/$export_name" \
    --db "$db" --week-start "$week_start"
