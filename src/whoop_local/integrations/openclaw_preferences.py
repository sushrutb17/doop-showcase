"""Validate and render user-local OpenClaw persona preferences."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CHOICES = {
    "hype": {"low", "medium", "high"},
    "playful_roast": {"off", "light"},
    "verbosity": {"concise", "standard"},
    "accountability": {"low", "medium", "high"},
}
REQUIRED_KEYS = set(CHOICES) | {"quiet_hours", "disallowed_language", "proactive_messages"}
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def validate_preferences(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("preferences must be a JSON object")
    unknown = set(value) - REQUIRED_KEYS
    missing = REQUIRED_KEYS - set(value)
    if unknown or missing:
        raise ValueError(
            f"preference keys mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    for key, allowed in CHOICES.items():
        if value[key] not in allowed:
            raise ValueError(f"{key} must be one of {sorted(allowed)}")
    quiet = value["quiet_hours"]
    if not isinstance(quiet, dict) or set(quiet) != {"start", "end", "timezone"}:
        raise ValueError("quiet_hours must contain exactly start, end, and timezone")
    if not TIME_RE.fullmatch(str(quiet["start"])) or not TIME_RE.fullmatch(str(quiet["end"])):
        raise ValueError("quiet-hour start/end must use 24-hour HH:MM")
    try:
        ZoneInfo(str(quiet["timezone"]))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("quiet_hours.timezone must be a valid IANA zone") from exc
    language = value["disallowed_language"]
    if not isinstance(language, list) or any(
        not isinstance(item, str) or not item.strip() or len(item) > 40 for item in language
    ):
        raise ValueError(
            "disallowed_language must be a list of non-empty strings up to 40 characters"
        )
    if len(language) > 30:
        raise ValueError("disallowed_language is limited to 30 entries")
    if not isinstance(value["proactive_messages"], bool):
        raise ValueError("proactive_messages must be boolean")
    return value


def render_user_md(preferences: dict) -> str:
    p = validate_preferences(preferences)
    quiet = p["quiet_hours"]
    disallowed = ", ".join(f"`{word}`" for word in p["disallowed_language"]) or "none"
    proactive = "enabled" if p["proactive_messages"] else "disabled"
    return f"""# Fictional training-preference template

- Athlete schedule, units, and constraints come from the canonical local plan.
- Values below are neutral fictional defaults, not an owner profile or health context.
- Copy and customize them only in private user-local configuration.

## Persona controls

- Hype: **{p["hype"]}**.
- Playful roast: **{p["playful_roast"]}**. Never shame, humiliate, or joke about safety.
- Verbosity: **{p["verbosity"]}**.
- Accountability: **{p["accountability"]}**.
- Quiet hours: **{quiet["start"]}–{quiet["end"]} {quiet["timezone"]}**.
- Proactive messages: **{proactive}**. This does not override the separately disabled heartbeat/scheduler.
- Disallowed language: {disallowed}.

Apply persona only after canonical Doop facts. Use at most one playful line in a
routine response and none for injury, illness, low-confidence evidence,
discouragement, or return from a gap. Training facts still come from
Doop tools, never from this preference profile or chat memory.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    preferences = json.loads(args.input.read_text())
    rendered = render_user_md(preferences)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    print(args.output)


if __name__ == "__main__":
    main()
