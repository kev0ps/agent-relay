from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from agent_relay import cli, config
from agent_relay.catalog import CatalogService, CatalogSnapshot, ProviderRegistration
from agent_relay.provider_tools import ProviderToolDescriptor


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
