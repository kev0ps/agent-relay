from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from agent_relay.config import AgentConfig, ServerConfig, configuration_keys


def test_server_config_model_owns_defaults_environment_and_cli_keys() -> None:
    model = ServerConfig.from_sources(
        {},
        {
            "RELAY_SERVER_PORT": "9100",
            "RELAY_MAX_TIMEOUT_SECONDS": "45.5",
            "RELAY_MCP_ALLOWED_HOSTS": "relay.example.test, localhost",
        },
    )

    assert model.model_dump() == {
        "host": "127.0.0.1",
        "port": 9100,
        "mcp": {"allowed_hosts": ["relay.example.test", "localhost"], "allowed_origins": []},
        "runtime": {
            "min_timeout_seconds": 0.1,
            "max_timeout_seconds": 45.5,
            "cancel_send_timeout_seconds": 0.25,
            "max_ws_message_bytes": 128 * 1024,
        },
    }
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


def test_agent_model_delegates_reserved_tool_rejection_to_catalog() -> None:
    with pytest.raises(ValidationError, match="server-local"):
        AgentConfig.model_validate({"tools": {"allowlist": ["relay_device_status"]}})


def test_agent_config_owns_environment_coercion_constraints_and_runtime_flattening(
    tmp_path,
) -> None:
    identity = str(uuid.uuid4())
    model = AgentConfig.from_sources(
        {"identity": {"id": identity}, "workspace": "workspace"},
        {
            "RELAY_AGENT_TOOLS": "relay_system_ping, relay_terminal_exec",
            "RELAY_AGENT_RECONNECT_MIN_SECONDS": "1.5",
            "RELAY_AGENT_COMPUTER_MAX_ELEMENTS": "400",
        },
    )

    assert model.tools.allowlist == ["relay_system_ping", "relay_terminal_exec"]
    assert model.runtime.reconnect_min_seconds == 1.5
    assert model.computer.max_elements == 400
    credential = "test-token"
    runtime = model.runtime_settings(token=credential, config_path=tmp_path / "config.yaml")
    assert runtime["device_id"] == identity
    assert runtime["workspace"] == tmp_path / "workspace"
    assert runtime["tools_allowlist"] == ("relay_system_ping", "relay_terminal_exec")
    assert runtime["computer_max_elements"] == 400

    with pytest.raises(ValidationError, match="minimum reconnect"):
        AgentConfig.model_validate(
            {
                "identity": {"id": identity},
                "runtime": {"reconnect_min_seconds": 10, "reconnect_max_seconds": 5},
            }
        )


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (ServerConfig, "min_timeout_seconds"),
        (ServerConfig, "max_timeout_seconds"),
        (ServerConfig, "cancel_send_timeout_seconds"),
        (AgentConfig, "heartbeat_interval_seconds"),
        (AgentConfig, "reconnect_min_seconds"),
        (AgentConfig, "reconnect_max_seconds"),
        (AgentConfig, "command_timeout_seconds"),
    ],
)
def test_runtime_models_keep_the_historical_positive_lower_bound(model, field) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"runtime": {field: 0.00001}})


def test_agent_environment_id_override_keeps_legacy_runtime_ids() -> None:
    runtime_id = "linux-terminal-e2e"

    model = AgentConfig.from_sources({}, {"RELAY_AGENT_ID": runtime_id})

    assert model.identity.id == runtime_id
    with pytest.raises(ValidationError, match="UUID"):
        AgentConfig.model_validate({"identity": {"id": runtime_id}})
