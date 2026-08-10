from __future__ import annotations

import io
from pathlib import Path

import pytest

from agent_relay import cli


def test_server_and_agent_are_the_only_runtime_dispatch_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received_server: list[list[str] | None] = []
    received_agent: list[list[str] | None] = []
    monkeypatch.setattr(
        cli.server,
        "main",
        lambda argv=None: received_server.append(argv),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        cli.agent,
        "main",
        lambda argv=None, **_kwargs: received_agent.append(argv),
    )
    monkeypatch.setattr(
        cli.config,
        "load_server_runtime",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        cli.config,
        "load_agent_settings",
        lambda *_args, **_kwargs: object(),
    )

    path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(path), "server"]) == 0
    assert cli.main(["agent", "--config", str(path)]) == 0
    assert received_server == [["--config", str(path)]]
    assert received_agent == [["--config", str(path)]]


def test_agent_can_use_runtime_environment_without_default_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received_agent: list[list[str] | None] = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cli.config, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(
        cli.config,
        "discover_local_catalog",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        cli.agent,
        "main",
        lambda argv=None, **_kwargs: received_agent.append(argv),
    )
    monkeypatch.setenv("RELAY_URL", "ws://127.0.0.1:8000/ws/agent")
    monkeypatch.setenv("RELAY_AGENT_WORKSPACE", str(workspace))
    monkeypatch.setenv("RELAY_AGENT_TOKEN", "agent-token")

    assert cli.main(["agent"]) == 0
    assert received_agent == [[]]


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


def test_agent_runtime_can_start_with_zero_selected_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("sys.stdin", io.StringIO("agent-secret\n"))
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "config",
                "init",
                "agent",
                "--no-tools",
                "--stdin",
            ]
        )
        == 0
    )

    from agent_relay.agent import RelayAgent
    from agent_relay.config import load_agent_settings

    settings = load_agent_settings(config_path)
    relay_agent = RelayAgent(settings)
    assert relay_agent._capabilities == {}
