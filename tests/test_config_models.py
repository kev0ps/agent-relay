from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from agent_relay.catalog import CatalogError, validate_agent_allowlist
from agent_relay.config_models import AgentConfig, ServerConfig, configuration_keys


def test_server_config_model_owns_defaults_types_and_nested_keys() -> None:
    assert ServerConfig().model_dump() == {
        "host": "127.0.0.1",
        "port": 8000,
        "mcp": {"allowed_hosts": [], "allowed_origins": []},
        "runtime": {
            "min_timeout_seconds": 0.1,
            "max_timeout_seconds": 30.0,
            "cancel_send_timeout_seconds": 0.25,
            "max_ws_message_bytes": 128 * 1024,
        },
    }
    model = ServerConfig.from_sources(
        {},
        {
            "RELAY_SERVER_PORT": "9100",
            "RELAY_MAX_TIMEOUT_SECONDS": "45.5",
            "RELAY_MCP_ALLOWED_HOSTS": "relay.example.test, localhost",
        },
    )

    assert model.port == 9100
    assert model.host == "127.0.0.1"
    assert model.mcp.allowed_hosts == ["relay.example.test", "localhost"]
    assert model.runtime.min_timeout_seconds == 0.1
    assert model.runtime.max_timeout_seconds == 45.5
    assert configuration_keys(ServerConfig) == {
        "host",
        "port",
        "mcp.allowed_hosts",
        "mcp.allowed_origins",
        "runtime.min_timeout_seconds",
        "runtime.max_timeout_seconds",
        "runtime.cancel_send_timeout_seconds",
        "runtime.max_ws_message_bytes",
    }


def test_agent_config_model_owns_defaults_types_and_cross_field_validation() -> None:
    defaults = AgentConfig().model_dump(exclude={"identity"})
    assert defaults == {
        "relay_url": "ws://127.0.0.1:8000/ws/agent",
        "workspace": "./workspace",
        "tools": {"allowlist": []},
        "computer": {
            "allowed_app_name": None,
            "allowed_window_title": None,
            "startup_timeout_seconds": 15.0,
            "action_timeout_seconds": 10.0,
            "shutdown_timeout_seconds": 3.0,
            "max_elements": 300,
        },
        "runtime": {
            "heartbeat_interval_seconds": 15.0,
            "reconnect_min_seconds": 0.1,
            "reconnect_max_seconds": 5.0,
            "stable_session_seconds": 30.0,
            "max_ws_message_bytes": 128 * 1024,
            "command_timeout_seconds": 30.0,
            "stdout_limit": 24 * 1024,
            "stderr_limit": 24 * 1024,
        },
    }
    identity = str(uuid.uuid4())
    model = AgentConfig.from_sources(
        {"identity": {"id": identity}},
        {
            "RELAY_AGENT_TOOLS": "relay_system_ping, relay_terminal_exec",
            "RELAY_AGENT_RECONNECT_MIN_SECONDS": "1.5",
            "RELAY_AGENT_COMPUTER_MAX_ELEMENTS": "400",
        },
    )

    assert model.identity.id == identity
    assert model.relay_url == "ws://127.0.0.1:8000/ws/agent"
    assert model.tools.allowlist == ["relay_system_ping", "relay_terminal_exec"]
    assert model.runtime.reconnect_min_seconds == 1.5
    assert model.runtime.reconnect_max_seconds == 5.0
    assert model.computer.max_elements == 400

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(
            {
                "identity": {"id": identity},
                "runtime": {
                    "reconnect_min_seconds": 10,
                    "reconnect_max_seconds": 5,
                },
            }
        )


def test_config_models_forbid_unknown_keys_and_reject_boolean_numbers() -> None:
    with pytest.raises(ValidationError):
        ServerConfig.model_validate({"runtime": {"typo": 1}})
    with pytest.raises(ValidationError):
        ServerConfig.model_validate({"port": True})
    with pytest.raises(ValidationError):
        ServerConfig.from_sources(
            {"runtime": "not-a-mapping"},
            {"RELAY_MAX_TIMEOUT_SECONDS": "45"},
        )


def test_config_models_flatten_to_existing_runtime_contract(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = str(uuid.uuid4())
    agent = AgentConfig.model_validate(
        {
            "identity": {"id": identity},
            "workspace": str(workspace),
            "tools": {"allowlist": ["relay_system_ping"]},
        }
    )

    agent_runtime = agent.runtime_settings(token="secret", config_path=tmp_path / "config.yaml")
    assert agent_runtime["server_url"] == agent.relay_url
    assert agent_runtime["device_id"] == identity
    assert agent_runtime["agent_id"] == identity
    assert agent_runtime["workspace"] == workspace
    assert agent_runtime["tools_allowlist"] == ("relay_system_ping",)
    assert agent_runtime["heartbeat_interval_seconds"] == 15.0
    assert agent_runtime["computer_max_elements"] == 300

    server_runtime = ServerConfig().runtime_settings(
        mcp_token="mcp", agent_token="agent"
    )
    assert server_runtime["bind_host"] == "127.0.0.1"
    assert server_runtime["mcp_allowed_hosts"] == ()
    assert server_runtime["max_timeout_seconds"] == 30.0


def test_catalog_centralizes_static_and_deferred_allowlist_validation() -> None:
    assert validate_agent_allowlist(
        ["relay_system_ping", "relay_cua_provider_added_later"]
    ) == ("relay_system_ping", "relay_cua_provider_added_later")
    assert validate_agent_allowlist(["relay_custom_future"], defer_unknown=True) == (
        "relay_custom_future",
    )

    with pytest.raises(CatalogError, match="server-local"):
        validate_agent_allowlist(["relay_device_status"])
    with pytest.raises(CatalogError, match="unknown Agent tool"):
        validate_agent_allowlist(["relay_unknown"])
    with pytest.raises(CatalogError, match="duplicates"):
        validate_agent_allowlist(["relay_system_ping", "relay_system_ping"])
