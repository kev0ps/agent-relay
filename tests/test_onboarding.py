from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

from agent_relay import cli, config
from agent_relay.catalog import CatalogSnapshot

EMPTY_CATALOG = CatalogSnapshot((), ())


def _private_token_file(path: Path, value: str = "remote-agent-secret") -> Path:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_local_onboarding_creates_both_valid_sections_without_printing_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / ".agent-relay" / "config.yaml"

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "onboard",
                "--role",
                "local",
                "--non-interactive",
                "--policy",
                "loopback",
                "--no-tools",
            ],
            catalog=EMPTY_CATALOG,
        )
        == 0
    )

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert set(document) == {"server", "agent"}
    assert document["server"]["host"] == "127.0.0.1"
    assert document["agent"]["tools"]["allowlist"] == []
    dotenv_values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (config_path.parent / ".env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    agent_secret = dotenv_values["RELAY_AGENT_TOKEN"]
    mcp_secret = dotenv_values["RELAY_MCP_TOKEN"]
    assert agent_secret != mcp_secret
    output = capsys.readouterr()
    assert agent_secret.strip() not in output.out
    assert mcp_secret.strip() not in output.out
    assert "MCP and Agent credentials are distinct" in output.out


def test_server_only_onboarding_does_not_create_an_agent_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "onboard",
                "--role",
                "server",
                "--non-interactive",
                "--policy",
                "lan",
            ],
            catalog=EMPTY_CATALOG,
        )
        == 0
    )

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert set(document) == {"server"}
    assert document["server"]["host"] == "0.0.0.0"
    assert "Agent administrator" in capsys.readouterr().out


def test_interactive_role_selection_uses_safe_server_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("2\n\n\n\n"))
    config_path = tmp_path / "config.yaml"

    assert cli.main(["--config", str(config_path), "onboard"], catalog=EMPTY_CATALOG) == 0

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert set(document) == {"server"}
    assert document["server"]["host"] == "127.0.0.1"
    assert "Server only" in capsys.readouterr().out


def test_remote_agent_onboarding_masks_file_secret_and_validates_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    token_file = _private_token_file(tmp_path / "incoming-agent-token")
    workspace = tmp_path / "agent-workspace"

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "onboard",
                "--role",
                "agent",
                "--non-interactive",
                "--relay-url",
                "wss://relay.example.test/ws/agent?ignored=secret",
                "--token-file",
                str(token_file),
                "--workspace",
                str(workspace),
                "--no-tools",
                "--deny-insecure-ws",
                "--no-check",
            ],
            catalog=EMPTY_CATALOG,
        )
        == 0
    )

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["agent"]["allow_insecure_ws"] is False
    assert document["agent"]["workspace"] == str(workspace)
    assert token_file.read_text(encoding="utf-8").strip() not in capsys.readouterr().out
    assert "RELAY_AGENT_TOKEN=remote-agent-secret" in (
        config_path.parent / ".env"
    ).read_text(encoding="utf-8")


def test_remote_agent_onboarding_rejects_plaintext_remote_url_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = _private_token_file(tmp_path / "token")
    result = cli.main(
        [
            "--config",
            str(tmp_path / "config.yaml"),
            "onboard",
            "--role",
            "agent",
            "--non-interactive",
            "--relay-url",
            "ws://relay.example.test/ws/agent",
            "--token-file",
            str(token_file),
            "--no-tools",
        ],
        catalog=EMPTY_CATALOG,
    )

    assert result == 1
    assert "non-loopback ws:// requires allow_insecure_ws" in capsys.readouterr().err


def test_onboarding_cancellation_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    assert (
        cli.main(
            [
                "--config",
                str(tmp_path / "config.yaml"),
                "onboard",
                "--role",
                "server",
            ],
            catalog=EMPTY_CATALOG,
        )
        == 1
    )
    assert "onboarding cancelled" in capsys.readouterr().err


def test_noninteractive_onboarding_requires_a_role(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.main(
            [
                "--config",
                str(tmp_path / "config.yaml"),
                "onboard",
                "--non-interactive",
            ],
            catalog=EMPTY_CATALOG,
        )
        == 1
    )
    assert "requires --role" in capsys.readouterr().err


def test_agent_only_default_policy_is_strict_for_existing_yaml(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    token_file = _private_token_file(tmp_path / "token")
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "onboard",
                "--role",
                "agent",
                "--non-interactive",
                "--relay-url",
                "wss://relay.example.test/ws/agent",
                "--token-file",
                str(token_file),
                "--no-tools",
            ],
            catalog=EMPTY_CATALOG,
        )
        == 0
    )
    settings = config.load_agent_settings(config_path, catalog=EMPTY_CATALOG)
    assert settings.allow_insecure_ws is False
