from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_relay import cli


def test_unified_cli_requires_an_explicit_role(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main([]) == 2
    output = capsys.readouterr().out
    assert "usage:" in output
    assert "server" in output
    assert "agent" in output


def test_unified_cli_help_is_global(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "{server,client,agent}" in output
    assert "--host" not in output


@pytest.mark.parametrize(
    ("role", "expected_option"),
    [("server", "--host"), ("agent", "Agent Relay outbound agent")],
)
def test_unified_cli_role_help_delegates_without_configuration(
    role: str, expected_option: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main([role, "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "usage:" in output
    assert expected_option in output


def test_unified_cli_server_help_exposes_server_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["server", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--host" in output
    assert "--port" in output


def test_unified_cli_delegates_server_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[list[str] | None] = []
    monkeypatch.setattr(
        cli.server, "main", lambda argv=None: received.append(argv)  # type: ignore[arg-type]
    )

    arguments = ["--port", "9000", "--", "unchanged"]
    assert cli.main(["server", *arguments]) == 0
    assert received == [arguments]


def test_unified_cli_accepts_server_run_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[list[str] | None] = []
    monkeypatch.setattr(
        cli.server, "main", lambda argv=None: received.append(argv)  # type: ignore[arg-type]
    )

    arguments = ["--port", "9000"]
    assert cli.main(["server", "run", *arguments]) == 0
    assert received == [arguments]


def test_unified_cli_accepts_client_run_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[list[str] | None] = []
    monkeypatch.setattr(
        cli.agent, "main", lambda argv=None: received.append(argv)  # type: ignore[arg-type]
    )

    arguments = ["--help"]
    assert cli.main(["client", "run", *arguments]) == 0
    assert received == [arguments]


def _configure_valid_client_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> str:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token_file = tmp_path / "agent.token"
    token_file.write_text("client-secret\n", encoding="utf-8")
    token_file.chmod(0o600)
    values = {
        "AGENT_RELAY_SERVER_URL": "wss://relay.example.test/ws/agent",
        "AGENT_RELAY_DEVICE_ID": "windows-laptop-1",
        "AGENT_RELAY_WORKSPACE": str(workspace),
        "AGENT_RELAY_AGENT_TOKEN_FILE": str(token_file),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return str(token_file)


def test_client_config_validate_checks_environment_without_printing_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_valid_client_environment(monkeypatch, tmp_path)

    assert cli.main(["client", "config", "validate"]) == 0
    output = capsys.readouterr().out
    assert output == "client configuration is valid\n"
    assert "client-secret" not in output


def test_client_config_show_redacts_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_valid_client_environment(monkeypatch, tmp_path)

    assert cli.main(["client", "config", "show"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["device_id"] == "windows-laptop-1"
    assert payload["agent_token"] == "[REDACTED]"
    assert "client-secret" not in json.dumps(payload)


def test_client_config_init_creates_private_non_secret_template(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / ".env.client.example"

    assert (
        cli.main(
            ["client", "config", "init", "--output", str(output_path)]
        )
        == 0
    )
    assert output_path.read_text(encoding="utf-8").count("AGENT_RELAY_") >= 4
    assert "client-secret" not in output_path.read_text(encoding="utf-8")
    assert os.stat(output_path).st_mode & 0o777 == 0o600
    assert "created" in capsys.readouterr().out


def test_client_config_init_refuses_to_overwrite_existing_file(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / ".env.client.example"
    output_path.write_text("keep\n", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        cli.main(
            ["client", "config", "init", "--output", str(output_path)]
        )

    assert error.value.code == 2
    assert output_path.read_text(encoding="utf-8") == "keep\n"


def test_unified_cli_delegates_agent_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[list[str] | None] = []
    monkeypatch.setattr(
        cli.agent, "main", lambda argv=None: received.append(argv)  # type: ignore[arg-type]
    )

    assert cli.main(["agent"]) == 0
    assert received == [[]]
