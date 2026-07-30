from __future__ import annotations

import sys
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_relay.protocol import (
    BrowserClickInvoke,
    BrowserFillInvoke,
    BrowserListTabsInvoke,
    BrowserNavigateInvoke,
    BrowserReadPageInvoke,
    ComputerCaptureInvoke,
    ComputerClickInvoke,
    ComputerTypeInvoke,
    SystemPingInvoke,
    TerminalExecInvoke,
)
from agent_relay.server import (
    RelaySettings,
    _is_allowed_bind_host,
    _is_loopback_bind_host,
    create_app,
    main,
)


def settings() -> RelaySettings:
    return RelaySettings(
        device_id="device-a", agent_token="agent-secret", control_token="control-secret"
    )


def test_server_main_uses_canonical_environment_defaults_and_optional_deferred_mcp_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "RELAY_MCP_TOKEN": "mcp-secret",
        "RELAY_AGENT_TOKEN": "agent-secret",
    }
    monkeypatch.setattr("agent_relay.server.os.environ", environment)
    observed: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int, **_: object) -> None:
        observed.update(app=app, host=host, port=port)

    monkeypatch.setattr("uvicorn.run", fake_run)
    try:
        main([])
    except SystemExit as exc:
        pytest.fail(f"canonical server environment was rejected: {exc}")

    assert observed["host"] == "0.0.0.0"
    assert observed["port"] == 8000
    assert "RELAY_AGENT_ID" not in environment
    assert "RELAY_MCP_ALLOWED_HOSTS" not in environment
    assert "RELAY_MCP_ALLOWED_ORIGINS" not in environment

    environment["RELAY_AGENT_TOKEN"] = "mcp-secret"
    with pytest.raises(SystemExit):
        main([])


def test_server_main_accepts_canonical_host_port_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "RELAY_SERVER_HOST": "127.0.0.1",
        "RELAY_SERVER_PORT": "8765",
        "RELAY_MCP_TOKEN": "mcp-secret",
        "RELAY_AGENT_TOKEN": "agent-secret",
    }
    monkeypatch.setattr("agent_relay.server.os.environ", environment)
    observed: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int, **_: object) -> None:
        observed.update(app=app, host=host, port=port)

    monkeypatch.setattr("uvicorn.run", fake_run)
    try:
        main([])
    except SystemExit as exc:
        pytest.fail(f"canonical server environment was rejected: {exc}")

    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 8765


def test_server_main_accepts_explicit_canonical_lan_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "RELAY_SERVER_HOST": "0.0.0.0",
        "RELAY_SERVER_PORT": "8000",
        "RELAY_MCP_TOKEN": "mcp-secret",
        "RELAY_AGENT_TOKEN": "agent-secret",
    }
    monkeypatch.setattr("agent_relay.server.os.environ", environment)
    observed: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int, **_: object) -> None:
        observed.update(app=app, host=host, port=port)

    monkeypatch.setattr("uvicorn.run", fake_run)
    try:
        main([])
    except SystemExit as exc:
        pytest.fail(f"explicit canonical LAN bind was rejected: {exc}")

    assert observed["host"] == "0.0.0.0"
    assert observed["port"] == 8000


def test_relay_configuration_errors_never_echo_tokens(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent_secret = "AGENT_TOKEN_SENTINEL"
    control_secret = "CONTROL_TOKEN_SENTINEL"
    base = {
        "device_id": "device-a",
        "agent_token": agent_secret,
        "control_token": control_secret,
    }
    for invalid in (
        {"device_id": ""},
        {"agent_token": ""},
        {"control_token": ""},
        {"agent_token": agent_secret, "control_token": agent_secret},
        {"min_timeout_seconds": 2, "max_timeout_seconds": 1},
        {"max_ws_message_bytes": 1},
    ):
        with pytest.raises(ValueError) as error:
            RelaySettings(**(base | invalid))
        assert str(error.value) == "invalid relay server configuration"
        assert agent_secret not in str(error.value)
        assert control_secret not in str(error.value)
    with pytest.raises(ValueError) as error:
        RelaySettings.model_validate(base | {"control_token": agent_secret})
    assert str(error.value) == "invalid relay server configuration"
    assert agent_secret not in str(error.value)
    assert control_secret not in str(error.value)

    monkeypatch.setattr(
        "agent_relay.server.os.environ",
        {
            "AGENT_RELAY_DEVICE_ID": "device-a",
            "AGENT_RELAY_AGENT_TOKEN": agent_secret,
            "AGENT_RELAY_CONTROL_TOKEN": agent_secret,
        },
    )
    monkeypatch.setattr(sys, "argv", ["agent-relay-server"])
    with pytest.raises(SystemExit):
        main()
    stderr = capsys.readouterr().err
    assert agent_secret not in stderr
    assert control_secret not in stderr


@pytest.mark.parametrize(
    ("field", "valid", "invalid"),
    [
        ("device_id", "a" * 128, "a" * 129),
        ("device_id", "device.a_1-2", "device/a"),
        ("agent_token", "a" * 256, "a" * 257),
        ("control_token", "c" * 256, "c" * 257),
    ],
)
def test_relay_settings_match_protocol_identifier_and_token_limits(
    field: str, valid: str, invalid: str
) -> None:
    base = {
        "device_id": "device-a",
        "agent_token": "agent-secret",
        "control_token": "control-secret",
    }

    assert getattr(RelaySettings(**(base | {field: valid})), field) == valid
    with pytest.raises(ValueError, match="^invalid relay server configuration$"):
        RelaySettings(**(base | {field: invalid}))
    with pytest.raises(ValueError, match="^invalid relay server configuration$"):
        RelaySettings.model_validate(base | {field: invalid})


@pytest.mark.parametrize(
    "host, accepted",
    [
        ("127.0.0.1", True),
        ("127.42.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("::", False),
        ("100.64.0.1", False),
        ("192.168.1.1", False),
        ("relay.example", False),
    ],
)
def test_server_bind_host_is_explicit_loopback(host: str, accepted: bool) -> None:
    assert _is_loopback_bind_host(host) is accepted


@pytest.mark.parametrize(
    ("host", "allow_non_loopback", "allowed_hosts", "accepted"),
    [
        ("127.0.0.1", False, (), True),
        ("0.0.0.0", False, ("relay.example:*",), False),
        ("0.0.0.0", True, (), False),
        ("0.0.0.0", True, ("relay.example:*",), True),
        ("192.168.1.20", True, ("relay.example:*",), False),
        ("::", True, ("[::1]:*",), True),
    ],
)
def test_non_loopback_bind_requires_explicit_opt_in_and_mcp_host_allowlist(
    host: str,
    allow_non_loopback: bool,
    allowed_hosts: tuple[str, ...],
    accepted: bool,
) -> None:
    assert _is_allowed_bind_host(
        host,
        allow_non_loopback=allow_non_loopback,
        mcp_allowed_hosts=allowed_hosts,
    ) is accepted


def test_server_cli_rejects_invalid_host_and_port_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "PORT_SENTINEL"
    monkeypatch.setattr(
        "agent_relay.server.os.environ",
        {
            "AGENT_RELAY_DEVICE_ID": "device-a",
            "AGENT_RELAY_AGENT_TOKEN": "agent-secret",
            "AGENT_RELAY_CONTROL_TOKEN": "control-secret",
            "AGENT_RELAY_PORT": sentinel,
        },
    )
    monkeypatch.setattr(sys, "argv", ["agent-relay-server"])
    with pytest.raises(SystemExit):
        main()
    assert sentinel not in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", ["agent-relay-server", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        main()
    assert "0.0.0.0" not in capsys.readouterr().err


def test_mcp_accepts_configured_remote_host_when_public_bind_is_enabled() -> None:
    app = create_app(
        RelaySettings(
            device_id="device-a",
            agent_token="agent-secret",
            control_token="control-secret",
            bind_host="0.0.0.0",
            mcp_allowed_hosts=("relay.example.test:*",),
            mcp_allowed_origins=("https://relay.example.test",),
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer control-secret",
                "Host": "relay.example.test:8000",
                "Origin": "https://relay.example.test",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code != 421


def test_control_endpoint_requires_distinct_bearer_token() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        path = "/v1/devices/device-a/invoke"
        missing = client.post(path, json={"tool": "system.ping"})
        wrong = client.post(
            path,
            headers={"Authorization": "Bearer nope"},
            json={"tool": "system.ping"},
        )
        assert missing.status_code == wrong.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert wrong.headers["www-authenticate"] == "Bearer"
        assert "control" not in missing.json()["detail"]


@pytest.mark.parametrize(
    ("body", "expected_type"),
    [
        ({"tool": "system.ping"}, SystemPingInvoke),
        (
            {"tool": "terminal.exec", "command_id": "pwd"},
            TerminalExecInvoke,
        ),
        ({"tool": "browser.list_tabs"}, BrowserListTabsInvoke),
        (
            {"tool": "browser.navigate", "url": "https://example.test"},
            BrowserNavigateInvoke,
        ),
        ({"tool": "browser.read_page"}, BrowserReadPageInvoke),
        (
            {"tool": "browser.fill", "element_id": "field-1", "value": "hello"},
            BrowserFillInvoke,
        ),
        ({"tool": "browser.click", "element_id": "button-1"}, BrowserClickInvoke),
        ({"tool": "computer.capture"}, ComputerCaptureInvoke),
        ({"tool": "computer.click", "element_id": "opaque-1"}, ComputerClickInvoke),
        (
            {"tool": "computer.type", "element_id": "opaque-1", "text": "hello \U0001f680"},
            ComputerTypeInvoke,
        ),
    ],
)
def test_compatibility_control_api_invokes_with_exact_typed_model(
    body: dict[str, object], expected_type: type[object]
) -> None:
    app = create_app(settings())
    calls: list[tuple[object, ...]] = []

    outputs = {
        "system.ping": {"pong": True},
        "terminal.exec": {"command_id": "pwd", "stdout": "", "stderr": "", "exit_code": 0, "timed_out": False, "stdout_truncated": False, "stderr_truncated": False},
        "browser.list_tabs": {"tabs": []},
        "browser.navigate": {"tab_id": "t", "element_id": None, "url": "https://example.test", "title": "", "success": True},
        "browser.read_page": {"tab_id": "t", "title": "", "url": "https://example.test", "text": "", "elements": []},
        "browser.fill": {"tab_id": "t", "element_id": "field-1", "url": "https://example.test", "title": "", "success": True},
        "browser.click": {"tab_id": "t", "element_id": "button-1", "url": "https://example.test", "title": "", "success": True},
        "computer.capture": {"app": "fixture", "window_title": "", "generation": "g", "elements": []},
        "computer.click": {"success": True, "generation": "g", "element_id": "opaque-1"},
        "computer.type": {"success": True, "generation": "g", "element_id": "opaque-1"},
    }

    async def invoke(*args: object) -> dict[str, object]:
        calls.append(args)
        return outputs[body["tool"]]  # type: ignore[index]

    app.state.registry.invoke = invoke
    with TestClient(app) as client:
        response = client.post(
            "/v1/devices/device-a/invoke",
            headers={"Authorization": "Bearer control-secret"},
            json=body,
        )

    assert response.status_code == 200
    assert len(calls) == 1
    device_id, message, timeout = calls[0]
    assert device_id == "device-a"
    assert type(message) is expected_type
    assert message.request_id == response.json()["request_id"]
    assert timeout == settings().max_timeout_seconds


def test_compatibility_control_api_has_closed_per_tool_request_schemas() -> None:
    schema = create_app(settings()).openapi()
    request = schema["paths"]["/v1/devices/{device_id}/invoke"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert request["discriminator"]["propertyName"] == "tool"
    variants = [
        schema["components"]["schemas"][ref["$ref"].rsplit("/", 1)[1]]
        for ref in request["oneOf"]
    ]
    by_tool = {variant["properties"]["tool"]["const"]: variant for variant in variants}
    assert set(by_tool) == {
        "system.ping",
        "terminal.exec",
        "browser.list_tabs",
        "browser.navigate",
        "browser.read_page",
        "browser.fill",
        "browser.click",
        "computer.capture",
        "computer.click",
        "computer.type",
    }
    expected = {
        "system.ping": {"tool", "timeout_seconds"},
        "terminal.exec": {"tool", "command_id", "timeout_seconds"},
        "browser.list_tabs": {"tool", "timeout_seconds"},
        "browser.navigate": {"tool", "url", "timeout_seconds"},
        "browser.read_page": {"tool", "timeout_seconds"},
        "browser.fill": {"tool", "element_id", "value", "timeout_seconds"},
        "browser.click": {"tool", "element_id", "timeout_seconds"},
        "computer.capture": {"tool", "timeout_seconds"},
        "computer.click": {"tool", "element_id", "timeout_seconds"},
        "computer.type": {"tool", "element_id", "text", "timeout_seconds"},
    }
    for tool, fields in expected.items():
        assert by_tool[tool]["additionalProperties"] is False
        assert set(by_tool[tool]["properties"]) == fields


def test_compatibility_control_api_response_schema_is_closed_recursively() -> None:
    schema = create_app(settings()).openapi()
    components = schema["components"]["schemas"]
    response = schema["paths"]["/v1/devices/{device_id}/invoke"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    seen: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            assert node.get("additionalProperties") is not True
            ref = node.get("$ref")
            if isinstance(ref, str):
                name = ref.rsplit("/", 1)[1]
                if name not in seen:
                    seen.add(name)
                    walk(components[name])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(response)
    for name in ("InvokeResponse", "ComputerCaptureOutput", "ComputerElementOutput", "ComputerActionOutput"):
        assert components[name]["additionalProperties"] is False
    request_id = components["InvokeResponse"]["properties"]["request_id"]
    assert request_id["minLength"] == 1
    assert request_id["maxLength"] == 128
    assert request_id["pattern"] == "^[A-Za-z0-9._:-]+$"


@pytest.mark.parametrize("malformed", [
    {"app": "fixture", "window_title": "Fixture", "generation": "g", "elements": [{"element_id": "e", "role": "button", "name": "Apply", "value": None, "enabled": True, "coordinates": [1, 2]}]},
    {"app": "fixture", "window_title": "Fixture", "generation": "g", "elements": [], "screenshot": "ATTACKER_SENTINEL"},
    {"success": True, "generation": "g", "element_id": "e"},
])
def test_compatibility_computer_capture_fails_closed_on_wrong_output(malformed: dict[str, object]) -> None:
    app = create_app(settings())
    async def invoke(*_args: object) -> dict[str, object]:
        return malformed
    app.state.registry.invoke = invoke
    with TestClient(app) as client:
        response = client.post("/v1/devices/device-a/invoke", headers={"Authorization": "Bearer control-secret"}, json={"tool": "computer.capture"})
    assert response.status_code == 502
    assert response.json() == {"detail": "device returned an invalid result"}
    assert "ATTACKER_SENTINEL" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"tool": "system.ping", "command_id": "pwd"},
        {"tool": "browser.list_tabs", "url": "https://example.test"},
        {"tool": "browser.navigate"},
        {"tool": "browser.fill", "element_id": "field"},
        {"tool": "browser.click", "element_id": "button", "value": "x"},
        {"tool": "computer.capture", "screenshot": True},
        {"tool": "computer.click", "element_id": "button", "coordinates": [1, 2]},
        {"tool": "computer.type", "text": "missing target"},
        {"tool": "computer.type", "element_id": "field", "text": "a\nb"},
        {"tool": "computer.type", "element_id": "field", "text": "a\u0085b"},
        {"tool": "computer.type", "element_id": "field", "text": "a\u202eb"},
    ],
)
def test_compatibility_control_api_rejects_wrong_per_tool_fields(
    body: dict[str, object],
) -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            "/v1/devices/device-a/invoke",
            headers={"Authorization": "Bearer control-secret"},
            json=body,
        )
    assert response.status_code == 422


def test_control_endpoint_rejects_non_ascii_bearer_token() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            "/v1/devices/device-a/invoke",
            headers=[(b"authorization", "Bearer token-\u00e9".encode())],
            json={"tool": "system.ping"},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("method", ["get", "post", "delete"])
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"authorization", b"Bearer wrong")],
        [(b"authorization", b"Basic control-secret")],
        [(b"authorization", b"Bearer")],
        [(b"authorization", b"bearer control-secret")],
        [(b"authorization", b"Bearer  control-secret")],
        [(b"authorization", b"Bearer control-secret extra")],
        [(b"authorization", b"Bearer token-\xff")],
        [(b"authorization", b"Bearer " + b"x" * 257)],
        [
            (b"authorization", b"Bearer control-secret"),
            (b"authorization", b"Bearer control-secret"),
        ],
    ],
)
def test_mcp_boundary_rejects_invalid_authorization(
    method: str,
    headers: list[tuple[bytes, bytes]],
) -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.request(
            method,
            "/mcp",
            headers=headers + [(b"accept", b"application/json, text/event-stream")],
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "authentication required"}


def test_mcp_canonical_path_accepts_valid_bearer_without_redirect() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer control-secret",
                "Host": "127.0.0.1:8000",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            follow_redirects=False,
        )

    assert response.status_code != 401
    assert response.status_code not in {301, 302, 303, 307, 308}


def test_mcp_slash_redirect_is_authenticated_and_points_to_canonical_path() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        missing = client.post("/mcp/", follow_redirects=False)
        authenticated = client.post(
            "/mcp/",
            headers={"Authorization": "Bearer control-secret"},
            follow_redirects=False,
        )

    assert missing.status_code == 401
    assert authenticated.status_code == 307
    assert authenticated.headers["location"] == "http://testserver/mcp"


def test_mcp_accepts_loopback_host_with_valid_bearer() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer control-secret",
                "Host": "127.0.0.1:43123",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code != 421


def test_mcp_rejects_arbitrary_host_with_valid_bearer() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer control-secret",
                "Host": "relay.example.test",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 421


def test_mcp_rejects_hostile_origin_with_valid_bearer() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer control-secret",
                "Host": "127.0.0.1:8000",
                "Origin": "https://hostile.example",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/mcp%2f", "/mcp/%2e", "/other/mcp"])
def test_unauthenticated_alternate_paths_cannot_reach_mcp(path: str) -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        response = client.post(
            path,
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            follow_redirects=False,
        )

    assert response.status_code in {401, 404}
    if response.status_code == 404:
        assert "jsonrpc" not in response.text


def test_control_endpoint_distinguishes_unknown_and_offline_device() -> None:
    app = create_app(settings())
    headers = {"Authorization": "Bearer control-secret"}
    with TestClient(app) as client:
        unknown = client.post(
            "/v1/devices/other/invoke", headers=headers, json={"tool": "system.ping"}
        )
        offline = client.post(
            "/v1/devices/device-a/invoke",
            headers=headers,
            json={"tool": "system.ping"},
        )
    assert unknown.status_code == 404
    assert offline.status_code == 503


def test_websocket_requires_register_as_first_message() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer agent-secret"}) as ws:
            ws.send_json({"version": 1, "type": "heartbeat"})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
    assert exc_info.value.code == 1002


def test_websocket_authenticates_agent_bearer_before_token_free_register_frame() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/agent", headers={"Authorization": "Bearer agent-secret"}
        ) as ws:
            ws.send_json(
                {"version": 1, "type": "register", "device_id": "device-a"}
            )
            try:
                registered = ws.receive_json()
            except WebSocketDisconnect as exc:
                pytest.fail(
                    "a valid Agent Bearer handshake must accept a token-free register "
                    f"frame (closed with {exc.code})"
                )
    assert registered == {
        "version": 1,
        "type": "registered",
        "device_id": "device-a",
    }


def test_websocket_rejects_invalid_agent_bearer_before_processing_register_frame() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        try:
            with client.websocket_connect(
                "/ws/agent", headers={"Authorization": "Bearer wrong"}
            ) as ws:
                ws.send_json(
                    {
                        "version": 1,
                        "type": "register",
                        "device_id": "device-a",
                        }
                )
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    ws.receive_json()
                assert exc_info.value.code == 1008
        except WebSocketDisconnect as exc:
            assert exc.code == 1008


def test_websocket_register_invoke_and_result() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer agent-secret"}) as ws:
            ws.send_json(
                {
                    "version": 1,
                    "type": "register",
                    "device_id": "device-a",
                }
            )
            assert ws.receive_json()["type"] == "registered"
            unavailable = client.post(
                "/v1/devices/device-a/invoke",
                headers={"Authorization": "Bearer control-secret"},
                json={"tool": "system.ping"},
            )
            assert unavailable.status_code == 409
            ws.send_json(
                {"version": 1, "type": "capabilities", "tools": ["system.ping"]}
            )
            response: dict[str, object] = {}

            def control_request() -> None:
                response["value"] = client.post(
                    "/v1/devices/device-a/invoke",
                    headers={"Authorization": "Bearer control-secret"},
                    json={"tool": "system.ping", "timeout_seconds": 1},
                )

            thread = threading.Thread(target=control_request)
            thread.start()
            invoke = ws.receive_json()
            assert invoke["type"] == "invoke"
            assert invoke["tool"] == "system.ping"
            ws.send_json(
                {
                    "version": 1,
                    "type": "result",
                    "request_id": invoke["request_id"],
                    "result": {"pong": True},
                }
            )
            thread.join(timeout=2)
            assert not thread.is_alive()
            completed = response["value"]
            assert completed.status_code == 200  # type: ignore[union-attr]
            assert completed.json() == {
                "request_id": invoke["request_id"],
                "result": {"pong": True},
            }  # type: ignore[union-attr]


def test_websocket_rejects_missing_agent_bearer_before_any_frame() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect("/ws/agent") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()

    assert exc_info.value.code == 1008
    assert "agent-secret" not in exc_info.value.reason


def test_websocket_rejects_register_frame_credentials_even_with_valid_bearer() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/agent", headers={"Authorization": "Bearer agent-secret"}
        ) as ws:
            ws.send_json(
                {
                    "version": 1,
                    "type": "register",
                    "device_id": "device-a",
                    "token": "agent-secret",
                }
            )
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()

    assert exc_info.value.code == 1002
    assert "agent-secret" not in exc_info.value.reason


def test_websocket_rejects_bad_token_and_second_connection_distinctly() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/agent", headers={"Authorization": "Bearer agent-secret"}
        ) as first:
            first.send_json(
                {"version": 1, "type": "register", "device_id": "device-a"}
            )
            assert first.receive_json()["type"] == "registered"
            with client.websocket_connect(
                "/ws/agent", headers={"Authorization": "Bearer agent-secret"}
            ) as duplicate:
                duplicate.send_json(
                    {"version": 1, "type": "register", "device_id": "device-a"}
                )
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    duplicate.receive_json()
            assert exc_info.value.code == 1013
            assert exc_info.value.reason == "device already connected"


def test_websocket_rejects_json_integer_exceeding_python_limit() -> None:
    app = create_app(settings())
    oversized_integer = "9" * 4301
    with TestClient(app) as client:
        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer agent-secret"}) as ws:
            ws.send_text(
                '{"version":1,"type":"heartbeat","integer":' + oversized_integer + "}"
            )
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()

    assert exc_info.value.code == 1002


def test_websocket_processes_capabilities_heartbeat_progress_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings())
    heartbeat_handled = threading.Event()
    capabilities_handled = threading.Event()
    progress_handled = threading.Event()
    original_heartbeat = app.state.registry.heartbeat
    original_set_capabilities = app.state.registry.set_capabilities
    original_handle_progress = app.state.registry.handle_progress

    async def observed_heartbeat(*args: object) -> None:
        await original_heartbeat(*args)
        heartbeat_handled.set()

    async def observed_set_capabilities(*args: object) -> None:
        await original_set_capabilities(*args)
        capabilities_handled.set()

    async def observed_handle_progress(*args: object) -> None:
        await original_handle_progress(*args)
        progress_handled.set()

    monkeypatch.setattr(app.state.registry, "heartbeat", observed_heartbeat)
    monkeypatch.setattr(
        app.state.registry, "set_capabilities", observed_set_capabilities
    )
    monkeypatch.setattr(app.state.registry, "handle_progress", observed_handle_progress)
    headers = {"Authorization": "Bearer control-secret"}
    with TestClient(app) as client:
        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer agent-secret"}) as ws:
            ws.send_json(
                {
                    "version": 1,
                    "type": "register",
                    "device_id": "device-a",
                }
            )
            assert ws.receive_json()["type"] == "registered"
            before = app.state.registry.last_heartbeat
            ws.send_json({"version": 1, "type": "heartbeat"})
            assert heartbeat_handled.wait(timeout=2)
            assert app.state.registry.last_heartbeat > before
            terminal = client.post(
                "/v1/devices/device-a/invoke",
                headers=headers,
                json={"tool": "terminal.exec", "command_id": "pwd"},
            )
            assert terminal.status_code == 409

            ws.send_json(
                {"version": 1, "type": "capabilities", "tools": ["terminal.exec"]}
            )
            assert capabilities_handled.wait(timeout=2)
            response: dict[str, object] = {}

            def control_request() -> None:
                response["value"] = client.post(
                    "/v1/devices/device-a/invoke",
                    headers=headers,
                    json={"tool": "terminal.exec", "command_id": "pwd"},
                )

            thread = threading.Thread(target=control_request)
            thread.start()
            invoke = ws.receive_json()
            assert invoke["tool"] == "terminal.exec"
            ws.send_json(
                {
                    "version": 1,
                    "type": "progress",
                    "request_id": invoke["request_id"],
                    "progress": 50,
                }
            )
            assert progress_handled.wait(timeout=2)
            assert app.state.registry.current_progress == 50
            ws.send_json(
                {
                    "version": 1,
                    "type": "error",
                    "request_id": invoke["request_id"],
                    "error": {"code": "failed", "message": "agent failed"},
                }
            )
            thread.join(timeout=2)
            assert not thread.is_alive()
            completed = response["value"]
            assert completed.status_code == 502  # type: ignore[union-attr]
            assert completed.json()["detail"] == {  # type: ignore[union-attr]
                "code": "failed",
                "message": "agent failed",
            }
            assert app.state.registry.current_progress is None


def test_websocket_closes_cleanly_for_a_duplicate_result() -> None:
    app = create_app(settings())
    headers = {"Authorization": "Bearer control-secret"}
    with TestClient(app) as client:
        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer agent-secret"}) as ws:
            ws.send_json(
                {
                    "version": 1,
                    "type": "register",
                    "device_id": "device-a",
                }
            )
            assert ws.receive_json()["type"] == "registered"
            ws.send_json(
                {"version": 1, "type": "capabilities", "tools": ["system.ping"]}
            )
            response: dict[str, object] = {}

            def control_request() -> None:
                response["value"] = client.post(
                    "/v1/devices/device-a/invoke",
                    headers=headers,
                    json={"tool": "system.ping", "timeout_seconds": 1},
                )

            thread = threading.Thread(target=control_request)
            thread.start()
            invoke = ws.receive_json()
            result = {
                "version": 1,
                "type": "result",
                "request_id": invoke["request_id"],
                "result": {"pong": True},
            }
            ws.send_json(result)
            thread.join(timeout=2)
            assert not thread.is_alive()
            assert response["value"].status_code == 200  # type: ignore[union-attr]
            ws.send_json(result)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
    assert exc_info.value.code == 1002
    assert exc_info.value.reason == "late or duplicate response"


def test_websocket_closes_for_a_result_sent_after_timeout() -> None:
    app = create_app(settings())
    headers = {"Authorization": "Bearer control-secret"}
    with TestClient(app) as client:
        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer agent-secret"}) as ws:
            ws.send_json(
                {
                    "version": 1,
                    "type": "register",
                    "device_id": "device-a",
                }
            )
            assert ws.receive_json()["type"] == "registered"
            ws.send_json(
                {"version": 1, "type": "capabilities", "tools": ["system.ping"]}
            )
            response: dict[str, object] = {}

            def control_request() -> None:
                response["value"] = client.post(
                    "/v1/devices/device-a/invoke",
                    headers=headers,
                    json={"tool": "system.ping", "timeout_seconds": 0.1},
                )

            thread = threading.Thread(target=control_request)
            thread.start()
            invoke = ws.receive_json()
            cancel = ws.receive_json()
            assert cancel["type"] == "cancel"
            assert cancel["request_id"] == invoke["request_id"]
            ws.send_json(
                {
                    "version": 1,
                    "type": "result",
                    "request_id": invoke["request_id"],
                    "result": {"too": "late"},
                }
            )
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            thread.join(timeout=2)
            assert not thread.is_alive()
            assert response["value"].status_code == 504  # type: ignore[union-attr]
            assert app.state.registry.pending_count == 0
    assert exc_info.value.code == 1002
    assert exc_info.value.reason == "late or duplicate response"


def test_websocket_disconnect_during_invocation_releases_caller_and_registry() -> None:
    app = create_app(settings())
    headers = {"Authorization": "Bearer control-secret"}
    with TestClient(app) as client:
        response: dict[str, object] = {}
        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer agent-secret"}) as ws:
            ws.send_json(
                {
                    "version": 1,
                    "type": "register",
                    "device_id": "device-a",
                }
            )
            assert ws.receive_json()["type"] == "registered"
            ws.send_json(
                {"version": 1, "type": "capabilities", "tools": ["system.ping"]}
            )

            def control_request() -> None:
                response["value"] = client.post(
                    "/v1/devices/device-a/invoke",
                    headers=headers,
                    json={"tool": "system.ping", "timeout_seconds": 1},
                )

            thread = threading.Thread(target=control_request)
            thread.start()
            assert ws.receive_json()["type"] == "invoke"
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert response["value"].status_code == 503  # type: ignore[union-attr]
        assert app.state.registry.pending_count == 0


def test_websocket_rejects_oversized_text_binary_and_deep_json() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer agent-secret"}) as ws:
            ws.send_text("x" * (app.state.settings.max_ws_message_bytes + 1))
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
        assert exc_info.value.code == 1009

        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer agent-secret"}) as ws:
            ws.send_bytes(b"{}")
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
        assert exc_info.value.code == 1002

        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer agent-secret"}) as ws:
            ws.send_text("{" * 1100 + "}" * 1100)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
        assert exc_info.value.code == 1002
