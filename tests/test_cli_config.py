from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
import yaml

from agent_relay import cli, config


def test_init_server_creates_private_yaml_and_dotenv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / ".agent-relay" / "config.yaml"

    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0

    assert config_path.is_file()
    if os.name != "nt":
        assert os.stat(config_path).st_mode & 0o777 == 0o600
    dotenv = config_path.parent / ".env"
    assert dotenv.is_file()
    assert "RELAY_MCP_TOKEN=" in dotenv.read_text(encoding="utf-8")
    assert "RELAY_AGENT_TOKEN=" in dotenv.read_text(encoding="utf-8")
    assert "secrets" not in config_path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert os.stat(dotenv).st_mode & 0o777 == 0o600
    assert "token" not in capsys.readouterr().out.lower()


def test_init_rejects_symlinked_dotenv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / "config-parent"
    outside = tmp_path / "outside"
    config_dir.mkdir()
    outside.mkdir()
    try:
        os.symlink(outside / ".env", config_dir / ".env")
    except OSError:
        pytest.skip("symbolic links are unavailable")

    config_path = config_dir / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 1
    assert not (outside / ".env").exists()
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


def test_init_server_rejects_shared_process_tokens(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="must be distinct"):
        config.init_config(
            tmp_path / "config.yaml",
            "server",
            env={
                "RELAY_MCP_TOKEN": "shared-token",
                "RELAY_AGENT_TOKEN": "shared-token",
            },
        )


def test_dotenv_values_are_used_without_mutating_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "server", env={})
    dotenv = config_path.parent / ".env"
    values = dotenv.read_text(encoding="utf-8").splitlines()
    dotenv.write_text(
        "\n".join(
            "RELAY_MCP_TOKEN=dotenv-mcp" if line.startswith("RELAY_MCP_TOKEN=") else line
            for line in values
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        dotenv.chmod(0o600)
    monkeypatch.delenv("RELAY_MCP_TOKEN", raising=False)
    runtime = config.load_server_runtime(config_path, env={})
    assert runtime.settings.mcp_token == "dotenv-mcp"
    assert "RELAY_MCP_TOKEN" not in os.environ


@pytest.mark.parametrize(
    "contents, expected",
    [
        ("RELAY_UNKNOWN=value\n", "key is not allowed"),
        ("RELAY_MCP_TOKEN=one\nRELAY_MCP_TOKEN=two\n", "duplicated"),
        ("RELAY_MCP_TOKEN\n", "line 1 is invalid"),
        ("x" * 4097, "too large"),
    ],
)
def test_dotenv_rejects_unsupported_syntax(
    tmp_path: Path, contents: str, expected: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "server", env={})
    dotenv = config_path.parent / ".env"
    dotenv.write_text(contents, encoding="utf-8")
    if os.name != "nt":
        dotenv.chmod(0o600)
    with pytest.raises(config.ConfigError, match=expected):
        config.load_server_runtime(config_path, env={})


def test_legacy_secrets_and_token_file_environment_are_rejected(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "server:\n  secrets:\n    mcp_token_file: ./secrets/mcp\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        config_path.chmod(0o600)
    report = config.validate_document(config_path, "server", env={})
    assert not report.valid
    assert any("legacy secrets configuration" in issue.message for issue in report.errors)

    with pytest.raises(config.ConfigError, match=r"create \.env next to the YAML"):
        config.load_server_runtime(config_path, env={})

    config.init_config(tmp_path / "agent.yaml", "agent", token="agent", tools=[], env={})
    with pytest.raises(config.ConfigError, match="no longer supported"):
        config.load_agent_settings(
            tmp_path / "agent.yaml",
            env={"RELAY_AGENT_TOKEN_FILE": str(tmp_path / "old-token")},
        )


def test_server_runtime_reports_sanitized_validation_errors(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.yaml"
    secret = "SHARED_TOKEN_SENTINEL"

    with pytest.raises(config.ConfigError) as error:
        config.load_server_runtime(
            config_path,
            env={"RELAY_MCP_TOKEN": secret, "RELAY_AGENT_TOKEN": secret},
        )

    message = str(error.value)
    assert message == (
        "invalid server configuration: mcp and agent tokens must be distinct"
    )
    assert secret not in message


def test_yaml_boolean_and_environment_boolean_are_parsed_strictly(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "agent", token="test-agent-token", tools=[], env={})
    config.set_value(config_path, "agent", "browser.headless", "true")
    settings = config.load_agent_settings(config_path, env={"RELAY_AGENT_BROWSER_HEADLESS": "false"})
    assert settings.browser_headless is False


def test_init_agent_explicitly_starts_with_empty_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / ".agent-relay" / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0

    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "config",
                "init",
                "agent",
                "--no-tools",
            ]
        )
        == 0
    )

    from yaml import safe_load

    document = safe_load(config_path.read_text(encoding="utf-8"))
    assert document["agent"]["tools"]["allowlist"] == []
    assert document["agent"]["identity"]["id"]
    dotenv = config_path.parent / ".env"
    assert "RELAY_AGENT_TOKEN=agent-secret\n" in dotenv.read_text(encoding="utf-8")
    if os.name != "nt":
        assert os.stat(dotenv).st_mode & 0o777 == 0o600


def test_reinit_in_tty_preserves_existing_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "agent", "--no-tools"]) == 0
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "config",
                "set",
                "agent",
                "tools.allowlist",
                "[relay_system_ping]",
            ]
        )
        == 0
    )

    class TTYInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", TTYInput(""))
    assert cli.main(["--config", str(config_path), "config", "init", "agent"]) == 0
    assert config.get_section(config_path, "agent")["tools"]["allowlist"] == [
        "relay_system_ping"
    ]


@pytest.mark.parametrize("bad_shape", ["duplicate", "scalar"])
def test_reinit_rejects_malformed_existing_allowlist(
    tmp_path: Path, bad_shape: str
) -> None:
    config_path = tmp_path / f"{bad_shape}.yaml"
    config.init_config(config_path, "agent", token="agent-secret", tools=[], env={})
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if bad_shape == "duplicate":
        document["agent"]["tools"]["allowlist"] = [
            "relay_system_ping",
            "relay_system_ping",
        ]
    else:
        document["agent"]["tools"] = "not-a-mapping"
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle, sort_keys=False)

    with pytest.raises(config.ConfigError, match="mapping|duplicates"):
        config.init_config(config_path, "agent", tools=None, env={})


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
    assert "RELAY_AGENT_TOKEN=agent-from-stdin\n" in (
        config_path.parent / ".env"
    ).read_text(encoding="utf-8")


def test_init_agent_from_server_uses_the_effective_dotenv_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "server", env={})
    dotenv = config_path.parent / ".env"
    values = dotenv.read_text(encoding="utf-8").splitlines()
    dotenv.write_text(
        "\n".join(
            "RELAY_AGENT_TOKEN=custom-server-token" if line.startswith("RELAY_AGENT_TOKEN=") else line
            for line in values
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        dotenv.chmod(0o600)
    monkeypatch.delenv("RELAY_AGENT_TOKEN", raising=False)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "config",
                "init",
                "agent",
                "--from-server",
                "--no-tools",
            ]
        )
        == 0
    )

    agent_token = dotenv.read_text(encoding="utf-8")
    assert "RELAY_AGENT_TOKEN=custom-server-token\n" in agent_token
    output = capsys.readouterr()
    assert "custom-server-token" not in output.out
    assert "custom-server-token" not in output.err


def test_server_agent_token_source_honors_environment_override(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "server", env={})

    assert config.read_server_agent_token(
        config_path,
        env={"RELAY_AGENT_TOKEN": "environment-server-token"},
    ) == "environment-server-token"


def test_get_outputs_yaml_without_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("RELAY_MCP_TOKEN", "must-not-appear")

    assert cli.main(["--config", str(config_path), "config", "get", "server"]) == 0
    output = capsys.readouterr().out
    assert "secrets" not in output
    assert "mcp_token_file" not in output
    assert "must-not-appear" not in output
    assert "mcp_token:" not in output


def test_get_distinguishes_a_missing_section_from_an_invalid_section(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "server", env={})

    assert cli.main(["--config", str(config_path), "config", "get", "agent"]) == 1
    assert "agent configuration is not initialized" in capsys.readouterr().err

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["agent"] = "invalid"
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    assert cli.main(["--config", str(config_path), "config", "get", "agent"]) == 1
    error = capsys.readouterr().err
    assert "agent configuration must be a mapping" in error
    assert "agent configuration is not initialized" not in error


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
    dotenv = config_path.parent / ".env"
    assert "RELAY_MCP_TOKEN=replacement-secret\n" in dotenv.read_text(encoding="utf-8")


def test_unset_secret_removes_dotenv_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    capsys.readouterr()
    dotenv = config_path.parent / ".env"
    assert "RELAY_MCP_TOKEN=" in dotenv.read_text(encoding="utf-8")
    assert cli.main(["--config", str(config_path), "config", "unset", "server", "mcp_token"]) == 0
    assert "RELAY_MCP_TOKEN=" not in dotenv.read_text(encoding="utf-8")
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


def test_doctor_prints_combined_human_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "agent", "--no-tools"]) == 0
    capsys.readouterr()

    assert cli.main(["--config", str(config_path), "doctor"]) == 0
    output = capsys.readouterr().out
    assert "Agent Relay doctor" in output
    assert "Server" in output
    assert "Agent" in output
    assert "[INFO] no tools enabled" in output
    assert "Summary" in output


def test_application_configuration_failures_return_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--config", str(tmp_path / "missing.yaml"), "doctor"]) == 1
    captured = capsys.readouterr()
    assert "error" in (captured.out + captured.err).lower()


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


def test_noninteractive_agent_init_requires_explicit_tool_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert (
        cli.main(
            [
                "--config",
                str(tmp_path / "config.yaml"),
                "config",
                "init",
                "agent",
            ]
        )
        == 1
    )
    assert "--tools or --no-tools" in capsys.readouterr().err


def test_set_and_validate_reject_unknown_configuration_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    assert cli.main(["--config", str(config_path), "config", "init", "server"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(config_path), "config", "set", "server", "typo", "1"]) == 1


def test_relay_url_is_not_locked_to_a_specific_websocket_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "agent", "--no-tools"]) == 0
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
        "RELAY_AGENT_TOKEN=agent-secret\n"
        in (config_path.parent / ".env").read_text(encoding="utf-8")
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
