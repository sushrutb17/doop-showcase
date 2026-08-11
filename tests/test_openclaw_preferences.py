import json
from pathlib import Path

import pytest

from whoop_local.integrations.openclaw_preferences import (
    render_user_md,
    validate_preferences,
)


def _preferences():
    return json.loads(open("integrations/openclaw/preferences.example.json").read())


def test_default_preferences_render_all_controls_and_safety_caps():
    rendered = render_user_md(_preferences())
    assert "Fictional training-preference template" in rendered
    assert "neutral fictional defaults" in rendered
    assert "Hype: **low**" in rendered
    assert "Playful roast: **off**" in rendered
    assert "00:00–00:00 Etc/UTC" in rendered
    assert "Disallowed language: none" in rendered
    assert "Proactive messages: **disabled**" in rendered
    assert "none for injury, illness, low-confidence evidence" in rendered
    assert "at most one playful line" in rendered
    assert rendered == Path("integrations/openclaw/workspace/USER.md").read_text()


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda p: p.update({"hype": "maximum"}), "hype"),
        (lambda p: p.update({"extra": True}), "keys mismatch"),
        (lambda p: p["quiet_hours"].update({"start": "25:00"}), "HH:MM"),
        (lambda p: p["quiet_hours"].update({"timezone": "Mars/Olympus"}), "IANA"),
        (lambda p: p.update({"proactive_messages": "yes"}), "boolean"),
    ],
)
def test_invalid_preferences_fail_closed(mutation, match):
    preferences = _preferences()
    mutation(preferences)
    with pytest.raises(ValueError, match=match):
        validate_preferences(preferences)


def test_workspace_rules_keep_persona_below_facts_and_safety():
    agents = open("integrations/openclaw/workspace/AGENTS.md").read().lower()
    soul = open("integrations/openclaw/workspace/SOUL.md").read().lower()
    assert "structured" in soul or "canonical" in soul
    assert "never joke about a safety report" in agents
    assert "at most one" in agents
