"""Diagnostic, non-promoted safety/degradation fallback tools for OpenClaw Doop.

The promoted route is the pre-model ``doop-safety-gate`` hook. A model-mediated
call to this server is defense-in-depth diagnostics and cannot satisfy promoted
acceptance.
"""

import json
from importlib.resources import files
from typing import Literal

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("doop-safety")

_MESSAGES = json.loads(
    files("whoop_local.integrations").joinpath("safety_messages.json").read_text(encoding="utf-8")
)
OFFLINE_TEXT: str = _MESSAGES["offline"]
SAFETY_TEXT: dict[str, str] = _MESSAGES["safety"]


@mcp.tool()
def get_training_offline_message() -> dict:
    """Diagnostic fallback only; this does not satisfy promoted acceptance."""
    return {
        "text": OFFLINE_TEXT,
        "relay_contract": (
            "Diagnostic, non-promoted fallback only. Return text verbatim and do not add "
            "training or biometric facts."
        ),
    }


@mcp.tool()
def get_training_safety_message(
    situation: Literal["pain_or_injury", "illness_or_severe_fatigue", "dizziness_or_chest_pain"],
) -> dict:
    """Diagnostic fallback only; the promoted path blocks before inference."""
    return {
        "text": "Doop safety guidance:\n" + SAFETY_TEXT[situation],
        "relay_contract": (
            "Diagnostic, non-promoted fallback only. Return text verbatim with no joke, hype, "
            "diagnosis, or extra training advice."
        ),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
