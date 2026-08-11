from copy import deepcopy

from whoop_local.integrations.openclaw import (
    FINAL_AGENT_TOOL_ALLOWLIST,
    FORBIDDEN_AGENT_TOOLS,
    READ_TOOL_ALLOWLIST,
    validate_config,
)


def valid_config():
    return {
        "plugins": {
            "entries": {
                "doop-safety-gate": {
                    "enabled": True,
                    "hooks": {"allowConversationAccess": True},
                    "config": {},
                }
            }
        },
        "channels": {
            "telegram": {
                "enabled": True,
                "dmPolicy": "allowlist",
                "allowFrom": ["123456"],
                "groupPolicy": "disabled",
                "tokenFile": "/private/openclaw/telegram-token",
                "configWrites": False,
                "dmHistoryLimit": 12,
                "linkPreview": False,
            }
        },
        "mcp": {
            "servers": {
                "doop": {
                    "command": "/venv/bin/python",
                    "args": ["-m", "whoop_local.mcp_server.server"],
                    "toolFilter": {"include": list(READ_TOOL_ALLOWLIST)},
                },
                "doop_safety": {
                    "command": "/venv/bin/python",
                    "args": ["-m", "whoop_local.integrations.openclaw_safety_server"],
                    "toolFilter": {
                        "include": ["get_training_offline_message", "get_training_safety_message"]
                    },
                },
            }
        },
        "agents": {
            "list": [
                {
                    "id": "doop",
                    "model": {
                        "primary": "ollama/synthetic-tool-model:latest",
                        "fallbacks": ["groq/synthetic-fallback"],
                    },
                    "tools": {
                        "profile": "full",
                        "allow": list(FINAL_AGENT_TOOL_ALLOWLIST),
                        "deny": list(FORBIDDEN_AGENT_TOOLS),
                    },
                }
            ]
        },
        "bindings": [{"agentId": "doop", "match": {"channel": "telegram"}}],
    }


def test_valid_openclaw_config_passes():
    assert validate_config(valid_config()) == []


def test_disabled_channel_is_only_accepted_with_explicit_prelaunch_mode():
    config = valid_config()
    config["channels"]["telegram"]["enabled"] = False
    assert validate_config(config) == ["Telegram channel is not enabled"]
    assert validate_config(config, require_channel_enabled=False) == []


def test_open_telegram_and_raw_tool_exposure_fail_closed():
    config = deepcopy(valid_config())
    config["channels"]["telegram"]["dmPolicy"] = "open"
    config["channels"]["telegram"]["allowFrom"] = ["*"]
    config["mcp"]["servers"]["doop"]["toolFilter"]["include"].append("query_readings")
    errors = validate_config(config)
    assert any("dmPolicy" in error for error in errors)
    assert any("numeric owner IDs" in error for error in errors)
    assert any("approved read tools" in error for error in errors)


def test_telegram_secret_and_privacy_controls_fail_closed():
    config = deepcopy(valid_config())
    telegram = config["channels"]["telegram"]
    telegram.pop("tokenFile")
    telegram["configWrites"] = True
    telegram["dmHistoryLimit"] = 50
    telegram["linkPreview"] = True
    errors = validate_config(config)
    assert any("credential" in error for error in errors)
    assert any("configWrites" in error for error in errors)
    assert any("dmHistoryLimit" in error for error in errors)
    assert any("linkPreview" in error for error in errors)


def test_cloud_primary_and_missing_denials_fail_closed():
    config = deepcopy(valid_config())
    agent = config["agents"]["list"][0]
    agent["model"] = {"primary": "groq/model", "fallbacks": []}
    agent["tools"]["deny"] = []
    errors = validate_config(config)
    assert any("primary model" in error for error in errors)
    assert any("fallback" in error for error in errors)
    assert any("tool denials" in error for error in errors)


def test_missing_or_broad_safety_server_fails_closed():
    config = deepcopy(valid_config())
    del config["mcp"]["servers"]["doop_safety"]
    assert any("safety MCP server" in error for error in validate_config(config))
    config = valid_config()
    config["mcp"]["servers"]["doop_safety"]["toolFilter"]["include"].append("other")
    assert any("approved safety" in error for error in validate_config(config))


def test_missing_or_unprivileged_pre_model_safety_plugin_fails_closed():
    config = deepcopy(valid_config())
    del config["plugins"]["entries"]["doop-safety-gate"]
    assert any("pre-model safety plugin" in error for error in validate_config(config))

    config = valid_config()
    config["plugins"]["entries"]["doop-safety-gate"]["hooks"] = {}
    assert any("conversation-hook access" in error for error in validate_config(config))


def test_workspace_forbids_planned_as_confirmed():
    skill = (
        __import__("pathlib")
        .Path("integrations/openclaw/workspace/skills/doop-training/SKILL.md")
        .read_text()
        .lower()
    )
    assert "planned; not yet logged" in skill
    assert "confirmed” is forbidden" in skill
