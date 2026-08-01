from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest
import uvicorn

from agent_relay.agent import AgentSettings, RelayAgent
from agent_relay.server import RelaySettings, create_app


@pytest.mark.integration
def test_real_loopback_server_starts_without_a_server_side_agent_identity() -> None:
    async def scenario() -> None:
        try:
            server_settings = RelaySettings(
                mcp_token="mcp-secret",
                agent_token="agent-secret",
                bind_host="127.0.0.1",
                max_timeout_seconds=5,
            )
        except (TypeError, ValueError) as exc:
            pytest.fail(f"loopback server still requires a configured Agent ID: {exc}")
            raise AssertionError("unreachable")

        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        app = create_app(server_settings)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="critical",
                ws_max_size=server_settings.max_ws_message_bytes,
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            snapshot = await app.state.registry.status_snapshot()
            assert snapshot.device_id is None
            assert snapshot.connected is False
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=2)


    asyncio.run(scenario())


@pytest.mark.integration
def test_real_loopback_server_agent_and_runner(tmp_path: Path) -> None:
    async def scenario() -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        server_settings = RelaySettings(
            device_id="linux-test",
            agent_token="agent-secret",
            mcp_token="control-secret",
            max_timeout_seconds=5,
        )
        app = create_app(server_settings)
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=port, log_level="critical",
                ws_max_size=server_settings.max_ws_message_bytes,
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            agent = RelayAgent(
                AgentSettings(
                    server_url=f"ws://127.0.0.1:{port}/ws/agent",
                    device_id="linux-test",
                    agent_token="agent-secret",
                    workspace=tmp_path,
                    heartbeat_interval_seconds=0.02,
                )
            )
            agent_task = asyncio.create_task(agent.run())
            headers = {"Authorization": "Bearer control-secret"}
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                for _ in range(100):
                    if app.state.registry.last_heartbeat is not None:
                        break
                    await asyncio.sleep(0.01)
                ping = await client.post(
                    "/v1/devices/linux-test/invoke", headers=headers,
                    json={"tool": "system.ping"},
                )
                assert ping.status_code == 200
                assert ping.json()["result"] == {"pong": True}
                for command_id in ("pwd", "python_version"):
                    response = await client.post(
                        "/v1/devices/linux-test/invoke", headers=headers,
                        json={"tool": "terminal.exec", "command_id": command_id},
                    )
                    assert response.status_code == 200
                    result = response.json()["result"]
                    assert result["command_id"] == command_id
                    assert result["exit_code"] == 0
                    assert result["timed_out"] is False
                    assert result["stdout_truncated"] is False
                    assert result["stderr_truncated"] is False
                    if command_id == "pwd":
                        assert result["stdout"].strip() == str(tmp_path.resolve())
                    else:
                        assert f"Python {sys.version_info.major}.{sys.version_info.minor}" in result["stdout"]
                forbidden = await client.post(
                    "/v1/devices/linux-test/invoke", headers=headers,
                    json={"tool": "terminal.exec", "command_id": "arbitrary"},
                )
                assert forbidden.status_code == 422
            agent.stop()
            await asyncio.wait_for(agent_task, timeout=2)
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=2)

    asyncio.run(scenario())
