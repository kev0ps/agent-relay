from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest

from agent_relay.agent import (
    AgentSettings,
    ConfigurationError,
    RelayAgent,
    _private_local_path,
    _read_agent_id_file,
    _read_token_file,
    _run_with_signal_handlers,
    main,
)
from agent_relay.protocol import (
    BrowserListTabsInvoke,
    SystemPingInvoke,
    TerminalExecInvoke,
)
from agent_relay.runner import CommandResult


def _canonical_agent_environment(
    tmp_path: Path, *, url: str = "wss://relay.example.test/ws/agent"
) -> tuple[dict[str, str], Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    token_file = tmp_path / "agent.token"
    token_file.write_text("canonical-agent-secret\n", encoding="utf-8")
    token_file.chmod(0o600)
    return (
        {
            "RELAY_URL": url,
            "RELAY_AGENT_TOKEN_FILE": str(token_file),
            "RELAY_AGENT_WORKSPACE": str(workspace),
        },
        token_file,
    )


def _load_canonical_agent_settings(environment: dict[str, str]) -> AgentSettings:
    try:
        return AgentSettings.from_environment(environment)
    except ConfigurationError as exc:
        pytest.fail(f"canonical Agent environment was rejected: {exc}")
    raise AssertionError("unreachable")


def test_windows_identity_state_does_not_require_posix_mode_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / ".agent-relay"
    state_dir.mkdir()
    state_dir.chmod(0o755)
    identity_path = state_dir / "agent-id"
    identity_path.write_text("windows-agent\n", encoding="utf-8")
    identity_path.chmod(0o644)

    monkeypatch.setattr(os, "name", "nt")

    assert _private_local_path(state_dir, directory=True).st_mode
    assert _read_agent_id_file(identity_path) == "windows-agent"


def test_agent_settings_validate_url_workspace_and_mask_secret(tmp_path: Path) -> None:
    settings = AgentSettings(
        server_url="ws://127.0.0.1:8765/ws/agent",
        device_id="device-a",
        agent_token="secret-token",
        workspace=tmp_path,
    )
    assert settings.workspace == tmp_path.resolve()
    assert "secret-token" not in repr(settings)
    with pytest.raises(ConfigurationError):
        AgentSettings(
            server_url="ws://relay.example/ws/agent",
            device_id="device-a",
            agent_token="secret-token",
            workspace=tmp_path,
        )


def test_canonical_agent_environment_uses_private_token_file_and_redacts_secret(
    tmp_path: Path,
) -> None:
    environment, token_file = _canonical_agent_environment(tmp_path)
    settings = _load_canonical_agent_settings(environment)

    assert settings.server_url == environment["RELAY_URL"]
    assert settings.workspace == (tmp_path / "workspace").resolve()
    assert settings.agent_token.get_secret_value() == "canonical-agent-secret"
    assert "canonical-agent-secret" not in repr(settings)
    assert token_file.is_file()
    assert not token_file.is_symlink()
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_generated_agent_id_is_stable_across_configuration_reloads(tmp_path: Path) -> None:
    environment, _ = _canonical_agent_environment(tmp_path)
    first = _load_canonical_agent_settings(environment)
    second = _load_canonical_agent_settings(environment)

    first_id = getattr(first, "agent_id", None)
    second_id = getattr(second, "agent_id", None)
    assert isinstance(first_id, str) and first_id
    assert first_id == second_id


def test_existing_agent_id_is_preserved_instead_of_silently_replaced(
    tmp_path: Path,
) -> None:
    environment, _ = _canonical_agent_environment(tmp_path)
    environment["RELAY_AGENT_ID"] = "provisioned-agent-1"

    settings = _load_canonical_agent_settings(environment)

    assert getattr(settings, "agent_id", None) == "provisioned-agent-1"


@pytest.mark.parametrize(
    ("url", "allow_insecure_ws", "accepted"),
    [
        ("ws://relay.example.test/ws/agent", "true", True),
        ("ws://relay.example.test/ws/agent", "false", False),
        ("wss://relay.example.test/ws/agent", "false", True),
        ("ws://127.0.0.1/ws/agent", "false", True),
    ],
)
def test_canonical_agent_transport_policy(
    tmp_path: Path,
    url: str,
    allow_insecure_ws: str,
    accepted: bool,
) -> None:
    environment, _ = _canonical_agent_environment(tmp_path, url=url)
    environment["RELAY_ALLOW_INSECURE_WS"] = allow_insecure_ws

    try:
        settings = AgentSettings.from_environment(environment)
    except ConfigurationError as exc:
        if accepted:
            pytest.fail(f"allowed Agent URL was rejected: {exc}")
        return

    if not accepted:
        pytest.fail("non-loopback ws:// was accepted while insecure transport was disabled")
    assert settings.server_url == url


def test_computer_settings_are_disabled_by_default_and_all_or_none(
    tmp_path: Path,
) -> None:
    base = dict(
        server_url="ws://localhost/ws/agent",
        device_id="d",
        agent_token="secret",
        workspace=tmp_path,
    )
    settings = AgentSettings(**base)
    assert settings.computer_driver_path is None
    assert settings.computer_startup_timeout_seconds == 15
    with pytest.raises(ConfigurationError):
        AgentSettings(**base, computer_driver_path=tmp_path / "missing")
    driver = tmp_path / "cua-driver"
    driver.write_text("#!/bin/sh\n")
    driver.chmod(0o755)
    with pytest.raises(ConfigurationError):
        AgentSettings(**base, computer_driver_path=driver)
    configured = AgentSettings(
        **base,
        computer_driver_path=driver,
        computer_allowed_app_name="Fixture",
        computer_allowed_window_title="Relay Desktop Fixture",
    )
    agent = RelayAgent(configured)
    assert {"computer.capture", "computer.click", "computer.type"} <= set(
        agent._capabilities
    )
    with pytest.raises(ConfigurationError):
        AgentSettings(
            server_url="wss://relay.example/not-agent",
            device_id="device-a",
            agent_token="secret-token",
            workspace=tmp_path / "missing",
        )


def test_computer_starts_before_browser_when_both_use_the_desktop(
    tmp_path: Path,
) -> None:
    driver = tmp_path / "cua-driver"
    driver.write_text("#!/bin/sh\n")
    driver.chmod(0o755)
    agent = RelayAgent(
        AgentSettings(
            server_url="ws://localhost/ws/agent",
            device_id="d",
            agent_token="secret",
            workspace=tmp_path,
            browser_cdp_url="http://127.0.0.1:9222",
            browser_allowed_origins=("http://127.0.0.1:8899",),
            computer_driver_path=driver,
            computer_allowed_app_name="relay-desktop-fixture",
            computer_allowed_window_title="Relay Desktop Fixture",
        )
    )
    starts: list[str] = []
    for ident in agent._unique_capabilities:
        capability = agent._capability_objects[ident]

        async def start(name: str = type(capability).__name__) -> None:
            starts.append(name)

        capability.start = start  # type: ignore[method-assign]

    asyncio.run(agent._start_capabilities())

    assert starts[-2:] == ["ComputerCapability", "BrowserCapability"]


@pytest.mark.parametrize(
    "url, accepted",
    [
        ("wss://relay.example/ws/agent", True),
        ("ws://localhost/ws/agent", True),
        ("ws://127.42.0.1/ws/agent", True),
        ("ws://[::1]/ws/agent", True),
        ("ws://relay.example/ws/agent", False),
        ("ws://localhost.example/ws/agent", False),
        ("ws://127.0.0.1.example/ws/agent", False),
        ("ws://user@localhost/ws/agent", False),
        ("wss://user@relay.example/ws/agent", False),
    ],
)
def test_agent_settings_only_permit_explicit_loopback_ws(
    tmp_path: Path, url: str, accepted: bool
) -> None:
    values = {
        "server_url": url,
        "device_id": "device-a",
        "agent_token": "secret-token",
        "workspace": tmp_path,
    }
    if accepted:
        assert AgentSettings(**values).server_url == url
    else:
        with pytest.raises(ConfigurationError):
            AgentSettings(**values)


def test_configuration_failures_never_echo_agent_token(tmp_path: Path) -> None:
    secret = "AGENT_TOKEN_SENTINEL"
    invalid_values = [
        {"server_url": "ws://relay.example/ws/agent"},
        {"device_id": "bad space"},
        {"agent_token": ""},
        {"workspace": tmp_path / "missing"},
        {"reconnect_min_seconds": 2, "reconnect_max_seconds": 1},
        {"stdout_limit": 48 * 1024, "stderr_limit": 48 * 1024},
    ]
    base = {
        "server_url": "ws://localhost/ws/agent",
        "device_id": "device-a",
        "agent_token": secret,
        "workspace": tmp_path,
    }
    for invalid in invalid_values:
        with pytest.raises(ConfigurationError) as error:
            AgentSettings(**(base | invalid))
        assert str(error.value) == "invalid agent configuration"
        assert secret not in str(error.value)
    with pytest.raises(ConfigurationError) as error:
        AgentSettings.model_validate(base | {"agent_token": ""})
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "cdp_url",
    [
        "ws://127.0.0.1:9222",
        "http://0.0.0.0:9222",
        "http://[::]:9222",
        "http://example.com:9222",
        "http://user@127.0.0.1:9222",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:9222/json",
        "http://127.0.0.1:9222/?secret=x",
        "http://127.0.0.1:9222/#fragment",
    ],
)
def test_browser_configuration_requires_safe_explicit_loopback_cdp(
    tmp_path: Path, cdp_url: str
) -> None:
    with pytest.raises(ConfigurationError) as error:
        AgentSettings(
            server_url="ws://localhost/ws/agent",
            device_id="d",
            agent_token="secret",
            workspace=tmp_path,
            browser_cdp_url=cdp_url,
            browser_allowed_origins=("http://127.0.0.1:8899",),
        )
    assert str(error.value) == "invalid agent configuration"


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "http://*.example.com",
        "ws://127.0.0.1:8899",
        "http://user@127.0.0.1:8899",
        "http://127.0.0.1:8899/path",
        "http://127.0.0.1:8899/?query=x",
        "http://127.0.0.1:8899/#fragment",
    ],
)
def test_browser_allowed_origins_are_exact_and_partial_config_is_rejected(
    tmp_path: Path, origin: str
) -> None:
    base = dict(
        server_url="ws://localhost/ws/agent",
        device_id="d",
        agent_token="secret",
        workspace=tmp_path,
    )
    with pytest.raises(ConfigurationError):
        AgentSettings(**base, browser_cdp_url="http://127.0.0.1:9222")
    with pytest.raises(ConfigurationError):
        AgentSettings(
            **base,
            browser_cdp_url="http://127.0.0.1:9222",
            browser_allowed_origins=(origin,),
        )


def test_environment_and_cli_configuration_errors_never_echo_agent_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "AGENT_TOKEN_SENTINEL"
    env = {
        "AGENT_RELAY_SERVER_URL": "ws://relay.example/ws/agent",
        "AGENT_RELAY_DEVICE_ID": "device-a",
        "AGENT_RELAY_WORKSPACE": str(tmp_path),
        "AGENT_RELAY_AGENT_TOKEN": secret,
    }
    with pytest.raises(ConfigurationError) as error:
        AgentSettings.from_environment(env)
    assert secret not in str(error.value)

    monkeypatch.setattr("agent_relay.agent.os.environ", env)
    monkeypatch.setattr(sys, "argv", ["agent-relay-agent"])
    with pytest.raises(SystemExit):
        main()
    assert secret not in capsys.readouterr().err


class _Connection(AbstractAsyncContextManager["_Socket"]):
    def __init__(self, socket: _Socket | None = None, error: Exception | None = None) -> None:
        self.socket = socket
        self.error = error

    async def __aenter__(self) -> _Socket:
        if self.error is not None:
            raise self.error
        assert self.socket is not None
        return self.socket

    async def __aexit__(self, *_: object) -> None:
        return None


def test_backoff_does_not_reset_after_registered_session_disconnects(tmp_path: Path) -> None:
    class DisconnectingSocket(_Socket):
        async def recv(self) -> str:
            if not self.inbound.empty():
                return await super().recv()
            raise ConnectionError("disconnected")

    async def scenario() -> None:
        connections = iter(
            [
                _Connection(error=ConnectionError("before handshake")),
                _Connection(error=ConnectionError("before handshake")),
                _Connection(
                    DisconnectingSocket(
                        [json.dumps({"version": 1, "type": "registered", "device_id": "d"})]
                    )
                ),
            ]
        )
        agent = RelayAgent(
            AgentSettings(
                server_url="ws://localhost/ws/agent",
                device_id="d",
                agent_token="token",
                workspace=tmp_path,
                reconnect_min_seconds=1,
                reconnect_max_seconds=8,
                heartbeat_interval_seconds=60,
            ),
            connector=lambda *_, **__: next(connections),
        )
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            if delay == 60:
                await agent._stop_event.wait()
                return
            delays.append(delay)
            if len(delays) == 3:
                agent.stop()

        agent._sleep_or_stop = record_sleep  # type: ignore[method-assign]
        await agent.run()
        assert delays == [1, 2, 4]

    asyncio.run(scenario())


def test_backoff_resets_only_after_stable_registered_session(tmp_path: Path) -> None:
    class DisconnectingSocket(_Socket):
        async def recv(self) -> str:
            if not self.inbound.empty():
                return await super().recv()
            clock[0] += 30
            raise ConnectionError("disconnected")

    async def scenario() -> None:
        connections = iter(
            [
                _Connection(error=ConnectionError("before handshake")),
                _Connection(DisconnectingSocket([json.dumps({"version": 1, "type": "registered", "device_id": "d"})])),
            ]
        )
        agent = RelayAgent(
            AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path, reconnect_min_seconds=1, reconnect_max_seconds=8, stable_session_seconds=10, heartbeat_interval_seconds=60),
            connector=lambda *_, **__: next(connections),
            monotonic=lambda: clock[0],
        )
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            if delay == 60:
                await agent._stop_event.wait()
                return
            delays.append(delay)
            if len(delays) == 2:
                agent.stop()

        agent._sleep_or_stop = record_sleep  # type: ignore[method-assign]
        await agent.run()
        assert delays == [1, 1]

    clock = [0.0]
    asyncio.run(scenario())


def test_agent_reregisters_and_invokes_after_socket_loss(
    tmp_path: Path,
) -> None:
    class DisconnectingSocket(_Socket):
        async def recv(self) -> str:
            if self.inbound.empty():
                raise ConnectionError("socket lost")
            return await super().recv()

    async def scenario() -> None:
        first = DisconnectingSocket(
            [json.dumps({"version": 1, "type": "registered", "device_id": "d"})]
        )
        second = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "device_id": "d"}),
                json.dumps(
                    {
                        "version": 1,
                        "type": "invoke",
                        "request_id": "after-reconnect",
                        "tool": "system.ping",
                    }
                ),
            ]
        )
        connections = iter([_Connection(first), _Connection(second)])
        agent = RelayAgent(
            AgentSettings(
                server_url="ws://localhost/ws/agent",
                device_id="d",
                agent_token="token",
                workspace=tmp_path,
                reconnect_min_seconds=0.001,
                reconnect_max_seconds=0.001,
                heartbeat_interval_seconds=60,
            ),
            capabilities=[_Capability("system.ping", result={"pong": True})],
            connector=lambda *_, **__: next(connections),
        )

        async def no_delay(_: float) -> None:
            await asyncio.sleep(0)

        agent._sleep_or_stop = no_delay  # type: ignore[method-assign]
        task = asyncio.create_task(agent.run())
        for _ in range(100):
            if any(
                message.get("request_id") == "after-reconnect"
                for message in second.sent
            ):
                break
            await asyncio.sleep(0.001)
        agent.stop()
        await asyncio.wait_for(task, timeout=1)

        assert [message["type"] for message in first.sent][:2] == [
            "register",
            "capabilities",
        ]
        assert [message["type"] for message in second.sent][:2] == [
            "register",
            "capabilities",
        ]
        assert [
            message
            for message in second.sent
            if message.get("request_id") == "after-reconnect"
        ] == [
            {
                "version": 1,
                "type": "result",
                "request_id": "after-reconnect",
                "result": {"pong": True},
            }
        ]

    asyncio.run(scenario())


def test_websocket_connections_send_bearer_only_in_handshake_options(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        seen: dict[str, object] = {}

        def connector(*_: object, **kwargs: object) -> _Connection:
            seen.update(kwargs)
            raise ConnectionError("unused")

        agent = RelayAgent(
            AgentSettings(
                server_url="ws://localhost/ws/agent",
                device_id="d",
                agent_token="agent-handshake-secret",
                workspace=tmp_path,
                reconnect_min_seconds=0.001,
                reconnect_max_seconds=0.001,
            ),
            connector=connector,
        )

        async def stop_after_retry(_: float) -> None:
            agent.stop()

        agent._sleep_or_stop = stop_after_retry  # type: ignore[method-assign]
        await agent.run()
        assert seen["additional_headers"] == {
            "Authorization": "Bearer agent-handshake-secret"
        }
        assert seen["proxy"] is None
        assert "agent-handshake-secret" not in json.dumps(
            {key: value for key, value in seen.items() if key != "additional_headers"}
        )

    asyncio.run(scenario())


def test_websocket_connections_disable_proxy_even_with_hostile_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        seen: dict[str, object] = {}

        def connector(*_: object, **kwargs: object) -> _Connection:
            seen.update(kwargs)
            agent.stop()
            return _Connection(error=ConnectionError("unused"))

        agent = RelayAgent(AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path), connector=connector)
        await agent.run()
        assert seen["proxy"] is None

    monkeypatch.setenv("HTTPS_PROXY", "http://hostile.invalid:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://hostile.invalid:8080")
    asyncio.run(scenario())


def test_stopped_agent_does_not_sleep(tmp_path: Path) -> None:
    async def scenario() -> None:
        agent = RelayAgent(
            AgentSettings(
                server_url="ws://localhost/ws/agent",
                device_id="d",
                agent_token="token",
                workspace=tmp_path,
            )
        )
        agent.stop()

        async def unexpected_sleep(_: float) -> None:
            raise AssertionError("a stopped agent must not sleep")

        agent._sleep_or_stop = unexpected_sleep  # type: ignore[method-assign]
        await agent.run()

    asyncio.run(scenario())


class _Runner:
    async def run(self, command_id: str) -> CommandResult:
        assert command_id == "pwd"
        return CommandResult(stdout="/local/workspace\n", exit_code=0)


class _Socket:
    def __init__(self, inbound: list[str]) -> None:
        self.inbound = asyncio.Queue()
        for item in inbound:
            self.inbound.put_nowait(item)
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        return await self.inbound.get()


def test_agent_register_frame_contains_no_agent_token(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = _Socket(
            [json.dumps({"version": 1, "type": "registered", "device_id": "d"})]
        )
        agent = RelayAgent(
            AgentSettings(
                server_url="ws://localhost/ws/agent",
                device_id="d",
                agent_token="secret-token",
                workspace=tmp_path,
                heartbeat_interval_seconds=60,
            )
        )
        task = asyncio.create_task(agent.run_session(socket))
        for _ in range(100):
            if len(socket.sent) >= 2:
                break
            await asyncio.sleep(0)
        agent.stop()
        await task

        register = socket.sent[0]
        assert register["type"] == "register"
        assert "token" not in register
        assert "agent_token" not in register
        assert "secret-token" not in json.dumps(register)

    asyncio.run(scenario())


class _Capability:
    def __init__(
        self,
        name: str,
        *,
        result: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.tools = frozenset({name})
        self.result = result or {"capability": name}
        self.error = error
        self.invocations: list[SystemPingInvoke | TerminalExecInvoke] = []
        self.closed = 0
        self.unavailable = asyncio.Event()

    async def start(self) -> None:
        return None

    async def wait_unavailable(self) -> None:
        await self.unavailable.wait()

    async def invoke(
        self, message: SystemPingInvoke | TerminalExecInvoke
    ) -> dict[str, object]:
        self.invocations.append(message)
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        self.closed += 1


def test_capability_start_failure_never_opens_websocket_or_advertises(tmp_path: Path) -> None:
    class FailingCapability(_Capability):
        async def start(self) -> None:
            raise RuntimeError("not ready")

    async def scenario() -> None:
        opened = 0

        def connector(*_: object, **__: object) -> _Connection:
            nonlocal opened
            opened += 1
            return _Connection(_Socket([]))

        capability = FailingCapability("system.ping")
        agent = RelayAgent(
            AgentSettings(
                server_url="ws://localhost/ws/agent",
                device_id="d",
                agent_token="token",
                workspace=tmp_path,
                reconnect_min_seconds=0.01,
                reconnect_max_seconds=0.01,
            ),
            capabilities=[capability],
            connector=connector,
        )
        async def stop_after_retry(_: float) -> None:
            agent.stop()
        agent._sleep_or_stop = stop_after_retry  # type: ignore[method-assign]
        await agent.run()
        assert opened == 0

    asyncio.run(scenario())


def test_browser_advertisement_is_canonical_and_only_after_start(tmp_path: Path) -> None:
    class BrowserCapability(_Capability):
        def __init__(self) -> None:
            super().__init__("browser.list_tabs")
            self.tools = frozenset({
                "browser.list_tabs", "browser.navigate", "browser.read_page",
                "browser.fill", "browser.click",
            })
            self.started = False

        async def start(self) -> None:
            self.started = True

    async def scenario() -> None:
        browser = BrowserCapability()
        socket = _Socket([json.dumps({"version": 1, "type": "registered", "device_id": "d"})])
        agent: RelayAgent

        def connector(*_: object, **__: object) -> _Connection:
            assert browser.started
            return _Connection(socket)

        agent = RelayAgent(
            AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path, heartbeat_interval_seconds=60),
            capabilities=[browser], connector=connector,
        )
        task = asyncio.create_task(agent.run())
        while len(socket.sent) < 2:
            await asyncio.sleep(0)
        agent.stop()
        await task
        assert socket.sent[1]["tools"] == [
            "browser.list_tabs", "browser.navigate", "browser.read_page",
            "browser.fill", "browser.click",
        ]

    asyncio.run(scenario())


def test_capability_unavailable_cancels_active_browser_action_without_result(tmp_path: Path) -> None:
    class BlockingBrowser(_Capability):
        cancelled = False

        async def invoke(self, message: BrowserListTabsInvoke) -> dict[str, object]:
            self.invocations.append(message)  # type: ignore[arg-type]
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def scenario() -> None:
        browser = BlockingBrowser("browser.list_tabs")
        socket = _Socket([
            json.dumps({"version": 1, "type": "registered", "device_id": "d"}),
            json.dumps({"version": 1, "type": "invoke", "request_id": "r", "tool": "browser.list_tabs"}),
        ])
        agent = RelayAgent(
            AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path, heartbeat_interval_seconds=60),
            capabilities=[browser],
        )
        task = asyncio.create_task(agent.run_session(socket))
        while not browser.invocations:
            await asyncio.sleep(0)
        browser.unavailable.set()
        with pytest.raises(ConnectionError):
            await task
        assert browser.cancelled
        assert not any(item.get("request_id") == "r" for item in socket.sent)

    asyncio.run(scenario())


def test_unique_multi_tool_capability_closes_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        capability = _Capability("system.ping")
        capability.tools = frozenset({"system.ping", "terminal.exec"})
        agent = RelayAgent(
            AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path),
            capabilities=[capability],
        )
        await asyncio.gather(agent.aclose(), agent.aclose())
        assert capability.closed == 1

    asyncio.run(scenario())


def test_run_loop_retries_capability_start_after_unavailable(tmp_path: Path) -> None:
    class RestartingCapability(_Capability):
        starts = 0

        async def start(self) -> None:
            self.starts += 1
            if self.starts == 1:
                raise RuntimeError("temporarily unavailable")

    async def scenario() -> None:
        capability = RestartingCapability("system.ping")
        agent: RelayAgent

        def connector(*_: object, **__: object) -> _Connection:
            agent.stop()
            return _Connection(error=ConnectionError("done"))

        agent = RelayAgent(
            AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path, reconnect_min_seconds=0.001, reconnect_max_seconds=0.001),
            capabilities=[capability], connector=connector,
        )

        async def no_delay(_: float) -> None:
            return None

        agent._sleep_or_stop = no_delay  # type: ignore[method-assign]
        await agent.run()
        assert capability.starts == 2

    asyncio.run(scenario())


def test_default_agent_advertises_exactly_built_in_capabilities(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = _Socket(
            [json.dumps({"version": 1, "type": "registered", "device_id": "d"})]
        )
        agent = RelayAgent(
            AgentSettings(
                server_url="ws://localhost/ws/agent",
                device_id="d",
                agent_token="token",
                workspace=tmp_path,
                heartbeat_interval_seconds=60,
            )
        )
        task = asyncio.create_task(agent.run_session(socket))
        for _ in range(100):
            if len(socket.sent) >= 2:
                break
            await asyncio.sleep(0.001)
        agent.stop()
        await task
        assert socket.sent[1] == {
            "version": 1,
            "type": "capabilities",
            "tools": ["system.ping", "terminal.exec"],
        }

    asyncio.run(scenario())


def test_each_typed_invocation_dispatches_only_to_matching_capability(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        system = _Capability("system.ping")
        terminal = _Capability("terminal.exec")
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "device_id": "d"}),
                json.dumps(
                    {
                        "version": 1,
                        "type": "invoke",
                        "request_id": "ping",
                        "tool": "system.ping",
                    }
                ),
                json.dumps(
                    {
                        "version": 1,
                        "type": "invoke",
                        "request_id": "exec",
                        "tool": "terminal.exec",
                        "command_id": "pwd",
                    }
                ),
            ]
        )
        agent = RelayAgent(
            AgentSettings(
                server_url="ws://localhost/ws/agent",
                device_id="d",
                agent_token="token",
                workspace=tmp_path,
                heartbeat_interval_seconds=60,
            ),
            capabilities=[system, terminal],
        )
        task = asyncio.create_task(agent.run_session(socket))
        for _ in range(100):
            if len(system.invocations) == len(terminal.invocations) == 1:
                break
            await asyncio.sleep(0.001)
        agent.stop()
        await task
        assert [message.request_id for message in system.invocations] == ["ping"]
        assert [message.request_id for message in terminal.invocations] == ["exec"]

    asyncio.run(scenario())


def test_unknown_and_duplicate_capabilities_are_rejected(tmp_path: Path) -> None:
    settings = AgentSettings(
        server_url="ws://localhost/ws/agent",
        device_id="d",
        agent_token="token",
        workspace=tmp_path,
    )
    with pytest.raises(ValueError, match="unsupported local capability"):
        RelayAgent(settings, capabilities=[_Capability("unknown.action")])
    with pytest.raises(ValueError, match="at least one local capability"):
        RelayAgent(settings, capabilities=[])
    with pytest.raises(ValueError, match="duplicate local capability"):
        RelayAgent(
            settings,
            capabilities=[_Capability("system.ping"), _Capability("system.ping")],
        )


def test_capability_exception_becomes_safe_agent_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        capability = _Capability(
            "system.ping", error=RuntimeError("sensitive capability detail")
        )
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "device_id": "d"}),
                json.dumps(
                    {
                        "version": 1,
                        "type": "invoke",
                        "request_id": "r",
                        "tool": "system.ping",
                    }
                ),
            ]
        )
        agent = RelayAgent(
            AgentSettings(
                server_url="ws://localhost/ws/agent",
                device_id="d",
                agent_token="token",
                workspace=tmp_path,
                heartbeat_interval_seconds=60,
            ),
            capabilities=[capability],
        )
        task = asyncio.create_task(agent.run_session(socket))
        for _ in range(100):
            if any(message["type"] == "error" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        agent.stop()
        await task
        assert socket.sent[-1] == {
            "version": 1,
            "type": "error",
            "request_id": "r",
            "error": {"code": "agent_error", "message": "local action failed"},
        }

    asyncio.run(scenario())


def test_terminal_runner_failure_preserves_safe_command_error(tmp_path: Path) -> None:
    class FailingRunner:
        async def run(self, command_id: str) -> CommandResult:
            return CommandResult(error="sensitive runner detail")

    async def scenario() -> None:
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "device_id": "d"}),
                json.dumps({"version": 1, "type": "invoke", "request_id": "r", "tool": "terminal.exec", "command_id": "pwd"}),
            ]
        )
        agent = RelayAgent(
            AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path, heartbeat_interval_seconds=60),
            runner=FailingRunner(),
        )
        task = asyncio.create_task(agent.run_session(socket))
        for _ in range(100):
            if any(message["type"] == "error" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        agent.stop()
        await task
        assert socket.sent[-1] == {
            "version": 1,
            "type": "error",
            "request_id": "r",
            "error": {
                "code": "command_failed",
                "message": "configured command failed",
            },
        }

    asyncio.run(scenario())


def test_cancellation_reaches_active_capability_and_suppresses_late_result(
    tmp_path: Path,
) -> None:
    class BlockingCapability(_Capability):
        cancelled = False

        async def invoke(
            self, message: SystemPingInvoke | TerminalExecInvoke
        ) -> dict[str, object]:
            self.invocations.append(message)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def scenario() -> None:
        terminal = BlockingCapability("terminal.exec")
        system = _Capability("system.ping", result={"pong": True})
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "device_id": "d"}),
                json.dumps({"version": 1, "type": "invoke", "request_id": "old", "tool": "terminal.exec", "command_id": "pwd"}),
                json.dumps({"version": 1, "type": "cancel", "request_id": "old", "reason": "stop"}),
                json.dumps({"version": 1, "type": "invoke", "request_id": "new", "tool": "system.ping"}),
            ]
        )
        agent = RelayAgent(
            AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path, heartbeat_interval_seconds=60),
            capabilities=[system, terminal],
        )
        task = asyncio.create_task(agent.run_session(socket))
        for _ in range(100):
            if any(message.get("request_id") == "new" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        agent.stop()
        await task
        assert terminal.cancelled
        assert [message.get("request_id") for message in socket.sent if message["type"] == "result"] == ["new"]

    asyncio.run(scenario())


def test_only_one_capability_action_runs_at_a_time(tmp_path: Path) -> None:
    class BlockingCapability(_Capability):
        started = asyncio.Event()
        release = asyncio.Event()

        async def invoke(
            self, message: SystemPingInvoke | TerminalExecInvoke
        ) -> dict[str, object]:
            self.invocations.append(message)
            self.started.set()
            await self.release.wait()
            return self.result

    async def scenario() -> None:
        blocking = BlockingCapability("system.ping")
        terminal = _Capability("terminal.exec")
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "device_id": "d"}),
                json.dumps({"version": 1, "type": "invoke", "request_id": "first", "tool": "system.ping"}),
                json.dumps({"version": 1, "type": "invoke", "request_id": "second", "tool": "terminal.exec", "command_id": "pwd"}),
            ]
        )
        agent = RelayAgent(
            AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path, heartbeat_interval_seconds=60),
            capabilities=[blocking, terminal],
        )
        task = asyncio.create_task(agent.run_session(socket))
        await blocking.started.wait()
        for _ in range(100):
            if any(message["type"] == "error" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        assert terminal.invocations == []
        assert socket.sent[-1] == {
            "version": 1,
            "type": "error",
            "request_id": "second",
            "error": {"code": "busy", "message": "an action is already running"},
        }
        blocking.release.set()
        agent.stop()
        await task

    asyncio.run(scenario())


def test_agent_shutdown_awaits_every_capability_close_after_partial_failure(
    tmp_path: Path,
) -> None:
    class ClosingCapability(_Capability):
        def __init__(self, name: str, *, fail: bool = False) -> None:
            super().__init__(name)
            self.fail = fail

        async def aclose(self) -> None:
            await asyncio.sleep(0)
            self.closed += 1
            if self.fail:
                raise RuntimeError("close failed")

    async def scenario() -> None:
        first = ClosingCapability("system.ping", fail=True)
        second = ClosingCapability("terminal.exec")
        agent: RelayAgent

        def connector(*_: object, **__: object) -> _Connection:
            agent.stop()
            return _Connection(error=ConnectionError("partial startup failure"))

        agent = RelayAgent(
            AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path),
            capabilities=[first, second],
            connector=connector,
        )
        await agent.run()
        assert first.closed == 1
        assert second.closed == 1

    asyncio.run(scenario())


def test_agent_close_defers_cancellation_and_is_shared(tmp_path: Path) -> None:
    class BlockingCapability(_Capability):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.close_started = asyncio.Event()
            self.close_allowed = asyncio.Event()

        async def aclose(self) -> None:
            self.closed += 1
            self.close_started.set()
            await self.close_allowed.wait()

    async def scenario() -> None:
        shared = BlockingCapability("system.ping")
        shared.tools = frozenset({"system.ping", "terminal.exec"})  # type: ignore[assignment]
        agent = RelayAgent(
            AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path),
            capabilities=[shared],
        )
        first = asyncio.create_task(agent.aclose())
        await shared.close_started.wait()
        second = asyncio.create_task(agent.aclose())
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        shared.close_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        await agent.aclose()
        assert shared.closed == 1

    asyncio.run(scenario())


def test_agent_handshake_capabilities_and_terminal_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "device_id": "d"}),
                json.dumps(
                    {
                        "version": 1,
                        "type": "invoke",
                        "request_id": "r",
                        "tool": "terminal.exec",
                        "command_id": "pwd",
                    }
                ),
            ]
        )
        settings = AgentSettings(
            server_url="ws://127.0.0.1:8765/ws/agent",
            device_id="d",
            agent_token="token",
            workspace=tmp_path,
            heartbeat_interval_seconds=60,
        )
        agent = RelayAgent(settings, runner=_Runner())
        task = asyncio.create_task(agent.run_session(socket))
        for _ in range(100):
            if len(socket.sent) >= 3:
                break
            await asyncio.sleep(0.001)
        agent.stop()
        await task
        assert [message["type"] for message in socket.sent] == [
            "register",
            "capabilities",
            "result",
        ]
        result = socket.sent[-1]["result"]
        assert result == {
            "command_id": "pwd",
            "stdout": "/local/workspace\n",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    asyncio.run(scenario())


def test_agent_cancel_suppresses_late_result_and_allows_next_action(tmp_path: Path) -> None:
    class BlockingRunner:
        cancelled = False

        async def run(self, command_id: str) -> CommandResult:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def scenario() -> None:
        socket = _Socket(
            [
                json.dumps({"version": 1, "type": "registered", "device_id": "d"}),
                json.dumps({"version": 1, "type": "invoke", "request_id": "old", "tool": "terminal.exec", "command_id": "pwd"}),
                json.dumps({"version": 1, "type": "cancel", "request_id": "old", "reason": "stop"}),
                json.dumps({"version": 1, "type": "invoke", "request_id": "new", "tool": "system.ping"}),
            ]
        )
        runner = BlockingRunner()
        agent = RelayAgent(AgentSettings(server_url="ws://localhost:8765/ws/agent", device_id="d", agent_token="token", workspace=tmp_path, heartbeat_interval_seconds=60), runner=runner)
        task = asyncio.create_task(agent.run_session(socket))
        for _ in range(100):
            if any(message.get("request_id") == "new" for message in socket.sent):
                break
            await asyncio.sleep(0.001)
        agent.stop()
        await task
        assert runner.cancelled
        assert [message.get("request_id") for message in socket.sent if message["type"] == "result"] == ["new"]

    asyncio.run(scenario())


def test_token_file_must_be_private(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    token_file.chmod(0o644)
    env = {
        "AGENT_RELAY_SERVER_URL": "ws://127.0.0.1:9999/ws/agent",
        "AGENT_RELAY_DEVICE_ID": "device-a",
        "AGENT_RELAY_WORKSPACE": str(tmp_path),
        "AGENT_RELAY_AGENT_TOKEN_FILE": str(token_file),
    }
    with pytest.raises(ConfigurationError, match="invalid agent configuration"):
        AgentSettings.from_environment(env)
    token_file.chmod(0o600)
    assert "secret" not in repr(AgentSettings.from_environment(env))


def test_token_file_refuses_symlink_fifo_and_oversize(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("secret")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        _read_token_file(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError):
        _read_token_file(fifo)
    large = tmp_path / "large"
    large.write_text("x" * 4097)
    large.chmod(0o600)
    with pytest.raises(ValueError):
        _read_token_file(large)
    assert _read_token_file(target) == "secret"


def test_signal_handlers_stop_agent_and_wait_for_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Loop:
        handlers: list[object] = []

        def add_signal_handler(self, signum: object, callback: object) -> None:
            self.handlers.append((signum, callback))

    async def scenario() -> None:
        agent = RelayAgent(AgentSettings(server_url="ws://localhost/ws/agent", device_id="d", agent_token="token", workspace=tmp_path))
        completed = False

        async def run() -> None:
            nonlocal completed
            callback = Loop.handlers[0][1]
            callback()  # type: ignore[operator]
            assert agent._stop_event.is_set()
            completed = True

        agent.run = run  # type: ignore[method-assign]
        await _run_with_signal_handlers(agent)
        assert completed

    Loop.handlers = []
    monkeypatch.setattr("agent_relay.agent.asyncio.get_running_loop", lambda: Loop())
    asyncio.run(scenario())


def test_signal_handlers_are_optional_on_windows_event_loops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class WindowsLoop:
        def add_signal_handler(self, signum: object, callback: object) -> None:
            raise NotImplementedError

    async def scenario() -> None:
        agent = RelayAgent(
            AgentSettings(
                server_url="ws://localhost/ws/agent",
                device_id="d",
                agent_token="token",
                workspace=tmp_path,
            )
        )
        completed = False

        async def run() -> None:
            nonlocal completed
            completed = True

        agent.run = run  # type: ignore[method-assign]
        await _run_with_signal_handlers(agent)
        assert completed

    monkeypatch.setattr(
        "agent_relay.agent.asyncio.get_running_loop", lambda: WindowsLoop()
    )
    asyncio.run(scenario())
