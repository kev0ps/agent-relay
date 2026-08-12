"""Owned local CUA provider over a bounded MCP stdio transport."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ..diagnostics import debug as _debug_log
from ..json_bounds import JsonValue
from ..output_models import ProviderTextContent, ProviderToolResult
from ..protocol import InvokeMessage
from ..provider_tools import ProviderToolDescriptor
from ..providers.base import (
    ProviderConnectionError,
    ProviderTimeoutError,
    ProviderToolError,
)
from ..providers.mcp_client import McpProviderToolClient, McpTransport

MAX_MCP_FRAME_BYTES = 256 * 1024
MAX_DRIVER_DIAGNOSTIC_LINES = 64
MAX_DRIVER_DIAGNOSTIC_LINE_BYTES = 4096
MAX_COMPUTER_APP_LENGTH = 128
MAX_COMPUTER_WINDOW_TITLE_LENGTH = 256
WINDOWS_CUA_DRIVER_PIPE = r"\\.\pipe\cua-driver"
_SCOPED_CUA_TOOLS = frozenset({"list_windows", "get_window_state", "click", "type_text"})
_CUA_ACTION_RESULT_KEYS = frozenset(
    {"path", "verified", "effect", "characters", "escalation", "scope"}
)


class _ProcessWithReturncode(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...


class _AsyncManagedProcess(_ProcessWithReturncode, Protocol):
    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class _AsyncPopenProcess:
    """Adapt a synchronous Windows daemon process to the async cleanup API."""

    def __init__(self, process: subprocess.Popen[Any]) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    async def wait(self) -> int:
        return await asyncio.to_thread(self._process.wait)


def windows_daemon_pipe_ready() -> bool:
    """Check the default Windows CUA daemon pipe without consuming it."""
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wait_named_pipe = kernel32.WaitNamedPipeW
        wait_named_pipe.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        wait_named_pipe.restype = ctypes.c_int
        return bool(wait_named_pipe(WINDOWS_CUA_DRIVER_PIPE, 1))
    except (AttributeError, OSError):
        return False


async def _wait_for_windows_daemon_ready(
    process: _ProcessWithReturncode,
    timeout_seconds: float,
    *,
    pipe_ready: Callable[[], bool] = windows_daemon_pipe_ready,
) -> None:
    """Wait for the daemon pipe instead of racing the MCP proxy startup."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        if process.returncode is not None:
            raise ValueError
        if pipe_ready():
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
        await asyncio.sleep(0.05)


def _driver_stderr_line_category(line: bytes) -> str | None:
    """Classify one driver stderr line without returning its contents."""
    text = line[:MAX_DRIVER_DIAGNOSTIC_LINE_BYTES].decode(
        "utf-8", errors="replace"
    ).casefold()
    if not text.strip():
        return None
    if "named pipe" in text or "broken pipe" in text or "pipe" in text:
        return "named-pipe"
    if "ui automation" in text or "uia" in text or "accessibility" in text:
        return "ui-automation"
    if "permission" in text or "access is denied" in text:
        return "permission"
    if "configuration" in text or "invalid agent" in text:
        return "configuration"
    if "session" in text or "desktop" in text:
        return "desktop-session"
    if "mcp" in text or "json-rpc" in text:
        return "mcp"
    if "daemon" in text:
        return "daemon"
    return "driver-error"


def _driver_stderr_category(categories: set[str], saw_output: bool) -> str | None:
    """Select one closed driver diagnostic category by stable priority."""
    for category in (
        "named-pipe",
        "ui-automation",
        "permission",
        "configuration",
        "desktop-session",
        "mcp",
        "daemon",
    ):
        if category in categories:
            return category
    return "driver-error" if saw_output else None

_SAFE_ENV = {
    "APPDATA",
    "COMSPEC",
    "DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "CUA_DRIVER_TELEMETRY_HOME",
    "CUA_DRIVER_RS_HOME",
    "CUA_E2E_BROWSER_NO_SANDBOX",
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
        "windows-daemon-ready",
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
        error_text = str(error)
        if error_text == "provider request failed":
            return "provider-request-error"
        if error_text == "invalid provider tool inventory":
            return "provider-invalid-inventory"
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


def get_cua_driver_path() -> Path:
    """Resolve the bundled driver owned by the installed cua-driver package."""
    try:
        import cua_driver

        raw_path = cua_driver.get_binary_path()
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        raise ValueError("cua-driver is unavailable") from None
    if not isinstance(raw_path, (str, Path)):
        raise ValueError("cua-driver returned an invalid binary path")
    return validate_driver_executable(Path(raw_path))


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
    """One persistent CUA provider with a runtime-discovered MCP inventory."""

    provider_name = "cua"
    requires_catalog = True
    # CUA is intentionally dynamic.  The catalog returned by the driver is
    # authoritative and can grow without a Relay release.
    tools: frozenset[str] = frozenset()

    def __init__(
        self,
        app_name: str | None = None,
        window_title: str | None = None,
        *,
        startup_timeout_seconds: float = 15,
        action_timeout_seconds: float = 10,
        shutdown_timeout_seconds: float = 3,
        max_elements: int = 300,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._path = get_cua_driver_path()
        if (app_name is None) != (window_title is None):
            raise ValueError("invalid computer configuration")
        if app_name is not None and (
            not app_name or len(app_name) > MAX_COMPUTER_APP_LENGTH
        ):
            raise ValueError("invalid computer configuration")
        if window_title is not None and (
            not window_title or len(window_title) > MAX_COMPUTER_WINDOW_TITLE_LENGTH
        ):
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
        self._env = safe_driver_environment(
            dict(os.environ if environ is None else environ)
        )
        self._windows = os.name == "nt"
        self._process: asyncio.subprocess.Process | None = None
        self._daemon: _AsyncManagedProcess | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._counter = 0
        self._write_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._reset_lock = asyncio.Lock()
        self._scope_lock = asyncio.Lock()
        self._unavailable = asyncio.Event()
        self._client: McpProviderToolClient | None = None
        self._transport = _ComputerMcpTransport(self)
        self._closing = False
        self._startup_phase: str | None = None
        self._driver_diagnostic_category: str | None = None
        self._pid: int | None = None
        self._window_id: int | None = None
        # Browser processes are admitted only after an explicit CUA launch or
        # isolated browser preparation call. This lets the same bounded
        # list_windows tool locate the driver's new browser window without
        # widening the configured desktop app/window scope.
        self._browser_pids: set[int] = set()
        self._element_tokens: frozenset[str] = frozenset()
        self._used_actions: set[tuple[str, str]] = set()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._process is not None and self._client is not None:
                return
            self._unavailable = asyncio.Event()
            self._closing = False
            self._driver_diagnostic_category = None
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
                await self._finish_driver_diagnostics()
                driver_category = self._driver_diagnostic_category
                driver_hint = f" driver={driver_category}" if driver_category else ""
                _debug_log(
                    "computer startup failed: "
                    f"phase={startup_phase or 'unknown'} "
                    f"category={_startup_failure_category(error)}"
                    f"{driver_hint}"
                )
                await self._reset()
                raise ComputerUnavailableError(startup_phase) from None
            finally:
                self._startup_phase = None
                if phase_reporter is not None:
                    phase_reporter.cancel()
                    await asyncio.gather(phase_reporter, return_exceptions=True)

    async def _finish_driver_diagnostics(self) -> None:
        task = self._stderr_task
        if task is None or task is asyncio.current_task():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), 0.5)
        except Exception:
            pass

    async def _read_driver_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        categories: set[str] = set()
        saw_output = False
        try:
            for _ in range(MAX_DRIVER_DIAGNOSTIC_LINES):
                line = await process.stderr.readline()
                if not line:
                    break
                category = _driver_stderr_line_category(line)
                if category is not None:
                    saw_output = True
                    categories.add(category)
            # Continue draining after the retained diagnostic bound so a
            # noisy child cannot block its stdout protocol pipe.
            while await process.stderr.readline():
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            self._driver_diagnostic_category = _driver_stderr_category(
                categories, saw_output
            ) or "driver-error"
            return
        self._driver_diagnostic_category = _driver_stderr_category(
            categories, saw_output
        )

    async def _report_startup_phase(self) -> None:
        observed: str | None = None
        while True:
            phase = self._startup_phase
            if phase != observed:
                _debug_log(f"computer startup phase: {phase or 'unknown'}")
                observed = phase
            await asyncio.sleep(0.25)

    async def _start_owned(self) -> None:
        if self._windows:
            # The Windows daemon owns the interactive desktop session; the
            # MCP client must use its proxy path instead of Session 0.
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
        self._process = await asyncio.create_subprocess_exec(
            str(self._path),
            *driver_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            limit=MAX_MCP_FRAME_BYTES + 1,
        )
        self._reader_task = asyncio.create_task(self._read_responses())
        self._stderr_task = asyncio.create_task(self._read_driver_stderr())
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
        )
        await self._client.list_tools()

    async def _start_windows_daemon(self) -> None:
        """Start the Windows UIA daemon used by the MCP proxy path."""
        self._startup_phase = "windows-daemon-spawn"
        self._daemon = _AsyncPopenProcess(
            subprocess.Popen(
                [
                    str(self._path),
                    "serve",
                    "--no-overlay",
                    "--no-permissions-gate",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._env,
            )
        )
        await asyncio.sleep(0)
        if self._daemon.returncode is not None:
            raise ValueError
        self._startup_phase = "windows-daemon-ready"
        await _wait_for_windows_daemon_ready(self._daemon, self._startup_timeout)

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
            _debug_log(
                "computer privacy command failed: "
                f"phase={self._startup_phase or 'unknown'} "
                f"exit={process.returncode} "
                f"driver_home={'present' if self._env.get('CUA_DRIVER_RS_HOME') else 'absent'}"
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
        if tool_name in _SCOPED_CUA_TOOLS:
            async with self._scope_lock:
                return await self._call_scoped_tool(client, tool_name, arguments)
        try:
            result = await client.call_tool(tool_name, arguments)
            if tool_name in {"launch_app", "browser_prepare"}:
                self._record_browser_processes(tool_name, result)
            return result
        except asyncio.CancelledError:
            await asyncio.shield(self._reset())
            raise
        except (ProviderConnectionError, ProviderTimeoutError):
            await self._reset()
            raise

    async def _call_scoped_tool(
        self,
        client: McpProviderToolClient,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> ProviderToolResult:
        try:
            scoped_arguments = self._scope_arguments(tool_name, arguments)
        except (TypeError, ValueError):
            _debug_cua_scope_rejection(
                f"{tool_name}-arguments", tool_name=tool_name
            )
            return _safe_cua_rejection()
        try:
            result = await client.call_tool(tool_name, scoped_arguments)
        except asyncio.CancelledError:
            await asyncio.shield(self._reset())
            raise
        except (ProviderConnectionError, ProviderTimeoutError):
            await self._reset()
            raise
        if result.is_error and tool_name in {"click", "type_text"}:
            if _is_background_unavailable(result):
                foreground_arguments = dict(scoped_arguments)
                foreground_arguments["delivery_mode"] = "foreground"
                result = await client.call_tool(tool_name, foreground_arguments)
        if result.is_error:
            _debug_cua_scope_rejection(
                f"{tool_name}-provider-error", tool_name=tool_name
            )
            return _safe_cua_rejection()
        try:
            if tool_name == "list_windows":
                browser_pid = scoped_arguments.get("pid")
                return self._scope_window_list(
                    result,
                    browser_pid=browser_pid if type(browser_pid) is int else None,
                )
            if tool_name == "get_window_state":
                return self._scope_window_state(result)
            return self._scope_action_result(result)
        except (TypeError, ValueError):
            _debug_cua_scope_rejection(
                f"{tool_name}-result", tool_name=tool_name
            )
            return _safe_cua_rejection()

    def _scope_arguments(
        self,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> dict[str, JsonValue]:
        scoped = dict(arguments)
        if tool_name == "list_windows":
            if set(scoped) - {"on_screen_only", "pid"}:
                raise ValueError
            browser_pid = scoped.get("pid")
            if browser_pid is not None and (
                type(browser_pid) is not int
                or browser_pid <= 0
                or browser_pid not in self._browser_pids
            ):
                raise ValueError
            # The Linux X11 driver reports MapState inconsistently for mapped
            # Chromium windows under Xvfb/Openbox. Exact configured app/title
            # matching below still provides the target scope; keep the native
            # visibility filter for Windows where the daemon reports it reliably.
            scoped["on_screen_only"] = self._windows if browser_pid is None else False
            return scoped
        if self._pid is None or self._window_id is None:
            raise ValueError
        if scoped.get("pid") != self._pid or scoped.get("window_id") != self._window_id:
            raise ValueError
        if tool_name == "get_window_state":
            if set(scoped) - {
                "pid",
                "window_id",
                "include_screenshot",
                "max_elements",
            }:
                raise ValueError
            if scoped.get("include_screenshot", False) is not False:
                raise ValueError
            maximum = scoped.get("max_elements", self._max_elements)
            if (
                type(maximum) is not int
                or maximum <= 0
                or maximum > self._max_elements
            ):
                raise ValueError
            scoped["include_screenshot"] = False
            scoped["max_elements"] = maximum
            return scoped
        allowed = {"pid", "window_id", "element_token", "delivery_mode"}
        required = {"pid", "window_id", "element_token"}
        if tool_name == "type_text":
            allowed.add("text")
            required.add("text")
        if set(scoped) - allowed or not required.issubset(scoped):
            raise ValueError
        delivery_mode = scoped.get("delivery_mode")
        if delivery_mode is not None and delivery_mode not in {"background", "foreground"}:
            raise ValueError
        token = scoped.get("element_token")
        if type(token) is not str or token not in self._element_tokens:
            raise ValueError
        action_key = (tool_name, token)
        if action_key in self._used_actions:
            raise ValueError
        self._used_actions.add(action_key)
        return scoped

    def _scope_window_list(
        self,
        result: ProviderToolResult,
        *,
        browser_pid: int | None = None,
    ) -> ProviderToolResult:
        structured = result.structured_content
        if not isinstance(structured, dict):
            _debug_cua_scope_rejection("structured-content", tool_name="list_windows")
            raise ValueError
        windows = structured.get("windows")
        if not isinstance(windows, list):
            _debug_cua_scope_rejection("windows-field", tool_name="list_windows")
            raise ValueError
        matches = [
            window
            for window in windows
            if isinstance(window, dict)
            and (
                (
                    window.get("pid") == browser_pid
                    if browser_pid is not None
                    else self._app_matches(window.get("app_name"))
                    and window.get("title") == self._title
                )
            )
        ]
        if len(matches) != 1:
            app_matches = (
                sum(
                    isinstance(window, dict)
                    and self._app_matches(window.get("app_name"))
                    for window in windows
                )
                if browser_pid is None
                else sum(
                    isinstance(window, dict) and window.get("pid") == browser_pid
                    for window in windows
                )
            )
            title_matches = (
                sum(
                    isinstance(window, dict) and window.get("title") == self._title
                    for window in windows
                )
                if browser_pid is None
                else 0
            )
            _debug_cua_scope_rejection(
                "identity",
                tool_name="list_windows",
                windows=len(windows),
                app_matches=app_matches,
                title_matches=title_matches,
                identity_matches=len(matches),
            )
            raise ValueError
        window = matches[0]
        pid = window.get("pid")
        window_id = window.get("window_id")
        bounds = window.get("bounds")
        if (
            type(pid) is not int
            or pid <= 0
            or type(window_id) is not int
            or window_id <= 0
            or type(window.get("is_on_screen")) is not bool
            or not isinstance(bounds, dict)
        ):
            _debug_cua_scope_rejection("identity-fields", tool_name="list_windows")
            raise ValueError
        safe_bounds: dict[str, JsonValue] = {}
        for key in ("x", "y", "width", "height"):
            value = bounds.get(key)
            if type(value) is not int:
                _debug_cua_scope_rejection("bounds-fields", tool_name="list_windows")
                raise ValueError
            safe_bounds[key] = value
        if browser_pid is not None:
            app_name = window.get("app_name")
            title = window.get("title")
            if (
                type(app_name) is not str
                or not app_name
                or len(app_name) > MAX_COMPUTER_APP_LENGTH
                or type(title) is not str
                or len(title) > MAX_COMPUTER_WINDOW_TITLE_LENGTH
            ):
                _debug_cua_scope_rejection(
                    "browser-window-identity", tool_name="list_windows"
                )
                raise ValueError
        else:
            app_name = self._app
            title = self._title
        self._pid = pid
        self._window_id = window_id
        self._element_tokens = frozenset()
        self._used_actions.clear()
        safe_window: dict[str, JsonValue] = {
            "pid": pid,
            "window_id": window_id,
            "app_name": app_name,
            "title": title,
            "is_on_screen": window["is_on_screen"],
            "bounds": safe_bounds,
        }
        return ProviderToolResult(
            content=[],
            structuredContent={"windows": [safe_window]},
            isError=False,
        )

    def _scope_window_state(self, result: ProviderToolResult) -> ProviderToolResult:
        structured = result.structured_content
        if not isinstance(structured, dict):
            raise ValueError
        if structured.get("pid") != self._pid or structured.get("window_id") != self._window_id:
            raise ValueError
        snapshot_id = structured.get("snapshot_id")
        elements = structured.get("elements")
        if (
            type(snapshot_id) is not str
            or not snapshot_id
            or len(snapshot_id) > 256
            or not isinstance(elements, list)
            or not 1 <= len(elements) <= self._max_elements
        ):
            raise ValueError
        safe_elements: list[JsonValue] = []
        tokens: set[str] = set()
        field_role_count = 0
        button_role_count = 0
        name_label_count = 0
        apply_label_count = 0
        for public_index, item in enumerate(elements):
            if not isinstance(item, dict):
                _debug_cua_scope_rejection(
                    "element-not-object",
                    tool_name="get_window_state",
                    element=public_index,
                )
                raise ValueError
            index = item.get("element_index")
            role = item.get("role")
            token = item.get("element_token")
            label = item.get("label", item.get("name", ""))
            invalid_element = (
                type(index) is not int
                or index < 0
                or type(role) is not str
                or not role
                or len(role) > 128
                or type(token) is not str
                or not token
                or len(token) > 256
                or token in tokens
                or type(label) is not str
                or len(label) > 512
            )
            if invalid_element:
                reasons: list[str] = []
                if type(index) is not int:
                    reasons.append("index-type")
                elif index < 0:
                    reasons.append("index-negative")
                if type(role) is not str:
                    reasons.append("role-type")
                elif not role:
                    reasons.append("role-empty")
                elif len(role) > 128:
                    reasons.append("role-too-long")
                if type(token) is not str:
                    reasons.append("token-type")
                elif not token:
                    reasons.append("token-empty")
                elif len(token) > 256:
                    reasons.append("token-too-long")
                elif token in tokens:
                    reasons.append("token-duplicate")
                if type(label) is not str:
                    reasons.append("label-type")
                elif len(label) > 512:
                    reasons.append("label-too-long")
                _debug_cua_scope_rejection(
                    "element-invalid-" + ("+".join(reasons) or "unknown"),
                    tool_name="get_window_state",
                    element=public_index,
                )
                raise ValueError
            normalized_role = role.casefold()
            normalized_label = label.casefold()
            if normalized_role in {"textbox", "entry", "text", "edit", "editable"}:
                field_role_count += 1
            if normalized_role in {"button", "push button"}:
                button_role_count += 1
            if normalized_label == "name":
                name_label_count += 1
            if normalized_label == "apply":
                apply_label_count += 1
            safe_item: dict[str, JsonValue] = {
                "element_index": public_index,
                "role": role,
                "element_token": token,
                "label": label,
            }
            value = item.get("value")
            if isinstance(value, str) and len(value) <= 2048:
                safe_item["value"] = value
            safe_elements.append(safe_item)
            tokens.add(token)
        _debug_cua_scope_rejection(
            "window-state-shape",
            tool_name="get_window_state",
            elements=len(safe_elements),
            field_roles=field_role_count,
            button_roles=button_role_count,
            name_labels=name_label_count,
            apply_labels=apply_label_count,
        )
        self._element_tokens = frozenset(tokens)
        self._used_actions.clear()
        return ProviderToolResult(
            content=[],
            structuredContent={
                "pid": self._pid,
                "window_id": self._window_id,
                "snapshot_id": snapshot_id,
                "elements": safe_elements,
            },
            isError=False,
        )

    def _scope_action_result(self, result: ProviderToolResult) -> ProviderToolResult:
        structured = result.structured_content
        if not isinstance(structured, dict):
            raise ValueError
        if "path" not in structured:
            return self._normalize_native_action_result(structured)
        safe = {
            key: value
            for key, value in structured.items()
            if key in _CUA_ACTION_RESULT_KEYS
        }
        if (
            type(safe.get("path")) is not str
            or not safe["path"]
            or len(safe["path"]) > 128
            or type(safe.get("verified")) is not bool
            or safe.get("effect")
            not in {"confirmed", "unverifiable", "suspected_noop"}
        ):
            raise ValueError
        characters = safe.get("characters")
        if characters is not None and (
            type(characters) is not int or characters < 0
        ):
            raise ValueError
        return ProviderToolResult(
            content=[],
            structuredContent=safe,
            isError=False,
        )

    @staticmethod
    def _normalize_native_action_result(
        structured: Mapping[str, JsonValue],
    ) -> ProviderToolResult:
        """Map cua-driver 0.19 ActionResult to Relay's stable action envelope."""
        raw_effect = structured.get("effect")
        effect = (
            raw_effect
            if raw_effect in {"confirmed", "unverifiable", "suspected_noop"}
            else "unverifiable"
            if raw_effect == "partial"
            else None
        )
        route = structured.get("route")
        if not isinstance(route, str) or not route or len(route) > 64:
            route = "cua"
        if effect is None:
            raise ValueError
        safe: dict[str, JsonValue] = {
            "path": route,
            "verified": effect == "confirmed",
            "effect": effect,
        }
        delivery = structured.get("delivery")
        if isinstance(delivery, dict):
            mode = delivery.get("mode")
            if isinstance(mode, str) and len(mode) <= 32:
                safe["scope"] = mode
            delivered_count = delivery.get("delivered_count")
            if type(delivered_count) is int and delivered_count >= 0:
                safe["characters"] = delivered_count
        return ProviderToolResult(
            content=[],
            structuredContent=safe,
            isError=False,
        )

    def _app_matches(self, value: object) -> bool:
        if value == self._app:
            return True
        return self._windows and value == self._app + ".exe"

    def _record_browser_processes(
        self,
        tool_name: str,
        result: ProviderToolResult,
    ) -> None:
        """Remember only PIDs explicitly returned by CUA browser lifecycle calls."""
        structured = result.structured_content
        if not isinstance(structured, dict):
            return
        candidates: list[object] = []
        if tool_name == "launch_app":
            candidates.append(structured.get("pid"))
        else:
            candidates.append(structured.get("prepared_pid"))
        for candidate in candidates:
            if type(candidate) is int and 0 < candidate <= 2**31:
                self._browser_pids.add(candidate)

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
            stderr_reader = self._stderr_task
            exit_task = self._exit_task
            self._reader_task = None
            self._stderr_task = None
            self._exit_task = None
            process = self._process
            daemon = self._daemon
            self._process = None
            self._daemon = None
            self._client = None
            self._pid = None
            self._window_id = None
            self._browser_pids.clear()
            self._element_tokens = frozenset()
            self._used_actions.clear()
            for task in (reader, stderr_reader, exit_task):
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (reader, stderr_reader, exit_task)
                    if task is not None
                ),
                return_exceptions=True,
            )
            if process is not None:
                await self._kill_process(process)
            if daemon is not None:
                await self._kill_process(daemon)
            self._closing = False

    @staticmethod
    async def _kill_process(process: _AsyncManagedProcess) -> None:
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


def _safe_cua_rejection() -> ProviderToolResult:
    return ProviderToolResult(
        content=[
            ProviderTextContent(type="text", text="computer action rejected")
        ],
        structuredContent=None,
        isError=True,
    )


def _is_background_unavailable(result: ProviderToolResult) -> bool:
    """Recognize only CUA's bounded background-delivery escalation signal."""
    structured = result.structured_content
    if isinstance(structured, dict):
        for key in ("code", "error", "reason", "status"):
            if structured.get(key) == "background_unavailable":
                return True
    for item in result.content:
        text = getattr(item, "text", None)
        if (
            isinstance(text, str)
            and text.strip().casefold() == "background_unavailable"
        ):
            return True
    return False


def _debug_cua_scope_rejection(
    reason: str,
    *,
    tool_name: str = "scope",
    **counts: int,
) -> None:
    """Emit only bounded CUA scope failure metadata in native debug mode."""
    details = " ".join(f"{key}={value}" for key, value in counts.items())
    suffix = f" {details}" if details else ""
    _debug_log(f"computer CUA {tool_name} rejected: reason={reason}{suffix}")


__all__ = [
    "ComputerCapability",
    "ComputerUnavailableError",
    "COMPUTER_STARTUP_PHASES",
    "get_cua_driver_path",
    "safe_driver_environment",
    "validate_driver_executable",
    "validate_windows_health",
    "windows_daemon_pipe_ready",
]
