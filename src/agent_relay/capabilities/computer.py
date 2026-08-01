"""Constrained local Computer Use capability over cua-driver MCP stdio."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..protocol import (
    MAX_COMPUTER_APP_LENGTH,
    MAX_COMPUTER_ELEMENT_VALUE_LENGTH,
    MAX_COMPUTER_ELEMENTS,
    MAX_COMPUTER_NAME_LENGTH,
    MAX_COMPUTER_ROLE_LENGTH,
    MAX_COMPUTER_WINDOW_TITLE_LENGTH,
    ComputerCaptureInvoke,
    ComputerClickInvoke,
    ComputerTypeInvoke,
    InvokeMessage,
    ToolName,
)

MAX_MCP_FRAME_BYTES = 256 * 1024
_SAFE_ENV = {
    "APPDATA",
    "COMSPEC",
    "DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
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
_TOOL_FIELDS = {
    "start_session": {"session"},
    "list_windows": {"on_screen_only", "pid"},
    "get_window_state": {
        "capture_mode",
        "include_screenshot",
        "max_depth",
        "max_elements",
        "pid",
        "query",
        "screenshot_out_file",
        "session",
        "window_id",
    },
    "click": {
        "button",
        "count",
        "cursor_id",
        "delivery_mode",
        "element_index",
        "element_token",
        "from_zoom",
        "pid",
        "session",
        "window_id",
        "x",
        "y",
    },
    "type_text": {
        "delivery_mode",
        "element_index",
        "element_token",
        "pid",
        "session",
        "text",
        "window_id",
        "x",
        "y",
    },
    "end_session": {"session"},
}
_TOOL_REQUIRED = {
    "start_session": {"session"},
    "list_windows": set(),
    "get_window_state": {"pid", "window_id"},
    "click": set(),
    "type_text": {"pid", "text"},
    "end_session": {"session"},
}
_TOOL_ADDITIONAL_PROPERTIES = {"start_session": True, "end_session": True}
_WINDOWS_TOOL_FIELDS = {
    "start_session": {"session"},
    "list_windows": {"on_screen_only", "pid"},
    "get_window_state": {
        "capture_mode",
        "include_screenshot",
        "max_depth",
        "max_elements",
        "pid",
        "query",
        "screenshot_out_file",
        "session",
        "window_id",
    },
    "click": {
        "action",
        "button",
        "count",
        "cursor_id",
        "debug_image_out",
        "delivery_mode",
        "element_index",
        "element_token",
        "from_zoom",
        "modifier",
        "pid",
        "scope",
        "session",
        "window_id",
        "x",
        "y",
    },
    "type_text": {
        "delay_ms",
        "delivery_mode",
        "element_index",
        "element_token",
        "pid",
        "session",
        "text",
        "window_id",
        "x",
        "y",
    },
    "bring_to_front": {"pid", "window_id"},
    "health_report": {"include", "skip"},
    "end_session": {"session"},
}
_WINDOWS_TOOL_REQUIRED = {
    "start_session": {"session"},
    "list_windows": set(),
    "get_window_state": {"pid", "window_id"},
    "click": set(),
    "type_text": {"pid", "text"},
    "bring_to_front": {"pid"},
    "health_report": set(),
    "end_session": {"session"},
}
_WINDOWS_TOOL_ADDITIONAL_PROPERTIES = {
    "start_session": True,
    "end_session": True,
}
_EDITABLE_ROLES = {
    "entry",
    "textbox",
    "text",
    "edit",
    "edit box",
    "text field",
    "searchbox",
    "combobox",
    "spinbutton",
}
_ACTIONABLE_ROLES = _EDITABLE_ROLES | {
    "button",
    "push button",
    "link",
    "checkbox",
    "check box",
    "radio",
    "radio button",
    "menuitem",
    "menu item",
    "option",
    "tab",
    "switch",
    "slider",
}
_SENSITIVE_WORDS = {
    "password",
    "passwd",
    "passcode",
    "pin",
    "secret",
    "credential",
    "token",
    "otp",
}
_PERMISSION_WORDS = {
    "permission",
    "permissions",
    "authentication",
    "authorize",
    "authorization",
    "override",
    "polkit",
}
COMPUTER_STARTUP_PHASES = frozenset(
    {
        "privacy-disable",
        "privacy-reset",
        "privacy-status",
        "privacy-status-json",
        "privacy-status-values",
        "process-spawn",
        "initialize",
        "initialize-response",
        "tools-list",
        "session-start",
        "window-select",
        "capture-readiness",
        "windows-daemon-spawn",
        "windows-health",
        "windows-privacy-skip",
    }
)


class ComputerUnavailableError(RuntimeError):
    """The backend failed without exposing backend details."""

    def __init__(self, startup_phase: str | None = None) -> None:
        super().__init__("computer capability unavailable")
        self.startup_phase = (
            startup_phase if startup_phase in COMPUTER_STARTUP_PHASES else None
        )


@dataclass
class _Element:
    token: str
    editable: bool
    clicked: bool = False
    consumed: bool = False


def _web_document_indices(elements: list[dict[str, Any]]) -> frozenset[int]:
    """Return driver indices rooted in an accessibility document subtree."""
    nodes: dict[int, tuple[int | None, bool]] = {}
    duplicates: set[int] = set()
    for item in elements:
        index = item.get("element_index")
        parent = item.get("parent_index")
        role = item.get("role")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            continue
        if index in nodes:
            duplicates.add(index)
            continue
        if parent is not None and (
            not isinstance(parent, int) or isinstance(parent, bool) or parent < 0
        ):
            parent = None
        is_document = isinstance(role, str) and "document" in role.casefold()
        nodes[index] = (parent, is_document)
    for index in duplicates:
        nodes.pop(index, None)

    result: set[int] = set()
    rejected: set[int] = set()
    for index in nodes:
        chain: list[int] = []
        seen: set[int] = set()
        current: int | None = index
        belongs = False
        while current is not None and current in nodes and current not in seen:
            if current in result:
                belongs = True
                break
            if current in rejected:
                break
            seen.add(current)
            chain.append(current)
            parent, is_document = nodes[current]
            if is_document:
                belongs = True
                break
            current = parent
        (result if belongs else rejected).update(chain)
    return frozenset(result)


def safe_driver_environment(
    source: dict[str, str] | os._Environ[str],
) -> dict[str, str]:
    """Return the small environment shared by privacy commands and the driver."""
    result = {key: value for key, value in source.items() if key in _SAFE_ENV}
    result["CUA_DRIVER_TELEMETRY"] = "0"
    result["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
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
    """Require the CUA driver's bounded UIA/session readiness contract."""
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


class ComputerCapability:
    """One persistent, fail-closed cua-driver process and semantic session."""

    tools: frozenset[ToolName] = frozenset(
        {"computer.capture", "computer.click", "computer.type"}
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
    ) -> None:
        self._path = validate_driver_executable(driver_path)
        if not app_name or len(app_name) > MAX_COMPUTER_APP_LENGTH:
            raise ValueError("invalid computer configuration")
        if not window_title or len(window_title) > MAX_COMPUTER_WINDOW_TITLE_LENGTH:
            raise ValueError("invalid computer configuration")
        lowered = window_title.casefold()
        if any(word in lowered for word in _PERMISSION_WORDS):
            raise ValueError("invalid computer configuration")
        if not (
            0 < startup_timeout_seconds <= 30
            and 0 < action_timeout_seconds <= 30
            and 0 < shutdown_timeout_seconds <= 30
            and 0 < max_elements <= 1000
        ):
            raise ValueError("invalid computer configuration")
        self._app, self._title = app_name, window_title
        self._startup_timeout = startup_timeout_seconds
        self._action_timeout = action_timeout_seconds
        self._shutdown_timeout = shutdown_timeout_seconds
        self._max_elements = max_elements
        self._env = safe_driver_environment(
            dict(os.environ if environ is None else environ)
        )
        self._windows = os.name == "nt"
        self._process: asyncio.subprocess.Process | None = None
        self._daemon: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._counter = 0
        self._write_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._reset_lock = asyncio.Lock()
        self._unavailable = asyncio.Event()
        self._session: str | None = None
        self._pid: int | None = None
        self._window_id: int | None = None
        self._generation: str | None = None
        self._records: dict[str, _Element] = {}
        self._closing = False
        self._startup_phase: str | None = None

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._process is not None and not self._unavailable.is_set():
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
            except Exception:
                startup_phase = self._startup_phase
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
            # cua-driver 0.12.6's Windows telemetry subcommands can block before
            # the UIA daemon starts. The environment flags below are the
            # supported non-interactive privacy control for the Windows binary;
            # Linux retains the stronger command/status verification.
            self._startup_phase = "windows-privacy-skip"
        else:
            self._startup_phase = "privacy-disable"
            await self._privacy_command("telemetry", "disable")
            self._startup_phase = "privacy-reset"
            await self._privacy_command("telemetry", "reset-id")
            self._startup_phase = "privacy-status"
            raw = await self._privacy_command(
                "telemetry", "status", "--json", capture=True
            )
            self._startup_phase = "privacy-status-json"
            status_result = json.loads(raw)
            self._startup_phase = "privacy-status-values"
            if (
                not isinstance(status_result, dict)
                or status_result.get("enabled") is not False
                or status_result.get("installation_id_present") is not False
            ):
                raise ValueError
        if self._windows:
            self._startup_phase = "windows-daemon-spawn"
            await self._start_windows_daemon()
        self._startup_phase = "process-spawn"
        self._process = await asyncio.create_subprocess_exec(
            str(self._path),
            "mcp",
            "--no-overlay",
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
        self._validate_tools(
            await self._request("tools/list", {}, self._startup_timeout),
            windows=self._windows,
        )
        if self._windows:
            self._startup_phase = "windows-health"
            validate_windows_health(
                await self._call("health_report", {}, self._startup_timeout)
            )
        self._session = "relay-" + os.urandom(16).hex()
        self._startup_phase = "session-start"
        await self._call(
            "start_session", {"session": self._session}, self._startup_timeout
        )
        self._startup_phase = "window-select"
        window = await self._select_window(self._startup_timeout)
        self._pid, self._window_id = window
        self._startup_phase = "capture-readiness"
        await self._capture_state(self._startup_timeout)

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
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        while True:
            if self._daemon.returncode is not None:
                raise ValueError
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            try:
                status = await asyncio.wait_for(
                    self._privacy_command("status", capture=True),
                    min(remaining, 1.0),
                )
            except (OSError, TimeoutError, ValueError):
                await asyncio.sleep(min(0.1, remaining))
                continue
            if "running" in status.casefold():
                return
            await asyncio.sleep(min(0.1, remaining))

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
            raise ValueError
        return stdout.decode("utf-8")

    @staticmethod
    def _validate_tools(result: dict[str, Any], *, windows: bool = False) -> None:
        fields_by_tool = _WINDOWS_TOOL_FIELDS if windows else _TOOL_FIELDS
        required_by_tool = _WINDOWS_TOOL_REQUIRED if windows else _TOOL_REQUIRED
        additional_properties = (
            _WINDOWS_TOOL_ADDITIONAL_PROPERTIES
            if windows
            else _TOOL_ADDITIONAL_PROPERTIES
        )
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise ValueError
        found: dict[str, dict[str, Any]] = {}
        for item in tools:
            if isinstance(item, dict) and item.get("name") in fields_by_tool:
                name = item["name"]
                if name in found:
                    raise ValueError
                found[name] = item
        if set(found) != set(fields_by_tool):
            raise ValueError
        if windows:
            # Windows cua-driver releases may add optional UIA arguments or
            # describe the same inputs with a different JSON-Schema detail.
            # The public Agent contract validates actual invoke payloads; here
            # we require only the allowlisted tool names and a coherent object
            # schema so discovery remains compatible across driver patch levels.
            for item in found.values():
                schema = item.get("inputSchema")
                if (
                    not isinstance(schema, dict)
                    or schema.get("type") != "object"
                    or not isinstance(schema.get("properties"), dict)
                    or not all(
                        isinstance(name, str) and isinstance(value, dict)
                        for name, value in schema["properties"].items()
                    )
                    or not isinstance(schema.get("required", []), list)
                    or not all(
                        isinstance(name, str)
                        for name in schema.get("required", [])
                    )
                    or not set(schema.get("required", [])).issubset(
                        schema["properties"]
                    )
                ):
                    raise ValueError
            return
        for name, item in found.items():
            schema = item.get("inputSchema")
            if (
                not isinstance(schema, dict)
                or schema.get("type") != "object"
                or schema.get("additionalProperties")
                is not additional_properties.get(name, False)
            ):
                raise ValueError
            properties = schema.get("properties")
            if (
                not isinstance(properties, dict)
                or set(properties) != fields_by_tool[name]
                or not all(isinstance(value, dict) for value in properties.values())
                or set(schema.get("required", [])) != required_by_tool[name]
            ):
                raise ValueError

    async def invoke(self, message: InvokeMessage) -> dict[str, object]:
        if self._unavailable.is_set() or self._process is None:
            raise ComputerUnavailableError()
        try:
            if isinstance(message, ComputerCaptureInvoke):
                return await self._capture()
            if isinstance(message, ComputerClickInvoke):
                return await self._click(message.element_id)
            if isinstance(message, ComputerTypeInvoke):
                return await self._type(message.element_id, message.text)
            raise ValueError("unsupported computer tool")
        except asyncio.CancelledError:
            await asyncio.shield(self._reset())
            raise
        except ComputerUnavailableError:
            raise
        except Exception:
            await self._reset()
            raise ComputerUnavailableError() from None

    async def _capture(self) -> dict[str, object]:
        selected = await self._select_window(self._action_timeout)
        if selected != (self._pid, self._window_id):
            raise ValueError
        elements = await self._capture_state(self._action_timeout)
        self._generation = os.urandom(16).hex()
        self._records = {}
        structured_elements = [item for item in elements if isinstance(item, dict)]
        web_document_indices = _web_document_indices(structured_elements)
        candidates: list[
            tuple[int, int, str, _Element, dict[str, object]]
        ] = []
        for item in structured_elements:
            role, name, token = (
                item.get("role"),
                item.get("label", item.get("name", "")),
                item.get("element_token"),
            )
            if not all(isinstance(value, str) for value in (role, name, token)):
                continue
            role, name, token = role.strip(), name.strip(), token
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError
            if not enabled:
                continue
            sensitive = (role + " " + name).casefold()
            if (
                not role
                or len(role) > MAX_COMPUTER_ROLE_LENGTH
                or len(name) > MAX_COMPUTER_NAME_LENGTH
                or not token
                or any(
                    word in sensitive
                    for word in _SENSITIVE_WORDS | _PERMISSION_WORDS
                )
            ):
                continue
            element_id = os.urandom(16).hex()
            normalized_role = role.casefold()
            editable = normalized_role in _EDITABLE_ROLES
            output: dict[str, object] = {
                "element_id": element_id,
                "role": role,
                "name": name,
                "value": None,
                "enabled": True,
            }
            value = item.get("value")
            if (
                isinstance(value, str)
                and len(value) <= MAX_COMPUTER_ELEMENT_VALUE_LENGTH
            ):
                output["value"] = value
            candidates.append(
                (
                    0
                    if item.get("element_index") in web_document_indices
                    else 1,
                    0 if normalized_role in _ACTIONABLE_ROLES else 1,
                    element_id,
                    _Element(token=token, editable=editable),
                    output,
                )
            )
        public: list[dict[str, object]] = []
        public_limit = min(self._max_elements, MAX_COMPUTER_ELEMENTS)
        for _, _, element_id, record, output in sorted(
            candidates, key=lambda item: item[:2]
        )[:public_limit]:
            self._records[element_id] = record
            public.append(output)
        return {
            "app": self._app,
            "window_title": self._title,
            "generation": self._generation,
            "elements": public,
        }

    async def _click(self, element_id: str) -> dict[str, object]:
        record = self._records.get(element_id)
        generation = self._generation
        if (
            record is None
            or record.consumed
            or generation is None
            or (record.editable and record.clicked)
        ):
            raise ComputerUnavailableError()
        if record.editable:
            record.clicked = True
        else:
            record.consumed = True
        result = await self._call(
            "click",
            self._target_args(record.token),
            self._action_timeout,
            allow_error=True,
        )
        if result[0]:
            raise ValueError
        # A non-error structured response means this one semantic dispatch was
        # accepted.  Do not replay an ambiguous side effect merely because the
        # driver reports verified:false; the E2E fixture is the independent oracle.
        return {"success": True, "generation": generation, "element_id": element_id}

    async def _type(self, element_id: str, text: str) -> dict[str, object]:
        record = self._records.get(element_id)
        generation = self._generation
        if (
            record is None
            or not record.editable
            or not record.clicked
            or record.consumed
            or generation is None
        ):
            raise ComputerUnavailableError()
        record.consumed = True
        args = self._target_args(record.token) | {"text": text}
        is_error, result = await self._call(
            "type_text", args, self._action_timeout, allow_error=True
        )
        if is_error:
            if result.get("code") != "background_unavailable":
                raise ValueError
            is_error, result = await self._call(
                "type_text",
                args | {"delivery_mode": "foreground"},
                self._action_timeout,
                allow_error=True,
            )
        if is_error:
            raise ValueError
        return {"success": True, "generation": generation, "element_id": element_id}

    def _target_args(self, token: str) -> dict[str, object]:
        if self._session is None or self._pid is None or self._window_id is None:
            raise ValueError
        return {
            "session": self._session,
            "pid": self._pid,
            "window_id": self._window_id,
            "element_token": token,
        }

    async def _select_window(self, timeout: float) -> tuple[int, int]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            result = await self._call(
                "list_windows", {"on_screen_only": True}, remaining
            )
            windows = result.get("windows")
            if not isinstance(windows, list):
                raise ValueError
            matches = [
                item
                for item in windows
                if isinstance(item, dict)
                and self._app_matches(item.get("app_name"))
                and item.get("title") == self._title
            ]
            if len(matches) > 1:
                raise ValueError
            if not matches:
                await asyncio.sleep(min(0.1, max(0.0, deadline - loop.time())))
                continue
            pid, window_id = matches[0].get("pid"), matches[0].get("window_id")
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or not isinstance(window_id, int)
                or isinstance(window_id, bool)
                or window_id <= 0
            ):
                raise ValueError
            return pid, window_id

    def _app_matches(self, value: object) -> bool:
        if value == self._app:
            return True
        return self._windows and value == self._app + ".exe"

    async def _capture_state(self, timeout: float) -> list[dict[str, Any]]:
        args = self._target_args("")
        del args["element_token"]
        args.update({"include_screenshot": False, "max_elements": self._max_elements})
        result = await self._call("get_window_state", args, timeout)
        elements = result.get("elements")
        if not isinstance(elements, list) or len(elements) > self._max_elements:
            raise ValueError
        return elements

    async def _call(
        self,
        name: str,
        arguments: dict[str, object],
        timeout: float,
        *,
        allow_error: bool = False,
    ) -> Any:
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout
        )
        if set(result) - {"content", "structuredContent", "isError"}:
            raise ValueError
        structured, is_error = (
            result.get("structuredContent"),
            result.get("isError", False),
        )
        if not isinstance(structured, dict) or not isinstance(is_error, bool):
            raise ValueError
        if is_error and not allow_error:
            raise ValueError
        return (is_error, structured) if allow_error else structured

    async def _request(
        self, method: str, params: dict[str, object], timeout: float
    ) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None:
            raise ValueError
        self._counter += 1
        request_id = self._counter
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        encoded = payload.encode()
        if len(encoded) > MAX_MCP_FRAME_BYTES:
            raise ValueError
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with self._write_lock:
                process.stdin.write(encoded)
                await process.stdin.drain()
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _notify(self, method: str, params: dict[str, object]) -> None:
        if self._process is None or self._process.stdin is None:
            raise ValueError
        payload = (
            json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": params},
                separators=(",", ":"),
            )
            + "\n"
        )
        async with self._write_lock:
            self._process.stdin.write(payload.encode())
            await self._process.stdin.drain()

    async def _read_responses(self) -> None:
        try:
            assert self._process is not None and self._process.stdout is not None
            while True:
                raw = await self._process.stdout.readline()
                if not raw or len(raw) > MAX_MCP_FRAME_BYTES or not raw.endswith(b"\n"):
                    raise ValueError
                value = json.loads(raw)
                if (
                    not isinstance(value, dict)
                    or value.get("jsonrpc") != "2.0"
                    or set(value)
                    not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"})
                ):
                    raise ValueError
                ident = value.get("id")
                if (
                    not isinstance(ident, int)
                    or isinstance(ident, bool)
                    or ident not in self._pending
                ):
                    raise ValueError
                if "error" in value or not isinstance(value.get("result"), dict):
                    raise ValueError
                future = self._pending[ident]
                if future.done():
                    raise ValueError
                future.set_result(value["result"])
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closing:
                asyncio.create_task(self._reset())

    async def _watch_exit(self) -> None:
        assert self._process is not None
        await self._process.wait()
        if not self._closing:
            await self._reset()

    async def wait_unavailable(self) -> None:
        await self._unavailable.wait()

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            if (
                self._process is not None
                and self._session is not None
                and not self._unavailable.is_set()
            ):
                try:
                    await self._call(
                        "end_session",
                        {"session": self._session},
                        self._shutdown_timeout,
                    )
                except Exception:
                    pass
            await self._reset()

    async def _reset(self) -> None:
        async with self._reset_lock:
            self._closing = True
            self._records.clear()
            self._generation = self._session = None
            self._pid = self._window_id = None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ComputerUnavailableError())
            self._pending.clear()
            process, self._process = self._process, None
            daemon, self._daemon = self._daemon, None
            current = asyncio.current_task()
            tasks = [
                task
                for task in (self._reader_task, self._exit_task)
                if task is not None and task is not current
            ]
            self._reader_task = self._exit_task = None
            for task in tasks:
                task.cancel()
            if process is not None:
                await self._kill_process(process)
            if daemon is not None:
                await self._kill_process(daemon)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._unavailable.set()

    async def _kill_process(self, process: asyncio.subprocess.Process) -> None:
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
            taskkill = (
                Path(system_root) / "System32" / "taskkill.exe"
                if system_root
                else None
            )
            if taskkill is not None and taskkill.is_file():
                killer = await asyncio.create_subprocess_exec(
                    str(taskkill),
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={"SystemRoot": system_root},
                    **_process_creation_options(windows=True),
                )
                try:
                    await asyncio.wait_for(killer.wait(), self._shutdown_timeout)
                except TimeoutError:
                    killer.kill()
                    await killer.wait()
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), self._shutdown_timeout)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            return
        pgid = process.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass

        async def group_gone() -> None:
            while True:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    return
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(
                asyncio.gather(process.wait(), group_gone()),
                self._shutdown_timeout,
            )
        except TimeoutError:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()
