from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest

from agent_relay import cli, config
from agent_relay.catalog import CatalogPolicy, CatalogService, ProviderRegistration
from agent_relay.cua_profiles import (
    CUA_PROFILE_PUBLIC_NAMES,
    FULL_CUA_TOOL_NAMES,
    STANDARD_CUA_TOOL_NAMES,
    cua_access_for_allowlist,
)
from agent_relay.provider_tools import ProviderToolDescriptor


class _Provider:
    def __init__(self, names: tuple[str, ...], provider_name: str = "cua") -> None:
        self._tools = [
            ProviderToolDescriptor(
                provider_name=provider_name,
                tool_name=name,
                public_name=name,
                description=name,
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                risk="interaction",
            )
            for name in names
        ]

    async def list_tools(self) -> list[ProviderToolDescriptor]:
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
        del tool_name, arguments
        raise AssertionError("profile tests do not dispatch tools")

    async def close(self) -> None:
        return None


def _catalog(*, include_full: bool = True, blocked: tuple[str, ...] = ()):
    names = (
        FULL_CUA_TOOL_NAMES
        if include_full
        else tuple(name for name in FULL_CUA_TOOL_NAMES if name != "click")
    )
    return asyncio.run(
        CatalogService(
            [
                ProviderRegistration(
                    "system",
                    _Provider(("ping",), "system"),
                    allow_reserved_public_names=True,
                ),
                ProviderRegistration(
                    "cua",
                    _Provider((*names, "new_tool", "page")),
                    allow_reserved_public_names=True,
                ),
            ],
            policy=CatalogPolicy(cua_blocked_names=frozenset(blocked)),
        ).discover()
    )


def _allowlist(path: Path) -> list[str]:
    return config.get_section(path, "agent")["tools"]["allowlist"]


def test_profiles_are_exact_ordered_and_versioned() -> None:
    assert tuple(name.removeprefix("relay_cua_") for name in CUA_PROFILE_PUBLIC_NAMES["standard"]) == STANDARD_CUA_TOOL_NAMES
    assert tuple(name.removeprefix("relay_cua_") for name in CUA_PROFILE_PUBLIC_NAMES["full"]) == FULL_CUA_TOOL_NAMES
    assert len(FULL_CUA_TOOL_NAMES) > len(STANDARD_CUA_TOOL_NAMES)
    assert cua_access_for_allowlist(list(CUA_PROFILE_PUBLIC_NAMES["standard"])) == "standard"


def test_full_profile_never_selects_policy_blocked_catalog_entries() -> None:
    catalog = _catalog()
    blocked = catalog.entry("relay_cua_page")
    assert blocked.status == "blocked"
    assert "relay_cua_page" not in CUA_PROFILE_PUBLIC_NAMES["full"]
    config.validate_cua_profile("full", catalog)


@pytest.mark.parametrize("level", ["standard", "full"])
def test_profiles_exclude_policy_blocked_tools(
    tmp_path: Path, level: str
) -> None:
    catalog = _catalog(blocked=("click",))
    path = tmp_path / f"{level}.yaml"
    config.init_config(
        path,
        "agent",
        token="agent-secret",
        tools=[],
        env={},
        catalog=catalog,
    )

    config.update_cua_access(path, level, catalog=catalog)
    assert "relay_cua_click" not in _allowlist(path)
    expected = tuple(
        name
        for name in CUA_PROFILE_PUBLIC_NAMES[level]  # type: ignore[index]
        if name != "relay_cua_click"
    )
    assert tuple(_allowlist(path)) == expected
    assert config.cua_tool_summary(path, catalog=catalog).access == level


def test_profile_transitions_preserve_non_cua_and_keep_dynamic_tools_off(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    path = tmp_path / "config.yaml"
    config.init_config(
        path,
        "agent",
        token="agent-secret",
        tools=["relay_system_ping"],
        env={},
        catalog=catalog,
    )

    config.update_cua_access(path, "standard", catalog=catalog)
    assert _allowlist(path) == [
        "relay_system_ping",
        *CUA_PROFILE_PUBLIC_NAMES["standard"],
    ]
    assert "relay_cua_new_tool" not in _allowlist(path)

    config.update_cua_access(path, "full", catalog=catalog)
    assert _allowlist(path) == [
        "relay_system_ping",
        *CUA_PROFILE_PUBLIC_NAMES["full"],
    ]

    with pytest.raises(config.ConfigError, match="blocked"):
        config.update_tool(path, "relay_cua_new_tool", enabled=True, catalog=catalog)
    assert _allowlist(path) == [
        "relay_system_ping",
        *CUA_PROFILE_PUBLIC_NAMES["full"],
    ]
    assert config.cua_tool_summary(path, catalog=catalog).access == "full"
    config.update_cua_access(path, "none", catalog=catalog)
    assert _allowlist(path) == ["relay_system_ping"]


def test_profile_mismatch_is_rejected_without_writing(tmp_path: Path) -> None:
    good_catalog = _catalog()
    path = tmp_path / "config.yaml"
    config.init_config(
        path,
        "agent",
        token="agent-secret",
        tools=[],
        env={},
        catalog=good_catalog,
    )
    before = path.read_bytes()
    with pytest.raises(config.ConfigError, match="not available"):
        config.update_cua_access(path, "standard", catalog=_catalog(include_full=False))
    assert path.read_bytes() == before


def test_full_access_confirmation_is_cli_owned_and_cancellation_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = _catalog()
    path = tmp_path / "config.yaml"
    config.init_config(path, "agent", token="agent-secret", tools=[], env={}, catalog=catalog)
    before = path.read_bytes()

    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert (
        cli.main(
            ["--config", str(path), "tools", "cua-access", "full"],
            catalog=catalog,
        )
        == 1
    )
    assert path.read_bytes() == before
    assert "requires --yes" in capsys.readouterr().err

    class _TTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _TTY("n\n"))
    assert (
        cli.main(
            ["--config", str(path), "tools", "cua-access", "full"],
            catalog=catalog,
        )
        == 0
    )
    assert path.read_bytes() == before
    assert "CUA access update cancelled" in capsys.readouterr().out


def test_local_onboarding_cua_cancellation_does_not_create_a_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = _catalog()
    path = tmp_path / "config.yaml"
    config.init_config(path, "agent", token="agent-secret", tools=[], env={}, catalog=catalog)
    dotenv = path.parent / ".env"
    config_before = path.read_bytes()
    dotenv_before = dotenv.read_bytes()

    class _TTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _TTY("n\n"))
    assert (
        cli.main(
            [
                "--config",
                str(path),
                "onboard",
                "--role",
                "local",
                "--topology",
                "local",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--cua-access",
                "full",
                "--no-check",
            ],
            catalog=catalog,
        )
        == 0
    )

    assert path.read_bytes() == config_before
    assert dotenv.read_bytes() == dotenv_before
    assert "CUA access update cancelled" in capsys.readouterr().out


def test_cli_profiles_compact_inventory_and_option_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = _catalog()
    path = tmp_path / "config.yaml"
    monkeypatch.setattr("getpass.getpass", lambda *_: "agent-secret")
    assert (
        cli.main(
            [
                "--config",
                str(path),
                "config",
                "init",
                "agent",
                "--tools",
                "relay_system_ping",
                "--cua-access",
                "standard",
            ],
            catalog=catalog,
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["--config", str(path), "tools", "list"], catalog=catalog) == 0
    compact = capsys.readouterr().out
    assert "CUA\tcua\tstandard" in compact
    assert "relay_cua_click" not in compact
    assert cli.main(["--config", str(path), "tools", "list", "--all"], catalog=catalog) == 0
    detailed = capsys.readouterr().out
    assert "relay_cua_click" in detailed

    conflict_path = tmp_path / "conflict.yaml"
    assert (
        cli.main(
            [
                "--config",
                str(conflict_path),
                "config",
                "init",
                "agent",
                "--tools",
                "relay_cua_click",
                "--cua-access",
                "standard",
            ],
            catalog=catalog,
        )
        == 1
    )
    assert not conflict_path.exists()


def test_onboarding_cua_access_and_noninteractive_full_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = _catalog()
    path = tmp_path / "onboard.yaml"
    assert (
        cli.main(
            [
                "--config",
                str(path),
                "onboard",
                "--role",
                "local",
                "--non-interactive",
                "--topology",
                "local",
                "--tools",
                "relay_system_ping",
                "--cua-access",
                "standard",
            ],
            catalog=catalog,
        )
        == 0
    )
    assert _allowlist(path) == [
        "relay_system_ping",
        *CUA_PROFILE_PUBLIC_NAMES["standard"],
    ]

    full_path = tmp_path / "full-onboard.yaml"
    result = cli.main(
        [
            "--config",
            str(full_path),
            "onboard",
            "--role",
            "local",
            "--non-interactive",
            "--topology",
            "local",
                "--cua-access",
                "full",
            ],
        catalog=catalog,
    )
    assert result == 1
    assert not full_path.exists()
    assert "requires --yes" in capsys.readouterr().err
