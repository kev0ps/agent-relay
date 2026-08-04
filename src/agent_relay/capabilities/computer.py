"""Owned local CUA provider over a bounded MCP stdio transport."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..catalog import CUA_REFERENCE_TOOL_NAMES
from ..json_bounds import JsonValue
from ..output_models import ProviderToolResult
from ..protocol import (
    InvokeMessage,
    ToolName,
)
from ..provider_tools import ProviderToolDescriptor
from ..providers.base import (
    ProviderConnectionError,
    ProviderTimeoutError,
    ProviderToolError,
)
from ..providers.mcp_client import McpProviderToolClient, McpTransport

MAX_MCP_FRAME_BYTES = 256 * 1024
MAX_COMPUTER_APP_LENGTH = 128
MAX_COMPUTER_WINDOW_TITLE_LENGTH = 256

_SAFE_ENV = {
    "APPDATA",
    "COMSPEC",
    "DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "CUA_DRIVER_TELEMETRY_HOME",
    "CUA_DRIVER_RS_HOME",
    "CUA_DRIVER_RS_INSTALL_DIR",
    "PATH",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "LOGONSERVER",
    "OS",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "NO_AT_BRIDGE",
    "GTK_MODULES",
    "QT_ACCESSIBILITY",
    "QT_LINUX_ACCESSIBILITY_ALWAYS_ON",
    "AT_SPI_BUS_ADDRESS",
    "AT_SPI_BUS_TYPE",
    "PATHEXT",
    "PSModulePath",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PUBLIC",
    "SESSIONNAME",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
}

COMPUTER_STARTUP_PHASES = frozenset(
    {
        "privacy-environment",
        "process-spawn",
        "initialize",
        "initialize-response",
        "tools-list",
        "windows-daemon-spawn",
        "windows-privacy-skip",
    }
)


class ComputerUnavailableError(RuntimeError):
    """The owned CUA provider process failed without exposing backend details."""

    def __init__(self, startup_phase: str | None = None) -> None:
        super().__init__("computer capability unavailable")
        self.startup_phase = (
            startup_phase if startup_phase in COMPUTER_STARTUP_PHASES else None
        )


def _startup_failure_category(error: BaseException) -> str:
    """Return a closed diagnostic category without exposing exception values."""
    if isinstance(error, ComputerUnavailableError):
        return "provider-connection"
    if isinstance(error, (ProviderConnectionError, ConnectionError, EOFError)):
        return "provider-connection"
    if isinstance(error, ProviderTimeoutError):
        return "provider-timeout"
    if isinstance(error, ProviderToolError):
        return "provider-tool"
    if isinstance(error, asyncio.TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        return "os-error"
    if isinstance(error, json.JSONDecodeError):
        return "json-error"
    if isinstance(error, ValueError):
        return "value-error"
    if isinstance(error, RuntimeError):
        return "runtime-error"
    return "other"


def safe_driver_environment(
    source: dict[str, str] | os._Environ[str],
) -> dict[str, str]:
    """Return only the environment needed by the local CUA driver."""
    result = {key: value for key, value in source.items() if key in _SAFE_ENV}
    telemetry_home = result.get("CUA_DRIVER_TELEMETRY_HOME") or result.get(
        "CUA_DRIVER_RS_HOME"
    )
    if telemetry_home:
        result["CUA_DRIVER_TELEMETRY_HOME"] = telemetry_home
    result["CUA_DRIVER_INSTALL_CHANNEL"] = "python_package"
    result["CUA_DRIVER_TELEMETRY"] = "0"
    result["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
    result["CUA_TELEMETRY_ENABLED"] = "0"
    return result


def _process_creation_options(*, windows: bool | None = None) -> dict[str, Any]:
    """Return platform-safe process-group options for a driver child."""
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        }
    return {"start_new_session": True}


def validate_windows_health(payload: dict[str, Any]) -> None:
    """Validate the bounded readiness report used by the Windows driver host."""
    if (
        payload.get("schema_version") != "1"
        or payload.get("platform") != "win32"
        or payload.get("overall") not in {"ok", "degraded"}
    ):
        raise ValueError
    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise ValueError
    observed: dict[str, str] = {}
    for item in checks:
        if not isinstance(item, dict):
            raise ValueError
        name, status, message = (
            item.get("name"),
            item.get("status"),
            item.get("message"),
        )
        if (
            not isinstance(name, str)
            or not isinstance(status, str)
            or not isinstance(message, str)
            or name in observed
            or status not in {"pass", "fail", "skip"}
        ):
            raise ValueError
        observed[name] = status
    required = {
        "binary_version",
        "platform_supported",
        "session_active",
        "ax_capability",
    }
    if any(observed.get(name) != "pass" for name in required):
        raise ValueError


def validate_driver_executable(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("invalid computer configuration")
    try:
        info = path.lstat()
    except OSError:
        raise ValueError("invalid computer configuration") from None
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or not os.access(path, os.X_OK)
    ):
        raise ValueError("invalid computer configuration")
    return path


class _ComputerMcpTransport(McpTransport):
    """Translate generic provider-client operations to the owned JSON-RPC process."""

    def __init__(self, owner: "ComputerCapability") -> None:
        self._owner = owner

    async def list_tools(self, cursor: str | None = None) -> object:
        params: dict[str, JsonValue] = {}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._owner._request("tools/list", params, self._owner._action_timeout)

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue]
    ) -> object:
        return await self._owner._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            self._owner._action_timeout,
        )

    async def close(self) -> None:
        await self._owner._reset()


class ComputerCapability:
    """One persistent CUA MCP provider with no operation-specific Relay dispatch."""

    provider_name = "cua"
    requires_catalog = True
    tools: frozenset[ToolName] = frozenset(
        f"cua.{name}" for name in CUA_REFERENCE_TOOL_NAMES
    )

    def __init__(
        self,
        driver_path: Path,
        app_name: str,
        window_title: str,
        *,
        startup_timeout_seconds: float = 15,
        action_timeout_seconds: float = 10,
        shutdown_timeout_seconds: float = 3,
        max_elements: int = 300,
        environ: dict[str, str] | None = None,
        allowed_tool_names: Collection[str] | None = None,
    ) -> None:
        self._path = validate_driver_executable(driver_path)
        if not app_name or len(app_name) > MAX_COMPUTER_APP_LENGTH:
            raise ValueError("invalid computer configuration")
        if not window_title or len(window_title) > MAX_COMPUTER_WINDOW_TITLE_LENGTH:
            raise ValueError("invalid computer configuration")
        if not (
            0 < startup_timeout_seconds <= 30
            and 0 < action_timeout_seconds <= 30
            and 0 < shutdown_timeout_seconds <= 30
            and 0 < max_elements <= 1000
        ):
            raise ValueError("invalid computer configuration")
        self._app = app_name
        self._title = window_title
        self._startup_timeout = float(startup_timeout_seconds)
        self._action_timeout = float(action_timeout_seconds)
        self._shutdown_timeout = float(shutdown_timeout_seconds)
        self._max_elements = max_elements
        if allowed_tool_names is not None and any(
            not isinstance(name, str) or not name for name in allowed_tool_names
        ):
            raise ValueError("allowed_tool_names must contain non-empty strings")
        self._allowed_tool_names = (
            None if allowed_tool_names is None else frozenset(allowed_tool_names)
        )
        self._env = safe_driver_environment(
            dict(os.environ if environ is None else environ)
        )
        self._windows = os.name == "nt"
        self._process: asyncio.subprocess.Process | None = None
        self._daemon: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._counter = 0
        self._write_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._reset_lock = asyncio.Lock()
        self._unavailable = asyncio.Event()
        self._client: McpProviderToolClient | None = None
        self._transport = _ComputerMcpTransport(self)
        self._closing = False
        self._startup_phase: str | None = None

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._process is not None and self._client is not None:
                return
            self._unavailable = asyncio.Event()
            self._closing = False
            phase_reporter = (
                asyncio.create_task(self._report_startup_phase())
                if os.environ.get("RELAY_NATIVE_DEBUG") == "1"
                else None
            )
            try:
                await asyncio.wait_for(self._start_owned(), self._startup_timeout)
            except asyncio.CancelledError:
                await asyncio.shield(self._reset())
                raise
            except Exception as error:
                startup_phase = self._startup_phase
                if os.environ.get("RELAY_NATIVE_DEBUG") == "1":
                    print(
                        "computer startup failed: "
                        f"phase={startup_phase or 'unknown'} "
                        f"category={_startup_failure_category(error)}",
                        file=sys.stderr,
                        flush=True,
                    )
                await self._reset()
                raise ComputerUnavailableError(startup_phase) from None
            finally:
                self._startup_phase = None
                if phase_reporter is not None:
                    phase_reporter.cancel()
                    await asyncio.gather(phase_reporter, return_exceptions=True)

    async def _report_startup_phase(self) -> None:
        observed: str | None = None
        while True:
            phase = self._startup_phase
            if phase != observed:
                print(
                    f"computer startup phase: {phase or 'unknown'}",
                    file=sys.stderr,
                    flush=True,
                )
                observed = phase
            await asyncio.sleep(0.25)

    async def _start_owned(self) -> None:
        if self._windows:
            self._startup_phase = "windows-privacy-skip"
            await self._start_windows_daemon()
        else:
            # The isolated child environment opts out before the binary starts.
            # Do not invoke finite telemetry subcommands here: cua-driver's
            # CLI wrapper can spawn a registration worker and block on hosted
            # runners before the MCP process is ever available. The telemetry
            # home is already isolated, so deleting an inherited identity is
            # neither necessary nor safe.
            self._startup_phase = "privacy-environment"

        self._startup_phase = "process-spawn"
        driver_args = ["mcp", "--no-overlay"]
        if not self._windows:
            driver_args.append("--no-daemon-relaunch")
        self._process = await asyncio.create_subprocess_exec(
            str(self._path),
            *driver_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._env,
            **_process_creation_options(windows=self._windows),
            limit=MAX_MCP_FRAME_BYTES + 1,
        )
        self._reader_task = asyncio.create_task(self._read_responses())
        self._exit_task = asyncio.create_task(self._watch_exit())
        self._startup_phase = "initialize"
        initialized = await self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "agent-relay", "version": "1"},
            },
            self._startup_timeout,
        )
        self._startup_phase = "initialize-response"
        server_info = initialized.get("serverInfo")
        if (
            initialized.get("protocolVersion") != "2025-06-18"
            or not isinstance(initialized.get("capabilities"), dict)
            or not isinstance(server_info, dict)
            or not isinstance(server_info.get("name"), str)
            or not isinstance(server_info.get("version"), str)
        ):
            raise ValueError
        await self._notify("notifications/initialized", {})
        self._startup_phase = "tools-list"
        self._client = McpProviderToolClient(
            self._transport,
            provider_name="cua",
            risk="interaction",
            timeout_seconds=self._action_timeout,
            close_timeout_seconds=self._shutdown_timeout,
            allowed_tool_names=self._allowed_tool_names,
        )
        await self._client.list_tools()

    async def _start_windows_daemon(self) -> None:
        """Start the Windows UIA daemon required by hosted runner sessions."""
        self._daemon = await asyncio.create_subprocess_exec(
            str(self._path),
            "serve",
            "--no-overlay",
            "--no-permissions-gate",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._env,
            **_process_creation_options(windows=True),
        )
        await asyncio.sleep(0)
        if self._daemon.returncode is not None:
            raise ValueError

    async def _privacy_command(self, *args: str, capture: bool = False) -> str:
        process = await asyncio.create_subprocess_exec(
            str(self._path),
            *args,
            stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._env,
            **_process_creation_options(windows=self._windows),
        )

        async def collect() -> bytes:
            if process.stdout is None:
                stdout = b""
            else:
                stdout = await process.stdout.read(MAX_MCP_FRAME_BYTES + 1)
                if len(stdout) > MAX_MCP_FRAME_BYTES:
                    raise ValueError
            await process.wait()
            return stdout

        try:
            stdout = await asyncio.wait_for(collect(), self._startup_timeout)
        except asyncio.CancelledError:
            await asyncio.shield(self._kill_process(process))
            raise
        except Exception:
            await self._kill_process(process)
            raise
        if process.returncode != 0:
            if os.environ.get("RELAY_NATIVE_DEBUG") == "1":
                print(
                    "computer privacy command failed: "
                    f"phase={self._startup_phase or 'unknown'} "
                    f"exit={process.returncode} "
                    f"driver_home={'present' if self._env.get('CUA_DRIVER_RS_HOME') else 'absent'}",
                    file=sys.stderr,
                    flush=True,
                )
            raise ValueError
        return stdout.decode("utf-8")

    async def list_tools(self) -> Sequence[ProviderToolDescriptor]:
        client = self._require_client()
        return await client.list_tools()

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult:
        client = self._require_client()
        try:
            return await client.call_tool(tool_name, arguments)
        except asyncio.CancelledError:
            await asyncio.shield(self._reset())
            raise
        except (ProviderConnectionError, ProviderTimeoutError):
            await self._reset()
            raise

    async def invoke(self, message: InvokeMessage) -> ProviderToolResult:
        prefix = "cua."
        if not message.tool_name.startswith(prefix):
            raise ValueError("unsupported provider tool")
        return await self.call_tool(message.tool_name.removeprefix(prefix), message.arguments)

    async def wait_unavailable(self) -> None:
        client = self._client
        if client is None:
            await self._unavailable.wait()
            return
        capability_wait = asyncio.create_task(self._unavailable.wait())
        provider_wait = asyncio.create_task(client.wait_unavailable())
        try:
            await asyncio.wait(
                {capability_wait, provider_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (capability_wait, provider_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(capability_wait, provider_wait, return_exceptions=True)

    @property
    def provider_inventory_ready(self) -> bool:
        return self._client is not None and self._process is not None

    async def close(self) -> None:
        client = self._client
        if client is not None:
            try:
                await client.close()
            finally:
                self._client = None
        else:
            await self._reset()

    async def aclose(self) -> None:
        await self.close()

    def _require_client(self) -> McpProviderToolClient:
        if self._unavailable.is_set() or self._process is None or self._client is None:
            raise ComputerUnavailableError()
        return self._client

    async def _request(
        self,
        method: str,
        params: Mapping[str, JsonValue],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None:
            raise ProviderToolError("provider connection failed")
        loop = asyncio.get_running_loop()
        request_id = self._counter
        self._counter += 1
        future: asyncio.Future[object] = loop.create_future()
        self._pending[request_id] = future
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params),
        }
        try:
            async with self._write_lock:
                process.stdin.write(
                    (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                )
                await process.stdin.drain()
            result = await asyncio.wait_for(asyncio.shield(future), timeout_seconds)
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            future.cancel()
            raise
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            future.cancel()
            raise TimeoutError from None
        finally:
            self._pending.pop(request_id, None)
        if not isinstance(result, dict):
            raise ProviderToolError("invalid provider response")
        return result

    async def _notify(self, method: str, params: Mapping[str, JsonValue]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise ProviderToolError("provider connection failed")
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": dict(params),
        }
        async with self._write_lock:
            process.stdin.write(
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            )
            await process.stdin.drain()

    async def _read_responses(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise ConnectionError
                if len(line) > MAX_MCP_FRAME_BYTES:
                    raise ValueError
                try:
                    message = json.loads(line)
                except (TypeError, ValueError):
                    raise ValueError from None
                if not isinstance(message, dict):
                    raise ValueError
                if message.get("jsonrpc") != "2.0":
                    raise ValueError
                request_id = message.get("id")
                if not isinstance(request_id, int) or isinstance(request_id, bool):
                    continue
                future = self._pending.pop(request_id, None)
                if future is None or future.done():
                    continue
                if "error" in message:
                    future.set_exception(ProviderToolError("provider request failed"))
                else:
                    result = message.get("result")
                    future.set_result(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._unavailable.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ComputerUnavailableError())
            self._pending.clear()

    async def _watch_exit(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            await process.wait()
        except asyncio.CancelledError:
            raise
        finally:
            if not self._closing:
                self._unavailable.set()

    async def _reset(self) -> None:
        async with self._reset_lock:
            if self._closing and self._process is None and self._daemon is None:
                self._unavailable.set()
                return
            self._closing = True
            self._unavailable.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ComputerUnavailableError())
            self._pending.clear()
            reader = self._reader_task
            exit_task = self._exit_task
            self._reader_task = None
            self._exit_task = None
            process = self._process
            daemon = self._daemon
            self._process = None
            self._daemon = None
            self._client = None
            for task in (reader, exit_task):
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (reader, exit_task) if task is not None),
                return_exceptions=True,
            )
            if process is not None:
                await self._kill_process(process)
            if daemon is not None:
                await self._kill_process(daemon)
            self._closing = False

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                process.terminate()
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        process.kill()
                await process.wait()
            except (ProcessLookupError, OSError):
                pass


__all__ = [
    "ComputerCapability",
    "ComputerUnavailableError",
    "COMPUTER_STARTUP_PHASES",
    "safe_driver_environment",
    "validate_driver_executable",
    "validate_windows_health",
]
