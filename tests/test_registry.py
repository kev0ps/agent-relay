from __future__ import annotations

import asyncio
import inspect

import pytest

from agent_relay.output_models import ProviderToolResult
from agent_relay.protocol import (
    AgentError,
    AgentResult,
    Capabilities,
    InvokeMessage,
    Progress,
    Register,
)
from agent_relay.registry import (
    AuthenticationError,
    DeviceAlreadyConnectedError,
    DeviceBusyError,
    DeviceOfflineError,
    DuplicateRequestError,
    LateResponseError,
    RelayRegistry,
    UnknownRequestError,
    UnsupportedToolError,
)


class FakeSocket:
    def __init__(self) -> None:
        self.messages: list[object] = []

    async def send_json(self, message: object) -> None:
        self.messages.append(message)


class BlockingCancelSocket(FakeSocket):
    """Hold cancellation delivery open to exercise the pending-cleanup window."""

    def __init__(self) -> None:
        super().__init__()
        self.cancel_started = asyncio.Event()
        self.release_cancel = asyncio.Event()

    async def send_json(self, message: object) -> None:
        await super().send_json(message)
        if isinstance(message, dict) and message.get("type") == "cancel":
            self.cancel_started.set()
            await self.release_cancel.wait()


class FailingInitialSendSocket(FakeSocket):
    async def send_json(self, message: object) -> None:
        if isinstance(message, dict) and message.get("type") == "invoke":
            raise RuntimeError("connection lost")
        await super().send_json(message)


class BlockingInitialSendSocket(FakeSocket):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_json(self, message: object) -> None:
        if isinstance(message, dict) and message.get("type") == "invoke":
            self.started.set()
            await self.release.wait()
        await super().send_json(message)


class ConcurrentWriteSocket(FakeSocket):
    def __init__(self) -> None:
        super().__init__()
        self.active_writes = 0
        self.max_active_writes = 0

    async def send_json(self, message: object) -> None:
        self.active_writes += 1
        self.max_active_writes = max(self.max_active_writes, self.active_writes)
        await asyncio.sleep(0)
        await super().send_json(message)
        self.active_writes -= 1


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def provider_result(value: dict[str, object] | None = None) -> ProviderToolResult:
    return ProviderToolResult(content=[], structuredContent=value or {})


def register(registry: RelayRegistry, socket: FakeSocket) -> None:
    run(
        registry.register(
            socket,
            Register(version=1, type="register", device_id="one"),
        )
    )


def declare_ping(registry: RelayRegistry, socket: FakeSocket) -> None:
    run(
        registry.set_capabilities(
            socket,
            Capabilities(version=1, type="capabilities", tools=["system.ping"]),
        )
    )


def ping(request_id: str) -> InvokeMessage:
    return InvokeMessage(
        version=2,
        type="invoke",
        request_id=request_id,
        tool_name="system.ping",
        arguments={},
    )


def terminal(request_id: str, command_id: str = "pwd") -> InvokeMessage:
    return InvokeMessage(
        version=2,
        type="invoke",
        request_id=request_id,
        tool_name="terminal.exec",
        arguments={"command_id": command_id},
    )


def test_invoke_signature_accepts_only_a_typed_message() -> None:
    signature = inspect.signature(RelayRegistry.invoke)
    assert list(signature.parameters) == [
        "self",
        "device_id",
        "message",
        "timeout_seconds",
    ]
    assert signature.parameters["message"].annotation == "InvokeMessage"


def test_registry_serializes_generic_v2_and_returns_provider_result() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(device_id="one", agent_token="agent-token")
        socket = FakeSocket()
        await registry.register(socket, Register(version=1, type="register", device_id="one"))
        await registry.set_capabilities(
            socket,
            Capabilities(version=1, type="capabilities", tools=["system.ping"]),
        )
        pending = asyncio.create_task(
            registry.invoke(
                "one",
                InvokeMessage(
                    version=2,
                    type="invoke",
                    request_id="generic",
                    tool_name="system.ping",
                    arguments={},
                ),
                1,
            )
        )
        await asyncio.sleep(0)
        assert socket.messages[-1] == {
            "version": 2,
            "type": "invoke",
            "request_id": "generic",
            "tool_name": "system.ping",
            "arguments": {},
        }
        expected = ProviderToolResult(content=[{"type": "text", "text": "ok"}])
        await registry.handle_result(
            AgentResult(version=2, type="result", request_id="generic", result=expected)
        )
        assert await pending == expected

    asyncio.run(scenario())


def test_status_snapshot_is_safe_and_offline() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")

    snapshot = run(registry.status_snapshot())

    assert snapshot.device_id == "one"
    assert snapshot.connected is False
    assert snapshot.capabilities == ()
    assert snapshot.invocation_state == "idle"
    assert snapshot.progress is None
    assert snapshot.heartbeat_age_seconds is None


def test_status_snapshot_atomically_copies_connected_state() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    socket = FakeSocket()
    register(registry, socket)
    run(
        registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=["terminal.exec", "system.ping"],
            ),
        )
    )

    snapshot = run(registry.status_snapshot())

    assert snapshot.device_id == "one"
    assert snapshot.connected is True
    assert snapshot.capabilities == ("system.ping", "terminal.exec")
    assert snapshot.invocation_state == "idle"
    assert snapshot.progress is None
    assert snapshot.heartbeat_age_seconds is not None
    assert snapshot.heartbeat_age_seconds >= 0
    assert set(vars(snapshot)) == {
        "device_id",
        "connected",
        "capabilities",
        "invocation_state",
        "progress",
        "heartbeat_age_seconds",
    }


def test_status_snapshot_captures_busy_progress_under_registry_lock() -> None:
    async def scenario() -> None:
        registry = RelayRegistry(device_id="one", agent_token="agent-token")
        socket = FakeSocket()
        await registry.register(
            socket,
            Register(version=1, type="register", device_id="one"),
        )
        await registry.set_capabilities(
            socket,
            Capabilities(version=1, type="capabilities", tools=["system.ping"]),
        )
        pending = asyncio.create_task(
            registry.invoke("one", ping("snapshot"), 1)
        )
        await asyncio.sleep(0)
        await registry.handle_progress(
            Progress(
                version=2,
                type="progress",
                request_id="snapshot",
                progress=40,
            )
        )

        snapshot = await registry.status_snapshot()

        assert snapshot.invocation_state == "busy"
        assert snapshot.progress == 40
        await registry.handle_result(
            AgentResult(version=2, type="result", request_id="snapshot", result=provider_result())
        )
        assert await pending == provider_result()

    run(scenario())


def test_unknown_device_and_second_connection_are_rejected() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    socket = FakeSocket()
    with pytest.raises(AuthenticationError):
        run(
            registry.register(
                socket,
                Register(version=1, type="register", device_id="other"),
            )
        )
    register(registry, socket)
    with pytest.raises(DeviceAlreadyConnectedError):
        register(registry, FakeSocket())



def test_registry_starts_offline_without_a_preconfigured_agent_identity() -> None:
    try:
        registry = RelayRegistry(agent_token="agent-token")
    except TypeError as exc:
        pytest.fail(f"server-side Agent identity must be dynamic: {exc}")
        raise AssertionError("unreachable")

    snapshot = run(registry.status_snapshot())

    assert snapshot.device_id is None
    assert snapshot.connected is False
    assert snapshot.capabilities == ()


def test_registry_binds_first_identity_and_rejects_a_different_identity() -> None:
    try:
        registry = RelayRegistry(agent_token="agent-token")
    except TypeError as exc:
        pytest.fail(f"server-side Agent identity must be dynamic: {exc}")
        raise AssertionError("unreachable")
    first_socket = FakeSocket()
    first = Register.model_construct(version=1, type="register", device_id="one")
    run(registry.register(first_socket, first))
    assert run(registry.status_snapshot()).device_id == "one"
    run(registry.disconnect(first_socket))

    second_socket = FakeSocket()
    second = Register.model_construct(version=1, type="register", device_id="two")
    with pytest.raises(AuthenticationError):
        run(registry.register(second_socket, second))


def test_every_tool_requires_a_declared_capability() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    socket = FakeSocket()
    register(registry, socket)

    with pytest.raises(UnsupportedToolError, match="system.ping"):
        run(registry.invoke("one", ping("ping"), 1))
    with pytest.raises(UnsupportedToolError, match="terminal.exec"):
        run(registry.invoke("one", terminal("a"), 1))

    run(
        registry.set_capabilities(
            socket,
            Capabilities(
                version=1,
                type="capabilities",
                tools=["system.ping", "terminal.exec"],
            ),
        )
    )

    async def scenario() -> None:
        pending = asyncio.create_task(
            registry.invoke("one", terminal("a"), 1)
        )
        await asyncio.sleep(0)
        assert socket.messages[-1] == {
            "version": 2,
            "type": "invoke",
            "request_id": "a",
            "tool_name": "terminal.exec",
            "arguments": {"command_id": "pwd"},
        }
        await registry.handle_result(
            AgentResult(version=2, type="result", request_id="a", result=provider_result())
        )
        assert await pending == provider_result()

    run(scenario())


def test_offline_busy_correlated_and_unknown_results() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    with pytest.raises(DeviceOfflineError):
        run(registry.invoke("one", ping("a"), 1))
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        first = asyncio.create_task(registry.invoke("one", ping("a"), 1))
        await asyncio.sleep(0)
        with pytest.raises(DeviceBusyError):
            await registry.invoke("one", ping("b"), 1)
        await registry.handle_result(
            AgentResult(version=2, type="result", request_id="a", result=provider_result({"ok": True}))
        )
        assert await first == provider_result({"ok": True})
        with pytest.raises(UnknownRequestError):
            await registry.handle_result(
                AgentResult(version=2, type="result", request_id="missing", result=provider_result())
            )

    run(scenario())


def test_duplicate_timeout_cancellation_and_disconnect_always_clean_up() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        task = asyncio.create_task(
            registry.invoke("one", ping("a"), 0.01)
        )
        await asyncio.sleep(0)
        with pytest.raises(DuplicateRequestError):
            await registry.invoke("one", ping("a"), 1)
        with pytest.raises(TimeoutError):
            await task
        assert registry.pending_count == 0
        assert socket.messages[-1] == {
            "version": 2,
            "type": "cancel",
            "request_id": "a",
            "reason": "control request cancelled or timed out",
        }

        waiting = asyncio.create_task(
            registry.invoke("one", ping("b"), 1)
        )
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert registry.pending_count == 0
        assert socket.messages[-1] == {
            "version": 2,
            "type": "cancel",
            "request_id": "b",
            "reason": "control request cancelled or timed out",
        }

        disconnected = asyncio.create_task(
            registry.invoke("one", ping("c"), 1)
        )
        await asyncio.sleep(0)
        await registry.disconnect(socket)
        with pytest.raises(DeviceOfflineError):
            await disconnected
        assert registry.pending_count == 0

    run(scenario())


def test_progress_and_correlated_error_affect_only_the_in_flight_invocation() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        pending = asyncio.create_task(registry.invoke("one", ping("a"), 1))
        await asyncio.sleep(0)
        await registry.handle_progress(
            Progress(version=2, type="progress", request_id="a", progress=45, message="work")
        )
        assert registry.current_progress == 45
        with pytest.raises(UnknownRequestError):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="missing", progress=100)
            )
        assert registry.current_progress == 45
        await registry.handle_error(
            AgentError(
                version=2,
                type="error",
                request_id="a",
                error={"code": "failed", "message": "agent failed"},
            )
        )
        with pytest.raises(Exception, match="agent failed"):
            await pending
        assert registry.current_progress is None

    run(scenario())


def test_duplicate_and_late_responses_are_rejected_without_touching_new_work() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        result_waiter = asyncio.create_task(
            registry.invoke("one", ping("result"), 1)
        )
        await asyncio.sleep(0)
        result = AgentResult(
            version=2, type="result", request_id="result", result=provider_result({"ok": True})
        )
        await registry.handle_result(result)
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_result(result)
        assert await result_waiter == provider_result({"ok": True})

        error_waiter = asyncio.create_task(
            registry.invoke("one", ping("error"), 1)
        )
        await asyncio.sleep(0)
        error = AgentError(
            version=2,
            type="error",
            request_id="error",
            error={"code": "failed", "message": "agent failed"},
        )
        await registry.handle_error(error)
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_error(error)
        with pytest.raises(Exception, match="agent failed"):
            await error_waiter

        timed_out = asyncio.create_task(
            registry.invoke("one", ping("timed-out"), 0.01)
        )
        with pytest.raises(TimeoutError):
            await timed_out
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_result(
                AgentResult(
                    version=2,
                    type="result",
                    request_id="timed-out",
                    result=provider_result({"too": "late"}),
                )
            )

        cancelled = asyncio.create_task(
            registry.invoke("one", ping("cancelled"), 1)
        )
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_result(
                AgentResult(
                    version=2,
                    type="result",
                    request_id="cancelled",
                    result=provider_result({"too": "late"}),
                )
            )

        next_waiter = asyncio.create_task(
            registry.invoke("one", ping("next"), 1)
        )
        await asyncio.sleep(0)
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_result(
                AgentResult(
                    version=2,
                    type="result",
                    request_id="cancelled",
                    result=provider_result({"wrong": "invocation"}),
                )
            )
        assert registry.pending_count == 1
        await registry.handle_result(
            AgentResult(version=2, type="result", request_id="next", result=provider_result())
        )
        assert await next_waiter == provider_result()
        assert registry.pending_count == 0
        assert registry.current_progress is None

    run(scenario())


def test_progress_after_terminal_result_or_error_is_rejected() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        result_waiter = asyncio.create_task(
            registry.invoke("one", ping("result"), 1)
        )
        await asyncio.sleep(0)
        await registry.handle_result(
            AgentResult(version=2, type="result", request_id="result", result=provider_result())
        )
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="result", progress=100)
            )
        assert await result_waiter == provider_result()
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="result", progress=100)
            )

        error_waiter = asyncio.create_task(
            registry.invoke("one", ping("error"), 1)
        )
        await asyncio.sleep(0)
        await registry.handle_error(
            AgentError(
                version=2,
                type="error",
                request_id="error",
                error={"code": "failed", "message": "agent failed"},
            )
        )
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="error", progress=100)
            )
        with pytest.raises(Exception, match="agent failed"):
            await error_waiter
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="error", progress=100)
            )

    run(scenario())


def test_progress_during_and_after_timeout_or_cancellation_is_rejected() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    socket = BlockingCancelSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def assert_late_progress(request_id: str) -> None:
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id=request_id, progress=100)
            )

    async def scenario() -> None:
        timed_out = asyncio.create_task(
            registry.invoke("one", ping("timed-out"), 0.01)
        )
        await socket.cancel_started.wait()
        # wait_for has already cancelled the Future, but invoke is still sending cancel.
        await assert_late_progress("timed-out")
        socket.release_cancel.set()
        with pytest.raises(TimeoutError):
            await timed_out
        await assert_late_progress("timed-out")

        socket.cancel_started = asyncio.Event()
        socket.release_cancel = asyncio.Event()
        cancelled = asyncio.create_task(
            registry.invoke("one", ping("cancelled"), 1)
        )
        await asyncio.sleep(0)
        cancelled.cancel()
        await socket.cancel_started.wait()
        # Direct HTTP cancellation is not exposed reliably by TestClient; this covers
        # the registry boundary while cancellation delivery is still in progress.
        await assert_late_progress("cancelled")
        socket.release_cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await assert_late_progress("cancelled")

    run(scenario())


def test_progress_tombstones_are_bounded_and_do_not_affect_next_invocation() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    socket = FakeSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        for index in range(registry._RECENTLY_COMPLETED_LIMIT + 1):
            registry._remember_completed(f"old-{index}")
        assert len(registry._recently_completed) == registry._RECENTLY_COMPLETED_LIMIT
        assert "old-0" not in registry._recently_completed

        next_waiter = asyncio.create_task(
            registry.invoke("one", ping("next"), 1)
        )
        await asyncio.sleep(0)
        with pytest.raises(LateResponseError, match="late or duplicate"):
            await registry.handle_progress(
                Progress(version=2, type="progress", request_id="old-1", progress=100)
            )
        assert registry.current_progress is None
        await registry.handle_progress(
            Progress(version=2, type="progress", request_id="next", progress=50)
        )
        assert registry.current_progress == 50
        await registry.handle_result(
            AgentResult(version=2, type="result", request_id="next", result=provider_result())
        )
        assert await next_waiter == provider_result()

    run(scenario())


def test_initial_send_failure_cleans_pending_and_translates_to_offline() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    socket = FailingInitialSendSocket()
    register(registry, socket)
    declare_ping(registry, socket)

    async def scenario() -> None:
        with pytest.raises(DeviceOfflineError):
            await registry.invoke("one", ping("a"), 1)
        assert registry.pending_count == 0
        assert "a" in registry._recently_completed

    run(scenario())


def test_initial_send_disconnect_and_all_writes_are_serialized() -> None:
    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    blocking = BlockingInitialSendSocket()
    register(registry, blocking)
    declare_ping(registry, blocking)

    async def disconnect_scenario() -> None:
        pending = asyncio.create_task(
            registry.invoke("one", ping("a"), 1)
        )
        await blocking.started.wait()
        await registry.disconnect(blocking)
        blocking.release.set()
        with pytest.raises(DeviceOfflineError):
            await pending
        assert registry.pending_count == 0

    run(disconnect_scenario())

    registry = RelayRegistry(device_id="one", agent_token="agent-token")
    concurrent = ConcurrentWriteSocket()
    register(registry, concurrent)

    async def serialization_scenario() -> None:
        await asyncio.gather(
            registry.send(concurrent, {"type": "one"}),
            registry.send(concurrent, {"type": "two"}),
        )
        assert concurrent.max_active_writes == 1

    run(serialization_scenario())
