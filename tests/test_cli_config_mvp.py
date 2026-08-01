from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
import yaml

from agent_relay import cli, config


def _invoke(
    config_path: Path,
    *args: str,
    monkeypatch: pytest.MonkeyPatch | None = None,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    if monkeypatch is not None and stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    # The CLI writes directly to the process streams; pytest's capsys is used by
    # individual tests when output assertions are needed.
    return cli.main(["--config", str(config_path), *args]), "", ""


def test_no_arguments_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert "usage: agent-relay" in output
    assert "config" in output
    assert "tools" in output
    assert "doctor" in output


def test_version_does_not_require_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "agent-relay 0.1.0"


def test_role_help_is_not_a_supported_legacy_command() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["server", "--help"])
    assert error.value.code == 2


def test_init_server_creates_private_yaml_and_secret_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / ".agent-relay" / "config.yaml"

    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0

    assert config_path.is_file()
    assert os.stat(config_path).st_mode & 0o777 == 0o600
    assert (config_path.parent / "secrets/server/mcp_token").is_file()
    assert (config_path.parent / "secrets/server/agent_token").is_file()
    assert "token" not in capsys.readouterr().out.lower()


def test_init_rejects_symlinked_secret_parent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / "config-parent"
    outside = tmp_path / "outside"
    config_dir.mkdir()
    outside.mkdir()
    try:
        os.symlink(outside, config_dir / "secrets", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    config_path = config_dir / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 1
    assert not (outside / "server/mcp_token").exists()
    assert "symlink" in capsys.readouterr().err.lower()


def test_empty_canonical_token_environment_override_is_rejected(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "server", env={})
    report = config.validate_document(config_path, "server", env={"RELAY_MCP_TOKEN": ""})
    assert not report.valid
    with pytest.raises(config.ConfigError):
        config.load_server_runtime(config_path, env={"RELAY_MCP_TOKEN": ""})


def test_yaml_boolean_and_environment_boolean_are_parsed_strictly(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "agent", token="test-agent-token", tools=[], env={})
    config.set_value(config_path, "agent", "browser.headless", "true")
    settings = config.load_agent_settings(config_path, env={"RELAY_AGENT_BROWSER_HEADLESS": "false"})
    assert settings.browser_headless is False


def test_init_agent_prompts_for_token_and_starts_with_empty_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / ".agent-relay" / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0

    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "agent"]) == 0

    from yaml import safe_load

    document = safe_load(config_path.read_text(encoding="utf-8"))
    assert document["agent"]["tools"]["allowlist"] == []
    assert document["agent"]["identity"]["id"]
    token_path = config_path.parent / "secrets/agent/agent_token"
    assert token_path.read_text(encoding="utf-8") == "agent-secret\n"
    assert os.stat(token_path).st_mode & 0o777 == 0o600


def test_init_agent_supports_explicit_no_tools_and_stdin_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("sys.stdin", io.StringIO("agent-from-stdin\n"))

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
    assert "agent-from-stdin" in (
        config_path.parent / "secrets/agent/agent_token"
    ).read_text(encoding="utf-8")


def test_get_outputs_yaml_without_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("RELAY_MCP_TOKEN", "must-not-appear")

    assert cli.main(["--config", str(config_path), "config", "get", "server"]) == 0
    output = capsys.readouterr().out
    assert "mcp_token_file:" in output
    assert "must-not-appear" not in output
    assert "mcp_token:" not in output


def test_set_get_and_unset_scalar_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    capsys.readouterr()

    assert (
        cli.main(
            ["--config", str(config_path), "config", "set", "server", "port", "9000"]
        )
        == 0
    )
    assert cli.main(["--config", str(config_path), "config", "get", "server"]) == 0
    assert "port: 9000" in capsys.readouterr().out

    assert (
        cli.main(
            ["--config", str(config_path), "config", "unset", "server", "port"]
        )
        == 0
    )
    assert cli.main(["--config", str(config_path), "config", "get", "server"]) == 0
    assert "port: 8000" in capsys.readouterr().out


def test_config_set_secret_reads_stdin_and_never_prints_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO("replacement-secret\n"))

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "config",
                "set",
                "server",
                "mcp_token",
                "--stdin",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "replacement-secret" not in output
    secret_path = config_path.parent / "secrets/server/mcp_token"
    assert secret_path.read_text(encoding="utf-8") == "replacement-secret\n"


def test_unset_secret_clears_content_but_preserves_private_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    capsys.readouterr()
    secret_path = config_path.parent / "secrets/server/mcp_token"
    assert secret_path.read_text(encoding="utf-8").strip()
    assert cli.main(["--config", str(config_path), "config", "unset", "server", "mcp_token"]) == 0
    assert secret_path.is_file()
    assert secret_path.read_text(encoding="utf-8").strip() == ""
    assert cli.main(["--config", str(config_path), "config", "validate", "server"]) == 1


def test_validate_agent_accepts_an_empty_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("sys.stdin", io.StringIO("agent-secret\n"))
    assert cli.main(["--config", str(config_path), "config", "init", "agent", "--no-tools", "--stdin"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(config_path), "config", "validate", "agent"]) == 0
    assert "error" not in capsys.readouterr().out.lower()


def test_tools_enable_disable_use_public_names_and_exclude_server_local_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "agent"]) == 0
    capsys.readouterr()

    assert cli.main(["--config", str(config_path), "tools", "enable", "relay_terminal_exec"]) == 0
    assert cli.main(["--config", str(config_path), "tools", "list"]) == 0
    output = capsys.readouterr().out
    assert "relay_terminal_exec" in output
    assert "enabled" in output
    assert "relay_device_status" in output
    assert cli.main(["--config", str(config_path), "tools", "disable", "relay_terminal_exec"]) == 0

    assert cli.main(["--config", str(config_path), "tools", "enable", "relay_device_status"]) == 1


def test_doctor_prints_combined_human_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "agent"]) == 0
    capsys.readouterr()

    assert cli.main(["--config", str(config_path), "doctor"]) == 0
    output = capsys.readouterr().out
    assert "Agent Relay doctor" in output
    assert "Server" in output
    assert "Agent" in output
    assert "[INFO] no tools enabled" in output
    assert "Summary" in output


def test_canonical_environment_overrides_yaml_for_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("RELAY_SERVER_PORT", "9100")

    assert cli.main(["--config", str(config_path), "config", "validate", "server"]) == 0
    output = capsys.readouterr().out
    assert "9100" in output


def test_set_allowlist_accepts_a_comma_separated_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "agent"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "config",
                "set",
                "agent",
                "tools.allowlist",
                "relay_system_ping,relay_terminal_exec",
            ]
        )
        == 0
    )
    assert cli.main(["--config", str(config_path), "config", "validate", "agent"]) == 0


def test_validate_rejects_an_unavailable_optional_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "agent"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "config",
                "set",
                "agent",
                "tools.allowlist",
                "[relay_browser_list_tabs]",
            ]
        )
        == 0
    )
    assert cli.main(["--config", str(config_path), "config", "validate", "agent"]) == 1


def test_set_and_validate_reject_unknown_configuration_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(config_path), "config", "set", "server", "typo", "1"]) == 1


def test_legacy_commands_are_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    with pytest.raises(SystemExit) as error:
        cli.main(["--config", str(config_path), "client"])
    assert error.value.code == 2


def test_relay_url_is_not_locked_to_a_specific_websocket_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "agent"]) == 0
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "config",
                "set",
                "agent",
                "relay_url",
                "ws://relay.example.test/future-endpoint",
            ]
        )
        == 0
    )
    assert cli.main(["--config", str(config_path), "config", "validate", "agent"]) == 0

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "config",
                "set",
                "agent",
                "relay_url",
                "ws://relay.example.test:not-a-port/future",
            ]
        )
        == 0
    )
    assert cli.main(["--config", str(config_path), "config", "validate", "agent"]) == 1


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


def test_repeated_agent_init_preserves_identity_tools_and_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "config",
                "init",
                "agent",
                "--tools",
                "relay_system_ping",
            ]
        )
        == 0
    )
    first = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    monkeypatch.setattr("getpass.getpass", lambda *_: pytest.fail("must not prompt"))
    assert cli.main(["--config", str(config_path), "config", "init", "agent"]) == 0
    second = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert second["agent"]["identity"]["id"] == first["agent"]["identity"]["id"]
    assert second["agent"]["tools"]["allowlist"] == ["relay_system_ping"]
    assert (
        (config_path.parent / "secrets" / "agent" / "agent_token")
        .read_text(encoding="utf-8")
        .strip()
        == "agent-secret"
    )


def test_force_reinitializes_mutable_settings_but_preserves_agent_identity_and_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    assert cli.main(["--config", str(config_path), "config", "init", "agent", "--tools", "relay_system_ping"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(config_path), "config", "set", "server", "port", "9000"]) == 0
    assert cli.main(["--config", str(config_path), "config", "init", "server", "--force"]) == 0
    assert cli.main(["--config", str(config_path), "config", "get", "server"]) == 0
    assert "port: 8000" in capsys.readouterr().out
    before = yaml.safe_load(config_path.read_text(encoding="utf-8"))["agent"]["identity"]["id"]
    assert cli.main(["--config", str(config_path), "config", "init", "agent", "--force"]) == 0
    after = yaml.safe_load(config_path.read_text(encoding="utf-8"))["agent"]
    assert after["identity"]["id"] == before
    assert after["tools"]["allowlist"] == ["relay_system_ping"]
