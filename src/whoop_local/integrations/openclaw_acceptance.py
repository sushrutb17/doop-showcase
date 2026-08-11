"""Evaluate a redacted OpenClaw trajectory against Doop's launch contract."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from whoop_local.db.schema import connect
from whoop_local.integrations.openclaw_safety_server import OFFLINE_TEXT, SAFETY_TEXT
from whoop_local.mcp_server.queries import (
    get_telegram_training_guidance,
    get_telegram_weekly_review,
)


@dataclass(frozen=True)
class TrajectoryResult:
    provider: str | None
    model: str | None
    final_text: str
    tool_names: tuple[str, ...]
    tool_arguments: tuple[dict, ...]
    status: str | None
    blocked_by: str | None
    model_tokens: int


def parse_session_branch(branch: dict) -> TrajectoryResult:
    """Extract only evidence needed for acceptance from an exported branch."""
    model = None
    provider = None
    final_text = ""
    tools: list[str] = []
    tool_arguments: list[dict] = []
    status = None
    blocked_by = None
    model_tokens = 0
    for entry in branch.get("entries", []):
        if entry.get("type") == "model_change":
            provider = entry.get("provider")
            model = entry.get("modelId")
        if entry.get("type") != "message":
            continue
        message = entry.get("message", {})
        role = message.get("role")
        block = message.get("__openclaw", {}).get("beforeAgentRunBlocked")
        if role == "user" and isinstance(block, dict):
            texts = [
                item.get("text", "")
                for item in message.get("content", [])
                if item.get("type") == "text"
            ]
            if texts:
                final_text = "\n".join(texts)
                status = "blocked"
                blocked_by = block.get("blockedBy")
        if role == "assistant":
            usage = message.get("usage", {})
            total_tokens = usage.get("totalTokens", 0)
            if isinstance(total_tokens, int):
                model_tokens += total_tokens
            texts = [
                item.get("text", "")
                for item in message.get("content", [])
                if item.get("type") == "text"
            ]
            if texts:
                final_text = "\n".join(texts)
                status = message.get("stopReason")
            for item in message.get("content", []):
                if item.get("type") == "toolCall":
                    tools.append(item.get("name", ""))
                    tool_arguments.append(item.get("arguments", {}))
    return TrajectoryResult(
        provider,
        model,
        final_text,
        tuple(tools),
        tuple(tool_arguments),
        status,
        blocked_by,
        model_tokens,
    )


def _normalize_relay_text(text: str) -> str:
    """Normalize transport-only differences without changing relay content."""
    return text.replace("\r\n", "\n").rstrip("\n")


def _check_exact_tool_call(
    result: TrajectoryResult,
    expected_tool: str,
    expected_arguments: dict,
) -> list[str]:
    if result.tool_names == (expected_tool,) and result.tool_arguments == (expected_arguments,):
        return []
    return [
        f"expected exactly one canonical {expected_tool} call; "
        "trajectory contained an unexpected count, name, or arguments"
    ]


def _check_exact_relay(answer: str, evidence: dict) -> list[str]:
    expected = evidence.get("text")
    if not isinstance(expected, str) or not expected:
        return ["canonical relay text is missing from acceptance evidence"]
    if _normalize_relay_text(answer) == _normalize_relay_text(expected):
        return []
    return [
        "visible answer must exactly match the canonical relay text; "
        "extra, omitted, or altered text is not allowed"
    ]


def _check_promoted_hook_block(
    result: TrajectoryResult,
    expected_text: str,
    scenario: str,
) -> list[str]:
    """Require the trusted zero-inference path used by promoted safety runs."""
    errors: list[str] = []
    if result.status != "blocked" or result.blocked_by != "doop-safety-gate":
        errors.append(
            f"promoted {scenario} acceptance requires a trusted pre-model doop-safety-gate block"
        )
    if result.model_tokens != 0:
        errors.append(f"promoted {scenario} acceptance requires zero model tokens")
    if result.tool_names or result.tool_arguments:
        errors.append(f"promoted {scenario} acceptance requires zero tool calls")
    if _normalize_relay_text(result.final_text) != _normalize_relay_text(expected_text):
        errors.append(
            f"promoted {scenario} visible text must exactly match the deterministic message; "
            "extra, omitted, or altered text is not allowed"
        )
    return errors


def evaluate_preworkout(branch: dict, guidance: dict) -> list[str]:
    """Return deterministic launch-contract violations for one planned session."""
    result = parse_session_branch(branch)
    errors: list[str] = []
    if result.status != "stop" or not result.final_text.strip():
        errors.append("model did not produce a complete visible answer")
    expected_tool = "doop__get_telegram_training_guidance"
    expected_date = guidance.get("date")
    errors.extend(_check_exact_tool_call(result, expected_tool, {"date": expected_date}))
    errors.extend(_check_exact_relay(result.final_text, guidance))
    return errors


def evaluate_weekly(branch: dict, review: dict) -> list[str]:
    result = parse_session_branch(branch)
    errors: list[str] = []
    if result.status != "stop" or not result.final_text.strip():
        errors.append("model did not produce a complete visible answer")
    expected_tool = "doop__get_telegram_weekly_review"
    week_start = review.get("week_start") or review.get("week", {}).get("start")
    errors.extend(_check_exact_tool_call(result, expected_tool, {"week_start": week_start}))
    errors.extend(_check_exact_relay(result.final_text, review))
    return errors


def evaluate_offline(branch: dict) -> list[str]:
    """Accept only the promoted pre-model, fact-free offline block."""
    result = parse_session_branch(branch)
    return _check_promoted_hook_block(result, OFFLINE_TEXT, "offline")


def evaluate_safety(branch: dict, situation: str) -> list[str]:
    """Accept only the promoted pre-model block for a known safety class."""
    if situation not in SAFETY_TEXT:
        return [f"unknown safety situation: {situation}"]
    result = parse_session_branch(branch)
    return _check_promoted_hook_block(result, SAFETY_TEXT[situation], "safety")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trajectory",
        type=Path,
        required=True,
        help="OpenClaw export directory or session-branch.json",
    )
    parser.add_argument("--db", type=Path)
    parser.add_argument(
        "--scenario", choices=("preworkout", "weekly", "offline", "safety"), default="preworkout"
    )
    parser.add_argument("--date", help="planned session date (YYYY-MM-DD)")
    parser.add_argument("--week-start", help="weekly review Monday (YYYY-MM-DD)")
    parser.add_argument("--situation", choices=tuple(SAFETY_TEXT))
    parser.add_argument("--expected-provider", help="require the final trajectory model provider")
    args = parser.parse_args()

    branch_path = args.trajectory
    if branch_path.is_dir():
        branch_path = branch_path / "session-branch.json"
    branch = json.loads(branch_path.read_text())
    if args.scenario == "offline":
        errors = evaluate_offline(branch)
    elif args.scenario == "safety":
        if not args.situation:
            parser.error("--situation is required for safety")
        errors = evaluate_safety(branch, args.situation)
    else:
        if not args.db:
            parser.error("--db is required for preworkout and weekly scenarios")
        conn = connect(args.db)
        try:
            if args.scenario == "preworkout":
                if not args.date:
                    parser.error("--date is required for preworkout")
                evidence = get_telegram_training_guidance(conn, args.date)
                errors = evaluate_preworkout(branch, evidence)
            else:
                if not args.week_start:
                    parser.error("--week-start is required for weekly")
                evidence = get_telegram_weekly_review(conn, args.week_start)
                errors = evaluate_weekly(branch, evidence)
        finally:
            conn.close()
    result = parse_session_branch(branch)
    if args.expected_provider and result.provider != args.expected_provider:
        errors.append(
            f"expected model provider {args.expected_provider}; got {result.provider or 'missing'}"
        )
    report = {
        "passed": not errors,
        "provider": result.provider,
        "model": result.model,
        "tools": list(result.tool_names),
        "errors": errors,
    }
    print(json.dumps(report, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
