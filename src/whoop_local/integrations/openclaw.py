"""OpenClaw integration constants and fail-closed configuration validation.

This module never reads or prints secret values.  It validates only the
security-relevant shape of an OpenClaw configuration and is intentionally
usable without importing OpenClaw itself.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DOOP_AGENT_ID = "doop"
DOOP_MCP_SERVER = "doop"
DOOP_SAFETY_MCP_SERVER = "doop_safety"
DOOP_SAFETY_PLUGIN = "doop-safety-gate"
DOOP_SAFETY_TOOL = "get_training_offline_message"
DOOP_SAFETY_TOOLS = (DOOP_SAFETY_TOOL, "get_training_safety_message")

# Minimum useful read surface for today, pre-workout, and weekly-review flows.
# Raw readings, prospective-library browsing, and every local write service are
# deliberately absent.
READ_TOOL_ALLOWLIST = (
    "get_telegram_training_guidance",
    "get_telegram_weekly_review",
)

FORBIDDEN_AGENT_TOOLS = (
    "exec",
    "process",
    "read",
    "write",
    "edit",
    "apply_patch",
    "browser",
    "web_search",
    "group:fs",
    "group:runtime",
)

PROJECTED_AGENT_TOOLS = tuple(f"{DOOP_MCP_SERVER}__{name}" for name in READ_TOOL_ALLOWLIST)
SAFETY_AGENT_TOOLS = tuple(f"{DOOP_SAFETY_MCP_SERVER}__{name}" for name in DOOP_SAFETY_TOOLS)
FINAL_AGENT_TOOL_ALLOWLIST = PROJECTED_AGENT_TOOLS + SAFETY_AGENT_TOOLS


def _agent(config: dict[str, Any], agent_id: str = DOOP_AGENT_ID) -> dict[str, Any] | None:
    for entry in config.get("agents", {}).get("list", []):
        if isinstance(entry, dict) and entry.get("id") == agent_id:
            return entry
    return None


def validate_config(config: dict[str, Any], *, require_channel_enabled: bool = True) -> list[str]:
    """Return safe, secret-free configuration errors for the Doop deployment.

    A pre-launch deployment may be deliberately disabled while retaining every
    other security invariant. Callers must opt into accepting that state.
    """
    errors: list[str] = []

    safety_plugin = config.get("plugins", {}).get("entries", {}).get(DOOP_SAFETY_PLUGIN)
    if not isinstance(safety_plugin, dict) or safety_plugin.get("enabled") is not True:
        errors.append("Doop pre-model safety plugin must be installed and enabled")
    elif safety_plugin.get("hooks", {}).get("allowConversationAccess") is not True:
        errors.append("Doop pre-model safety plugin must have explicit conversation-hook access")

    telegram = config.get("channels", {}).get("telegram", {})
    if require_channel_enabled and telegram.get("enabled") is not True:
        errors.append("Telegram channel is not enabled")
    if telegram.get("dmPolicy") != "allowlist":
        errors.append("Telegram dmPolicy must be 'allowlist'")
    allow_from = telegram.get("allowFrom")
    if not isinstance(allow_from, list) or not allow_from:
        errors.append("Telegram allowFrom must contain at least one numeric owner ID")
    elif any(
        not str(value).removeprefix("telegram:").removeprefix("tg:").isdigit()
        for value in allow_from
    ):
        errors.append("Telegram allowFrom may contain numeric owner IDs only")
    if telegram.get("groupPolicy", "disabled") != "disabled":
        errors.append("Telegram groupPolicy must be 'disabled' for the owner-only MVP")
    token_file = telegram.get("tokenFile")
    bot_token = telegram.get("botToken")
    has_secret_ref = isinstance(bot_token, dict) and {"source", "provider", "id"}.issubset(
        bot_token
    )
    if not has_secret_ref and not (isinstance(token_file, str) and Path(token_file).is_absolute()):
        errors.append("Telegram credential must use a structured SecretRef or absolute tokenFile")
    if telegram.get("configWrites") is not False:
        errors.append("Telegram configWrites must be false")
    if (
        not isinstance(telegram.get("dmHistoryLimit"), int)
        or not 0 <= telegram["dmHistoryLimit"] <= 20
    ):
        errors.append("Telegram dmHistoryLimit must be explicitly bounded to 0-20")
    if telegram.get("linkPreview") is not False:
        errors.append("Telegram linkPreview must be false")

    server = config.get("mcp", {}).get("servers", {}).get(DOOP_MCP_SERVER)
    if not isinstance(server, dict):
        errors.append("Doop MCP server is not registered")
    else:
        if server.get("enabled", True) is not True:
            errors.append("Doop MCP server is disabled")
        include = server.get("toolFilter", {}).get("include", [])
        if set(include) != set(READ_TOOL_ALLOWLIST):
            errors.append("Doop MCP include filter does not exactly match the approved read tools")
        if server.get("command") is None:
            errors.append("Doop MCP server must use a local stdio command")

    safety_server = config.get("mcp", {}).get("servers", {}).get(DOOP_SAFETY_MCP_SERVER)
    if not isinstance(safety_server, dict):
        errors.append("Doop safety MCP server is not registered")
    else:
        if safety_server.get("enabled", True) is not True:
            errors.append("Doop safety MCP server is disabled")
        safety_include = safety_server.get("toolFilter", {}).get("include", [])
        if set(safety_include) != set(DOOP_SAFETY_TOOLS):
            errors.append(
                "Doop safety MCP filter must expose exactly the approved safety relay tools"
            )
        if safety_server.get("command") is None:
            errors.append("Doop safety MCP server must use a local stdio command")

    agent = _agent(config)
    if agent is None:
        errors.append("Dedicated Doop agent is not configured")
    else:
        tools = agent.get("tools", {})
        denied = set(tools.get("deny", []))
        missing_denies = set(FORBIDDEN_AGENT_TOOLS) - denied
        if missing_denies:
            errors.append("Doop agent is missing required tool denials")
        # `allow` is the final explicit allowlist. The full base profile is safe
        # here because nothing outside this exact projected MCP list survives it.
        if tools.get("profile") != "full":
            errors.append("Doop agent tool profile must be full with an exact final allowlist")
        if set(tools.get("allow", [])) != set(FINAL_AGENT_TOOL_ALLOWLIST):
            errors.append(
                "Doop agent tool allowlist does not exactly match the approved Doop relay tools"
            )
        model = agent.get("model", {})
        primary = model.get("primary") if isinstance(model, dict) else model
        fallbacks = model.get("fallbacks", []) if isinstance(model, dict) else []
        if not isinstance(primary, str) or not primary.startswith(
            ("ollama/", "lmstudio/", "local/")
        ):
            errors.append("Doop agent primary model is not a recognized local provider")
        if not fallbacks or not str(fallbacks[0]).startswith("groq/"):
            errors.append("Doop agent first fallback must be a Groq model")

    bindings = config.get("bindings", [])
    has_binding = any(
        isinstance(binding, dict)
        and binding.get("agentId") == DOOP_AGENT_ID
        and binding.get("match", {}).get("channel") == "telegram"
        for binding in bindings
    )
    if not has_binding:
        errors.append("Telegram is not routed to the dedicated Doop agent")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Doop's OpenClaw deployment")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--allow-disabled",
        action="store_true",
        help="accept an explicitly disabled pre-launch Telegram channel",
    )
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    errors = validate_config(config, require_channel_enabled=not args.allow_disabled)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("OpenClaw Doop configuration passes the fail-closed checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
