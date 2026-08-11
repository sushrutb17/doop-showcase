#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=${DOOP_REPO_ROOT:-$(CDPATH= cd -- "$script_dir/../.." && pwd)}
openclaw=${OPENCLAW_BIN:-openclaw}
python=${DOOP_PYTHON:-$repo_root/.venv/bin/python}
profile=${DOOP_OPENCLAW_PROFILE:?set DOOP_OPENCLAW_PROFILE to an isolated profile}
export_root=${DOOP_ACCEPTANCE_EXPORT_ROOT:-$repo_root/tmp}
: "${DOOP_ALLOW_CONFIG_MUTATION:?set DOOP_ALLOW_CONFIG_MUTATION=1 for this restoring fault-injection drill}"
[ "$DOOP_ALLOW_CONFIG_MUTATION" = 1 ] || { echo "DOOP_ALLOW_CONFIG_MUTATION must equal 1" >&2; exit 2; }

session_suffix=$(date +%s)
session_key="agent:doop:acceptance-offline-$session_suffix"
export_name="acceptance-offline-$session_suffix"
original_enabled=$("$openclaw" --profile "$profile" config get mcp.servers.doop.enabled --json |
    jq -er 'if type == "boolean" then tostring else empty end')

restore_mcp() {
    set +e
    "$openclaw" --profile "$profile" config set mcp.servers.doop.enabled "$original_enabled" >/dev/null
    "$openclaw" --profile "$profile" gateway restart >/dev/null
}
trap restore_mcp EXIT HUP INT TERM

"$openclaw" --profile "$profile" config set mcp.servers.doop.enabled false >/dev/null
"$openclaw" --profile "$profile" gateway restart >/dev/null
"$openclaw" --profile "$profile" agent --agent doop --session-key "$session_key" \
    --message "What should I do for training today, and is my recovery good enough?" \
    --thinking off --timeout 300 --json >/dev/null
"$openclaw" --profile "$profile" sessions export-trajectory --agent doop --session-key "$session_key" \
    --workspace "$export_root" --output "$export_name" --json >/dev/null
"$python" -m whoop_local.integrations.openclaw_acceptance \
    --scenario offline \
    --trajectory "$export_root/.openclaw/trajectory-exports/$export_name"
