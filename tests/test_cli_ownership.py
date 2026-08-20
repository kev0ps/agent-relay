from __future__ import annotations

from pathlib import Path

from agent_relay import cli, config


def test_cli_is_the_only_terminal_interaction_module() -> None:
    package_dir = Path(cli.__file__).parent

    assert not (package_dir / "onboarding.py").exists()

    config_source = Path(config.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "input(",
        "print(",
        "getpass",
        "sys.stdin",
        ".isatty()",
        "--yes",
        "--stdin",
        "discover_local_catalog(",
    ):
        assert forbidden not in config_source


def test_config_keeps_secret_redaction_as_deterministic_data_logic() -> None:
    value = {
        "nested": {"agent_token": "agent-secret", "name": "safe"},
        "values": [{"api_key": "api-secret"}],
    }

    assert config.redact_for_output(value) == {
        "nested": {"agent_token": "[REDACTED]", "name": "safe"},
        "values": [{"api_key": "[REDACTED]"}],
    }
