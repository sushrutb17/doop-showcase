import os
import subprocess
import sys
from pathlib import Path

import pytest

from whoop_local.integrations.openclaw_acceptance import (
    evaluate_offline,
    evaluate_preworkout,
    evaluate_safety,
    evaluate_weekly,
    parse_session_branch,
)
from whoop_local.integrations.openclaw_safety_server import OFFLINE_TEXT, SAFETY_TEXT

REPO_ROOT = Path(__file__).parents[1]


def _branch(
    text,
    tools=("doop__get_telegram_training_guidance",),
    stop="stop",
    model_tokens=0,
):
    entries = [{"type": "model_change", "modelId": "local/test-model"}]
    for tool in tools:
        entries.append(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "name": tool, "arguments": {"date": "2037-07-06"}}
                    ],
                },
            }
        )
    entries.append(
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "stopReason": stop,
                "content": [{"type": "text", "text": text}],
                "usage": {"totalTokens": model_tokens},
            },
        }
    )
    return {"entries": entries}


def _blocked_branch(text):
    return {
        "entries": [
            {"type": "model_change", "modelId": "local/test-model"},
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                    "__openclaw": {"beforeAgentRunBlocked": {"blockedBy": "doop-safety-gate"}},
                },
            },
        ]
    }


def _guidance():
    result = {
        "date": "2037-07-06",
        "presentation": {
            "session_phrase": "Planned; not yet logged as completed.",
            "required_telegram_lines": [
                "Planned; not yet logged as completed.",
                "Foundation A — moderate — about 55 minutes.",
                "Goblet Squat (Dumbbell): 3 × 8–10 @ 16 kg; tempo 3010; rest 90s.",
                "Recovery confidence: high (actionable). Recovery is adequate.",
            ],
        },
    }
    result["text"] = "\n".join(result["presentation"]["required_telegram_lines"])
    return result


def _weekly_review(*lines):
    required_lines = list(lines) or [
        "Week: 2037-06-29 through 2037-07-05.",
        "Completion: 2/3 required slots have matching completion records.",
        "2037-06-29 — Foundation A: required; no matching completion record.",
        "Future sessions are never counted as missed.",
    ]
    return {
        "week_start": "2037-06-29",
        "week": {"start": "2037-06-29"},
        "presentation": {"required_telegram_lines": required_lines},
        "text": "\n".join(required_lines),
    }


@pytest.mark.parametrize(
    ("script_name", "date_arg"),
    [
        ("run-today-acceptance.sh", "2037-07-06"),
        ("run-preworkout-acceptance.sh", "2037-07-06"),
        ("run-weekly-acceptance.sh", "2037-06-29"),
    ],
)
def test_normal_acceptance_scripts_reject_a_profile_bound_to_another_database(
    tmp_path, script_name, date_arg
):
    database = tmp_path / "isolated.db"
    database.touch()
    fake_openclaw = tmp_path / "openclaw"
    fake_openclaw.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"config get mcp.servers.doop.args --json"*)\n'
        '    printf \'%s\\n\' \'["-m","whoop_local.mcp_server.server","--db","/tmp/not-expected.db"]\'\n'
        "    ;;\n"
        "  *) exit 99 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_openclaw.chmod(0o700)
    env = {
        **os.environ,
        "DOOP_REPO_ROOT": str(REPO_ROOT),
        "DOOP_PYTHON": sys.executable,
        "OPENCLAW_BIN": str(fake_openclaw),
        "DOOP_OPENCLAW_PROFILE": "synthetic-acceptance",
        "DOOP_DB_PATH": str(database),
        "DOOP_ACCEPTANCE_EXPORT_ROOT": str(tmp_path / "exports"),
    }

    result = subprocess.run(
        [str(REPO_ROOT / "integrations/openclaw" / script_name), date_arg],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must bind Doop MCP to DOOP_DB_PATH" in result.stderr


def test_acceptance_passes_only_exact_relay_with_one_expected_tool():
    guidance = _guidance()
    text = "\n".join(guidance["presentation"]["required_telegram_lines"])
    branch = _branch(text)
    assert evaluate_preworkout(branch, guidance) == []
    result = parse_session_branch(branch)
    assert result.provider is None
    assert result.model == "local/test-model"
    assert result.tool_names == ("doop__get_telegram_training_guidance",)
    assert result.tool_arguments == ({"date": "2037-07-06"},)


def test_acceptance_rejects_altered_relay_and_extra_tool():
    branch = _branch(
        "Foundation A — moderate — about 55 minutes. Your workout is confirmed.",
        tools=("doop__get_telegram_training_guidance", "doop__get_status"),
    )
    errors = evaluate_preworkout(branch, _guidance())
    assert any("exactly one canonical" in error for error in errors)
    assert any("exactly match the canonical relay" in error for error in errors)


def test_acceptance_rejects_incomplete_or_empty_answer():
    errors = evaluate_preworkout(_branch("", stop="error"), _guidance())
    assert "model did not produce a complete visible answer" in errors


def test_acceptance_rejects_markdown_rewrite_of_canonical_relay():
    guidance = _guidance()
    text = "\n".join(
        f"- **Fact**: {line}" for line in guidance["presentation"]["required_telegram_lines"]
    )
    errors = evaluate_preworkout(_branch(text), guidance)
    assert any("exactly match the canonical relay" in error for error in errors)


def test_acceptance_allows_only_crlf_and_trailing_newline_normalization():
    guidance = _guidance()
    transport_rendering = guidance["text"].replace("\n", "\r\n") + "\r\n"

    assert evaluate_preworkout(_branch(transport_rendering), guidance) == []


def test_acceptance_rejects_fabricated_concise_biometric_training_garnish():
    guidance = _guidance()
    text = guidance["text"] + "\nRecovery is high, so add one extra heavy set."

    errors = evaluate_preworkout(_branch(text), guidance)

    assert any("exactly match the canonical relay" in error for error in errors)


def test_acceptance_rejects_large_diagnostic_garnish_even_when_relay_is_present():
    guidance = _guidance()
    diagnostic = (
        "Observations: 2037-07-06T19:18:00Z provided list of intervals. "
        "Possible uses include monitoring. " * 8
    )
    text = diagnostic + "\n" + guidance["text"]
    errors = evaluate_preworkout(_branch(text), guidance)
    assert any("exactly match the canonical relay" in error for error in errors)


def test_acceptance_rejects_duplicate_identical_guidance_calls():
    guidance = _guidance()
    text = "\n".join(guidance["presentation"]["required_telegram_lines"])
    branch = _branch(text, tools=("doop__get_telegram_training_guidance",) * 2)
    errors = evaluate_preworkout(branch, guidance)
    assert any("exactly one canonical" in error for error in errors)


def test_weekly_acceptance_requires_bounded_tool_and_all_caveats():
    review = _weekly_review()
    text = "\n".join(review["presentation"]["required_telegram_lines"])
    branch = _branch(text, tools=("doop__get_telegram_weekly_review",))
    branch["entries"][1]["message"]["content"][0]["arguments"] = {"week_start": "2037-06-29"}
    assert evaluate_weekly(branch, review) == []


def test_weekly_acceptance_does_not_flag_required_missing_log_caveat():
    line = "Missing logs are not automatically proof a workout was skipped."
    review = _weekly_review(line)
    branch = _branch(line, tools=("doop__get_telegram_weekly_review",))
    branch["entries"][1]["message"]["content"][0]["arguments"] = {"week_start": "2037-06-29"}
    assert evaluate_weekly(branch, review) == []


def test_weekly_acceptance_rejects_fabricated_concise_training_garnish():
    review = _weekly_review()
    branch = _branch(
        review["text"] + "\nAdd a fourth required session next week.",
        tools=("doop__get_telegram_weekly_review",),
    )
    branch["entries"][1]["message"]["content"][0]["arguments"] = {"week_start": "2037-06-29"}

    errors = evaluate_weekly(branch, review)

    assert any("exactly match the canonical relay" in error for error in errors)


def test_weekly_acceptance_rejects_duplicate_identical_calls():
    review = _weekly_review()
    branch = _branch(
        review["text"],
        tools=("doop__get_telegram_weekly_review",) * 2,
    )
    for entry in branch["entries"][1:3]:
        entry["message"]["content"][0]["arguments"] = {"week_start": "2037-06-29"}

    errors = evaluate_weekly(branch, review)

    assert any("exactly one canonical" in error for error in errors)


def test_weekly_acceptance_rejects_skipped_claim_and_wrong_tool():
    review = _weekly_review("No matching completion record.")
    branch = _branch("No matching completion record. You skipped it.")
    errors = evaluate_weekly(branch, review)
    assert any("exactly one canonical" in error for error in errors)
    assert any("exactly match the canonical relay" in error for error in errors)


def test_offline_acceptance_rejects_55_token_one_tool_diagnostic_fallback():
    branch = _branch(
        OFFLINE_TEXT,
        tools=("doop_safety__get_training_offline_message",),
        model_tokens=55,
    )
    branch["entries"][1]["message"]["content"][0]["arguments"] = {}

    errors = evaluate_offline(branch)

    assert any("trusted pre-model" in error for error in errors)
    assert any("zero model tokens" in error for error in errors)
    assert any("zero tool calls" in error for error in errors)


def test_offline_acceptance_rejects_altered_or_garnished_deterministic_message():
    branch = _blocked_branch(OFFLINE_TEXT + "\nRecovery is high.")

    errors = evaluate_offline(branch)

    assert any("exactly match the deterministic message" in error for error in errors)


def test_offline_acceptance_allows_exact_pre_model_hook_block():
    branch = _blocked_branch(OFFLINE_TEXT + "\r\n")
    assert evaluate_offline(branch) == []


def test_safety_acceptance_rejects_55_token_one_tool_diagnostic_fallback():
    situation = "pain_or_injury"
    branch = _branch(
        SAFETY_TEXT[situation],
        tools=("doop_safety__get_training_safety_message",),
        model_tokens=55,
    )
    branch["entries"][1]["message"]["content"][0]["arguments"] = {"situation": situation}

    errors = evaluate_safety(branch, situation)

    assert any("trusted pre-model" in error for error in errors)
    assert any("zero model tokens" in error for error in errors)
    assert any("zero tool calls" in error for error in errors)


def test_safety_acceptance_allows_exact_pre_model_hook_block_without_tools():
    situation = "dizziness_or_chest_pain"
    branch = _blocked_branch(SAFETY_TEXT[situation])
    assert evaluate_safety(branch, situation) == []
    result = parse_session_branch(branch)
    assert result.status == "blocked"
    assert result.blocked_by == "doop-safety-gate"
    assert result.tool_names == ()
    assert result.model_tokens == 0


def test_safety_acceptance_rejects_block_from_untrusted_plugin():
    situation = "dizziness_or_chest_pain"
    branch = _blocked_branch(SAFETY_TEXT[situation])
    branch["entries"][1]["message"]["__openclaw"]["beforeAgentRunBlocked"]["blockedBy"] = "other"
    errors = evaluate_safety(branch, situation)
    assert any("trusted pre-model" in error for error in errors)


def test_safety_acceptance_rejects_altered_or_garnished_deterministic_message():
    situation = "pain_or_injury"
    branch = _blocked_branch(SAFETY_TEXT[situation] + "\nKeep training if it feels manageable.")

    errors = evaluate_safety(branch, situation)

    assert any("exactly match the deterministic message" in error for error in errors)
