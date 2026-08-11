import json
from importlib.resources import files
from pathlib import Path

from whoop_local.integrations.openclaw_safety_server import (
    OFFLINE_TEXT,
    SAFETY_TEXT,
    get_training_offline_message,
    get_training_safety_message,
)


def test_python_and_plugin_safety_message_resources_match():
    packaged = json.loads(
        files("whoop_local.integrations")
        .joinpath("safety_messages.json")
        .read_text(encoding="utf-8")
    )
    plugin_path = (
        Path(__file__).parents[1]
        / "integrations/openclaw/plugins/doop-safety-gate/safety-messages.json"
    )

    assert json.loads(plugin_path.read_text(encoding="utf-8")) == packaged


def test_offline_message_is_fact_free_and_minimum_disclosure():
    result = get_training_offline_message()
    assert result["text"] == OFFLINE_TEXT
    assert "Training data is offline" in result["text"]
    assert not any(unit in result["text"] for unit in ("%", " bpm", " lb", " kg"))
    assert set(result) == {"text", "relay_contract"}


def test_safety_messages_are_deterministic_and_never_motivational():
    for situation, text in SAFETY_TEXT.items():
        result = get_training_safety_message(situation)
        assert result["text"] == "Doop safety guidance:\n" + text
        assert not any(word in text.lower() for word in ("bro", "crush", "no pain no gain", "lol"))
        assert set(result) == {"text", "relay_contract"}
