import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
SCRIPT_ROOT = REPO_ROOT / "integrations" / "openclaw"
FAULT_SCRIPTS = (
    "run-offline-acceptance.sh",
    "run-broken-mcp-acceptance.sh",
)
ALL_DRILL_SCRIPTS = ("run-safety-acceptance.sh", *FAULT_SCRIPTS)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    call_log = tmp_path / "openclaw-calls.txt"
    fake_openclaw = tmp_path / "openclaw"
    _write_executable(
        fake_openclaw,
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$OPENCLAW_CALL_LOG"\n'
        'case "$*" in\n'
        '  *"config get mcp.servers.doop.enabled --json"*)\n'
        "    printf '%s\\n' \"$FAKE_ORIGINAL_ENABLED\"\n"
        "    ;;\n"
        '  *"config get mcp.servers.doop.command --json"*)\n'
        "    printf '%s\\n' '\"/synthetic/original-python\"'\n"
        "    ;;\n"
        "esac\n",
    )
    fake_python = tmp_path / "python"
    _write_executable(fake_python, "#!/bin/sh\nexit 0\n")
    return (
        {
            **os.environ,
            "DOOP_REPO_ROOT": str(REPO_ROOT),
            "DOOP_PYTHON": str(fake_python),
            "OPENCLAW_BIN": str(fake_openclaw),
            "OPENCLAW_CALL_LOG": str(call_log),
            "DOOP_OPENCLAW_PROFILE": "synthetic-fault-drill",
            "DOOP_ALLOW_CONFIG_MUTATION": "1",
            "DOOP_ACCEPTANCE_EXPORT_ROOT": str(tmp_path / "exports"),
            "FAKE_ORIGINAL_ENABLED": "true",
        },
        call_log,
    )


def _run(script_name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    arguments = [str(SCRIPT_ROOT / script_name)]
    if script_name == "run-safety-acceptance.sh":
        arguments.append("pain_or_injury")
    return subprocess.run(arguments, env=env, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("script_name", ALL_DRILL_SCRIPTS)
def test_drills_require_an_explicit_profile_before_calling_openclaw(tmp_path, script_name):
    env, call_log = _environment(tmp_path)
    del env["DOOP_OPENCLAW_PROFILE"]

    result = _run(script_name, env)

    assert result.returncode != 0
    assert "DOOP_OPENCLAW_PROFILE" in result.stderr
    assert not call_log.exists()


@pytest.mark.parametrize("script_name", FAULT_SCRIPTS)
def test_mutating_drills_preserve_the_explicit_opt_in(tmp_path, script_name):
    env, call_log = _environment(tmp_path)
    del env["DOOP_ALLOW_CONFIG_MUTATION"]

    result = _run(script_name, env)

    assert result.returncode != 0
    assert "DOOP_ALLOW_CONFIG_MUTATION" in result.stderr
    assert not call_log.exists()


@pytest.mark.parametrize("script_name", ALL_DRILL_SCRIPTS)
def test_every_openclaw_call_uses_the_isolated_profile(tmp_path, script_name):
    env, call_log = _environment(tmp_path)

    result = _run(script_name, env)

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls
    assert all(call.startswith("--profile synthetic-fault-drill ") for call in calls)


@pytest.mark.parametrize("original_enabled", ["true", "false"])
def test_offline_drill_restores_the_exact_original_enabled_state(tmp_path, original_enabled):
    env, call_log = _environment(tmp_path)
    env["FAKE_ORIGINAL_ENABLED"] = original_enabled

    result = _run("run-offline-acceptance.sh", env)

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    enabled_changes = [call for call in calls if "config set mcp.servers.doop.enabled" in call]
    assert enabled_changes == [
        "--profile synthetic-fault-drill config set mcp.servers.doop.enabled false",
        f"--profile synthetic-fault-drill config set mcp.servers.doop.enabled {original_enabled}",
    ]


def test_broken_command_drill_restores_the_exact_original_command(tmp_path):
    env, call_log = _environment(tmp_path)

    result = _run("run-broken-mcp-acceptance.sh", env)

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    command_changes = [call for call in calls if "config set mcp.servers.doop.command" in call]
    assert command_changes == [
        "--profile synthetic-fault-drill config set mcp.servers.doop.command /usr/bin/false",
        "--profile synthetic-fault-drill config set mcp.servers.doop.command /synthetic/original-python",
    ]
