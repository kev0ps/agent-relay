from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path

import pytest
import yaml

from agent_relay import cli, config
from agent_relay.catalog import CatalogService, CatalogSnapshot, ProviderRegistration
from agent_relay.provider_tools import ProviderToolDescriptor


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
    token_path = config_path.parent / "secrets/agent/agent_token"
    assert token_path.read_text(encoding="utf-8") == "agent-secret\n"
    assert os.stat(token_path).st_mode & 0o777 == 0o600


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
    assert "agent-from-stdin" in (
        config_path.parent / "secrets/agent/agent_token"
    ).read_text(encoding="utf-8")


def test_init_agent_from_server_uses_the_effective_custom_token_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "server", env={})
    default_server_token = config_path.parent / "secrets/server/agent_token"
    stale_token = default_server_token.read_text(encoding="utf-8")

    custom_dir = config_path.parent / "custom"
    custom_dir.mkdir(mode=0o700)
    custom_dir.chmod(0o700)
    custom_server_token = custom_dir / "server-agent-token"
    custom_server_token.write_text("custom-server-token\n", encoding="utf-8")
    custom_server_token.chmod(0o600)
    config.set_value(
        config_path,
        "server",
        "secrets.agent_token_file",
        "./custom/server-agent-token",
    )
    monkeypatch.delenv("RELAY_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_AGENT_TOKEN_FILE", raising=False)

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

    agent_token = (config_path.parent / "secrets/agent/agent_token").read_text(
        encoding="utf-8"
    )
    assert agent_token == "custom-server-token\n"
    assert agent_token != stale_token
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
    assert "mcp_token_file:" in output
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
    assert cli.main(["--config", str(config_path), "config", "init", "agent", "--no-tools"]) == 0
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
    assert cli.main(["--config", str(config_path), "config", "init", "agent", "--no-tools"]) == 0
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


def test_cli_uses_provider_catalog_and_shows_risk_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    descriptor = ProviderToolDescriptor(
        provider_name="browser",
        tool_name="snapshot",
        public_name="provider-name-is-not-trusted",
        description="semantic page snapshot",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        risk="read_only",
    )

    class _Provider:
        async def list_tools(self) -> list[ProviderToolDescriptor]:
            return [descriptor]

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            raise AssertionError("the CLI catalog test must not dispatch a tool")

        async def close(self) -> None:
            return None

    catalog = asyncio.run(
        CatalogService(
            [
                ProviderRegistration(
                    "browser", _Provider(), allow_reserved_public_names=True
                )
            ]
        ).discover()
    )
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
                "relay_browser_snapshot",
            ],
            catalog=catalog,
        )
        == 0
    )
    assert cli.main(["--config", str(config_path), "tools", "list"], catalog=catalog) == 0
    output = capsys.readouterr().out
    assert "relay_browser_snapshot\tbrowser\tenabled\tread_only" in output


def test_agent_config_init_discovers_local_catalog_without_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    observed: list[Path] = []

    def discover(path: Path, *, env: dict[str, str] | None = None) -> CatalogSnapshot:
        observed.append(path)
        return CatalogSnapshot(entries=(), providers=())

    monkeypatch.setattr(config, "discover_local_catalog", discover)
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
    assert observed == [config_path]


def test_config_set_allowlist_uses_discovered_catalog(
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
                "--no-tools",
            ]
        )
        == 0
    )

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "config",
                "set",
                "agent",
                "tools.allowlist",
                "relay_system_ping",
            ]
        )
        == 0
    )
    assert config.get_section(config_path, "agent")["tools"]["allowlist"] == [
        "relay_system_ping"
    ]


def test_set_allowlist_accepts_a_comma_separated_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "agent", "--no-tools"]) == 0
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


def test_set_rejects_an_unavailable_optional_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert cli.main(["--config", str(config_path), "config", "init", "agent", "--no-tools"]) == 0
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
        == 1
    )
    assert "unavailable" in capsys.readouterr().err
    assert cli.main(["--config", str(config_path), "config", "validate", "agent"]) == 0


def test_validation_without_catalog_rejects_static_optional_provider_fallback(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(
        config_path,
        "agent",
        token="agent-secret",
        tools=[],
        env={},
    )
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    profile = tmp_path / "profile"
    profile.mkdir()
    document["agent"]["tools"]["allowlist"] = ["relay_browser_snapshot"]
    document["agent"]["browser"]["user_data_dir"] = str(profile)
    document["agent"]["browser"]["allowed_origins"] = ["https://example.test"]
    yaml.safe_dump(document, config_path.open("w", encoding="utf-8"), sort_keys=False)

    report = config.validate_document(
        config_path,
        "agent",
        env={"RELAY_AGENT_TOKEN": "agent-secret"},
    )

    assert not report.valid
    assert any("unavailable" in issue.message for issue in report.errors)


def test_reinit_without_catalog_rejects_static_optional_allowlist(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config.init_config(config_path, "agent", token="agent-secret", tools=[], env={})
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    profile = tmp_path / "profile"
    profile.mkdir()
    document["agent"]["tools"]["allowlist"] = ["relay_browser_snapshot"]
    document["agent"]["browser"]["user_data_dir"] = str(profile)
    document["agent"]["browser"]["allowed_origins"] = ["https://example.test"]
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle, sort_keys=False)

    with pytest.raises(config.ConfigError, match="unavailable"):
        config.init_config(config_path, "agent", tools=None, env={})


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
