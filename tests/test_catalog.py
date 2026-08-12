from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from agent_relay.catalog import (
    CatalogError,
    CatalogPolicy,
    CatalogService,
    ProviderRegistration,
    _configured_cua_catalog_client,
    discover_local_catalog,
    public_tool_name,
)
from agent_relay.provider_tools import ProviderToolDescriptor
from agent_relay.providers.base import ProviderToolClient


def _descriptor(
    provider: str,
    tool: str,
    *,
    risk: str = "interaction",
) -> ProviderToolDescriptor:
    return ProviderToolDescriptor(
        provider_name=provider,
        tool_name=tool,
        public_name=tool,
        description=f"{provider} {tool}",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        risk=risk,  # type: ignore[arg-type]
    )


class _Provider(ProviderToolClient):
    def __init__(
        self,
        descriptors: Sequence[ProviderToolDescriptor | Mapping[str, object]],
    ) -> None:
        self.descriptors = descriptors

    async def list_tools(self) -> Sequence[ProviderToolDescriptor | Mapping[str, object]]:
        return self.descriptors

    async def call_tool(self, tool_name: str, arguments: Mapping[str, object]) -> object:
        raise AssertionError("catalog tests must not invoke provider tools")

    async def close(self) -> None:
        return None


def test_local_catalog_loader_discovers_builtins_and_the_standard_cua_provider() -> None:
    snapshot = discover_local_catalog(env={})
    by_name = {entry.public_name: entry for entry in snapshot.entries}

    assert by_name["relay_system_ping"].status == "disabled"
    assert by_name["relay_terminal_exec"].status == "disabled"
    assert by_name["relay_terminal_exec"].descriptor.input_schema == {
        "type": "object",
        "properties": {
            "command_id": {
                "type": "string",
                "enum": [
                    "pwd",
                    "whoami",
                    "python_version",
                    "git_status",
                    "git_branch",
                ],
            }
        },
        "required": ["command_id"],
        "additionalProperties": False,
    }
    assert "cua" not in snapshot.unavailable_providers
    assert by_name["relay_cua_click"].status == "disabled"
    assert all(entry.risk for entry in snapshot.entries)


def test_local_catalog_loader_does_not_require_an_application_for_discovery() -> None:
    snapshot = discover_local_catalog(env={})

    assert "cua" not in snapshot.unavailable_providers
    assert any(entry.public_name == "relay_cua_browser_navigate" for entry in snapshot.entries)


def test_cua_catalog_construction_debug_is_bounded(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "agent_relay.capabilities.computer.get_cua_driver_path",
        lambda: (_ for _ in ()).throw(ValueError("unavailable")),
    )

    result = _configured_cua_catalog_client(
        {
            "RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME": "fixture",
            "RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE": "Fixture",
            "RELAY_NATIVE_DEBUG": "1",
        }
    )

    assert result is None
    assert capsys.readouterr().err == (
        "[DEBUG] cua catalog construction failed: "
        "stage=capability-init category=value-error\n"
    )


def test_local_catalog_loader_uses_injected_runtime_provider_tools_list() -> None:
    provider = _Provider([_descriptor("cua", "get_browser_state", risk="read_only")])

    snapshot = discover_local_catalog(providers={"cua": provider})

    assert snapshot.entry("relay_cua_get_browser_state").status == "disabled"
    assert snapshot.unavailable_providers == ()


def test_public_names_are_stable_and_collision_checked() -> None:
    assert public_tool_name("cua-driver", "capture") == "relay_cua_driver_capture"
    assert public_tool_name("cua", "browser_list_tabs") == "relay_cua_browser_list_tabs"

    service = CatalogService(
        [
            ProviderRegistration(
                "cua-driver",
                _Provider([_descriptor("cua-driver", "capture")]),
            ),
            ProviderRegistration(
                "cua_driver",
                _Provider([_descriptor("cua_driver", "capture")]),
            ),
        ]
    )
    with pytest.raises(CatalogError, match="public tool name collision"):
        asyncio.run(service.discover())


def test_reserved_public_names_require_trusted_local_registration() -> None:
    with pytest.raises(CatalogError, match="reserved public tool name collision"):
        asyncio.run(
            CatalogService(
                [
                    ProviderRegistration(
                        "system", _Provider([_descriptor("system", "ping")])
                    )
                ]
            ).discover()
        )


def test_discovery_returns_all_candidates_disabled_and_only_explicit_selection_enabled() -> None:
    service = CatalogService(
        [
            ProviderRegistration(
                "cua-driver",
                _Provider(
                    [
                        _descriptor("cua-driver", "capture", risk="read_only"),
                        _descriptor("cua-driver", "click", risk="interaction"),
                    ]
                ),
            )
        ]
    )

    snapshot = asyncio.run(service.discover())
    assert [entry.public_name for entry in snapshot.entries] == [
        "relay_cua_driver_capture",
        "relay_cua_driver_click",
    ]
    assert {entry.status for entry in snapshot.entries} == {"disabled"}
    assert snapshot.selected_descriptors == ()

    selected = snapshot.select(["relay_cua_driver_click"])
    assert selected.selected_public_names == ("relay_cua_driver_click",)
    assert selected.selected_descriptors[0].tool_name == "click"
    assert selected.entry("relay_cua_driver_capture").status == "disabled"


def test_new_provider_tools_remain_disabled_after_reload_and_selection_is_individual() -> None:
    first = CatalogService(
        [
            ProviderRegistration(
                "cua-driver",
                _Provider([_descriptor("cua-driver", "capture")]),
            )
        ]
    )
    first_snapshot = asyncio.run(first.discover()).select(["relay_cua_driver_capture"])
    assert first_snapshot.selected_public_names == ("relay_cua_driver_capture",)

    upgraded = CatalogService(
        [
            ProviderRegistration(
                "cua-driver",
                _Provider(
                    [
                        _descriptor("cua-driver", "capture"),
                        _descriptor("cua-driver", "type"),
                    ]
                ),
            )
        ]
    )
    reloaded = asyncio.run(upgraded.discover(first_snapshot.selected_public_names))
    assert reloaded.selected_public_names == ("relay_cua_driver_capture",)
    assert reloaded.entry("relay_cua_driver_type").status == "disabled"


def test_missing_provider_is_unavailable_and_never_falls_back() -> None:
    service = CatalogService(
        [
            ProviderRegistration(
                "cua",
                None,
                known_tools=(_descriptor("cua", "browser_navigate"),),
                allow_reserved_public_names=True,
            )
        ]
    )

    snapshot = asyncio.run(service.discover())
    entry = snapshot.entry("relay_cua_browser_navigate")
    assert entry.status == "unavailable"
    assert snapshot.selected_descriptors == ()
    with pytest.raises(CatalogError, match="unavailable"):
        snapshot.validate_allowlist(["relay_cua_browser_navigate"])


def test_policy_blocks_unsafe_tools_and_exposes_risk_class() -> None:
    service = CatalogService(
        [
            ProviderRegistration(
                "cua",
                _Provider(
                    [
                        _descriptor("cua", "get_browser_state", risk="read_only"),
                        _descriptor("cua", "page", risk="interaction"),
                    ]
                ),
                allow_reserved_public_names=True,
            )
        ],
        policy=CatalogPolicy(blocked_tool_names=frozenset({"page"})),
    )

    snapshot = asyncio.run(service.discover())
    assert snapshot.entry("relay_cua_get_browser_state").risk == "read_only"
    assert snapshot.entry("relay_cua_page").status == "blocked"
    assert snapshot.entry("relay_cua_page").risk == "blocked"
    with pytest.raises(CatalogError, match="blocked"):
        snapshot.validate_allowlist(["relay_cua_page"])


def test_invalid_provider_inventory_fails_closed() -> None:
    service = CatalogService(
        [
            ProviderRegistration(
                "cua",
                _Provider(
                    [
                        {
                            "provider_name": "cua",
                            "tool_name": "browser_navigate",
                            "public_name": "navigate",
                            "description": "navigate",
                            "input_schema": {"type": "object"},
                            "risk": "interaction",
                        }
                    ]
                ),
                allow_reserved_public_names=True,
            )
        ]
    )

    with pytest.raises(CatalogError, match="invalid provider inventory"):
        asyncio.run(service.discover())


def test_invalid_json_schema_type_fails_closed() -> None:
    service = CatalogService(
        [
            ProviderRegistration(
                "cua",
                _Provider(
                    [
                        {
                            "provider_name": "cua",
                            "tool_name": "get_browser_state",
                            "public_name": "snapshot",
                            "description": "snapshot",
                            "input_schema": {
                                "type": "not-a-json-schema-type",
                                "additionalProperties": False,
                            },
                            "risk": "read_only",
                        }
                    ]
                ),
                allow_reserved_public_names=True,
            )
        ]
    )

    with pytest.raises(CatalogError, match="invalid provider inventory"):
        asyncio.run(service.discover())


def test_unknown_or_duplicate_allowlist_entries_fail_validation() -> None:
    service = CatalogService(
        [
            ProviderRegistration(
                "terminal",
                _Provider([_descriptor("terminal", "exec")]),
                allow_reserved_public_names=True,
            )
        ]
    )
    snapshot = asyncio.run(service.discover())
    with pytest.raises(CatalogError, match="unknown"):
        snapshot.validate_allowlist(["relay_terminal_missing"])
    with pytest.raises(CatalogError, match="duplicate"):
        snapshot.validate_allowlist(["relay_terminal_exec", "relay_terminal_exec"])
