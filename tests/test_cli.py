from __future__ import annotations

from pathlib import Path

import pytest

from agent_relay import cli


def test_no_arguments_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert "usage: agent-relay" in output
    assert "config init server" in output
    assert "tools list" in output
    assert "doctor" in output


def test_help_and_version_are_top_level_only(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert "--version" in capsys.readouterr().out
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "agent-relay 0.1.0"

    with pytest.raises(SystemExit) as error:
        cli.main(["server", "--help"])
    assert error.value.code == 2


def test_config_option_can_be_global_before_or_after_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(path), "config", "init", "server"]) == 0
    capsys.readouterr()
    assert cli.main(["config", "get", "server", "--config", str(path)]) == 0
    assert "host:" in capsys.readouterr().out


def test_server_and_agent_are_the_only_runtime_dispatch_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received_server: list[list[str] | None] = []
    received_agent: list[list[str] | None] = []
    monkeypatch.setattr(
        cli.server, "main", lambda argv=None: received_server.append(argv)  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        cli.agent,
        "main",
        lambda argv=None, **_kwargs: received_agent.append(argv),
    )
    monkeypatch.setattr(cli.config, "load_server_runtime", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli.config, "load_agent_settings", lambda *_args, **_kwargs: object())

    path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(path), "server"]) == 0
    assert cli.main(["agent", "--config", str(path)]) == 0
    assert received_server == [["--config", str(path)]]
    assert received_agent == [["--config", str(path)]]


def test_legacy_runtime_commands_are_rejected() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["client"])
    assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        cli.main(["server", "run"])
    assert error.value.code == 2


def test_start_commands_return_configuration_error_status_for_missing_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "missing.yaml"
    assert cli.main(["--config", str(config_path), "server"]) == 1
    assert cli.main(["--config", str(config_path), "agent"]) == 1
    assert "error" in capsys.readouterr().err.lower()


def test_server_start_prints_sanitized_validation_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "SHARED_TOKEN_SENTINEL"
    monkeypatch.setenv("RELAY_MCP_TOKEN", secret)
    monkeypatch.setenv("RELAY_AGENT_TOKEN", secret)

    assert cli.main(["--config", str(tmp_path / "missing.yaml"), "server"]) == 1

    stderr = capsys.readouterr().err
    assert "mcp and agent tokens must be distinct" in stderr
    assert secret not in stderr


def test_application_configuration_failures_return_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--config", str(tmp_path / "missing.yaml"), "doctor"]) == 1
    captured = capsys.readouterr()
    assert "error" in (captured.out + captured.err).lower()
