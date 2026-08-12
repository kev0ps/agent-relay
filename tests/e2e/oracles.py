"""Portable validation oracles for Agent Relay MCP results.

The functions in this module validate ``CallToolResult`` payloads received from
the public MCP endpoint. They are derived from the invariants declared in
``AGENTS.md`` (closed authority surface, strict typing, fail-closed dispatch)
and are intentionally re-derived from those invariants rather than copied from
platform-specific harnesses.

Each oracle:

* accepts a ``CallToolResult``-like object (``structuredContent``,
  ``isError``, ``model_extra``);
* raises ``ValueError`` on any deviation from the contract;
* performs no I/O, no subprocess, and no global state mutation;
* is portable across Linux and Windows native runners.

The native platform harnesses are responsible for constructing
``CallToolResult`` instances from the official MCP SDK; this module
only inspects their shape.
"""

from __future__ import annotations

import json as _json
import math
import os as _os
import stat as _stat
import time as _time
from pathlib import Path as _Path
from typing import Any

# Stable, sorted list of capabilities advertised by a connected Relay Agent.
# ``RelayRegistry.status_snapshot()`` exposes ``sorted(device.capabilities)``;
# the portable oracle must therefore validate the actual wire order rather
# than the scenario execution order.
_CONNECTED_CAPABILITIES: tuple[str, ...] = (
    "cua.click",
    "cua.get_window_state",
    "cua.list_windows",
    "cua.type_text",
    "system.ping",
    "terminal.exec",
)

# Required top-level keys of a ``relay_device_status`` payload. The set
# is fixed; any deviation fails closed.
_STATUS_KEYS: frozenset[str] = frozenset(
    {
        "device_id",
        "connected",
        "capabilities",
        "invocation_state",
        "progress",
        "heartbeat_age_seconds",
    }
)


def _structured(result: Any, description: str) -> dict[str, Any]:
    """Extract ``structuredContent`` as a strict ``dict``.

    Any deviation (``None``, list, ``isError=True``, unknown extra
    fields, or non-``dict`` payload) raises ``ValueError``. This is the
    universal gate that every other oracle relies on.
    """
    if not hasattr(result, "structuredContent") or not hasattr(result, "isError"):
        raise ValueError(f"invalid {description} response schema")
    if result.isError is not False:
        raise ValueError(f"invalid {description} response schema")
    extra = getattr(result, "model_extra", None)
    if bool(extra):
        raise ValueError(f"invalid {description} response schema")
    payload = result.structuredContent
    if type(payload) is not dict:
        raise ValueError(f"invalid {description} response schema")
    return payload


def validate_status(
    result: Any,
    *,
    device_id: str | None,
    connected: bool,
    expected_capabilities: tuple[str, ...] | None = None,
    allow_unenrolled: bool = False,
) -> None:
    """Validate a ``relay_device_status`` ``CallToolResult``.

    Parameters
    ----------
    result:
        The ``CallToolResult`` returned by the server. The kernel only
        inspects ``structuredContent``, ``isError``, and ``model_extra``.
    device_id:
        The learned opaque device identifier, or ``None`` only for the
        pre-enrollment offline state.
    allow_unenrolled:
        Permit ``device_id=None`` for the initial offline probe only. This
        must remain false for post-enrollment offline assertions.
    connected:
        ``True`` if the harness expects the agent to be online;
        ``False`` if the harness is asserting the offline / restarting
        state. The expected capability list and heartbeat age depend
        on this flag.
    """
    payload = _structured(result, "status")

    if set(payload) != set(_STATUS_KEYS):
        raise ValueError("invalid status response schema")

    observed_device_id = payload["device_id"]
    if allow_unenrolled:
        if connected or device_id is not None or observed_device_id is not None:
            raise ValueError("invalid status response schema")
    elif type(observed_device_id) is not str or observed_device_id != device_id:
        raise ValueError("invalid status response schema")

    if type(payload["connected"]) is not bool or payload["connected"] is not connected:
        raise ValueError("invalid status response schema")

    capabilities = payload["capabilities"]
    expected = list(
        _CONNECTED_CAPABILITIES
        if expected_capabilities is None
        else expected_capabilities
    )
    expected_capabilities_payload = expected if connected else []
    if (
        type(capabilities) is not list
        or capabilities != expected_capabilities_payload
        or not all(type(item) is str for item in capabilities)
    ):
        raise ValueError("invalid status response schema")

    if (
        type(payload["invocation_state"]) is not str
        or payload["invocation_state"] != "idle"
    ):
        raise ValueError("invalid status response schema")

    if payload["progress"] is not None:
        raise ValueError("invalid status response schema")

    heartbeat_age = payload["heartbeat_age_seconds"]
    if connected:
        if (
            type(heartbeat_age) is not float
            or not math.isfinite(heartbeat_age)
            or heartbeat_age < 0.0
        ):
            raise ValueError("invalid status response schema")
    else:
        if heartbeat_age is not None:
            raise ValueError("invalid status response schema")


def classify_status_failure(
    result: Any,
    *,
    device_id: str | None,
    connected: bool,
    expected_capabilities: tuple[str, ...],
) -> str:
    """Classify status drift without returning response fields or names."""
    payload = getattr(result, "structuredContent", None)
    if type(payload) is not dict:
        return "response-shape"
    if payload.get("device_id") != device_id:
        return "device-id"
    if payload.get("connected") is not connected:
        return "connection-state"
    capabilities = payload.get("capabilities")
    if type(capabilities) is not list:
        return "capabilities-shape"
    expected = list(expected_capabilities) if connected else []
    if len(capabilities) != len(expected):
        return "capabilities-count"
    if set(capabilities) != set(expected):
        return "capabilities-set"
    if capabilities != expected:
        return "capabilities-order"
    return "status-contract"


# --- Ping oracle ------------------------------------------------------------

_PING_KEYS: frozenset[str] = frozenset({"pong"})


def validate_ping(result: Any) -> None:
    """Validate a ``relay_system_ping`` ``CallToolResult``.

    The contract is the smallest possible: the payload must contain
    exactly the key ``pong`` and its value must be the literal
    ``True``. Anything else fails closed.
    """
    payload = _structured(result, "ping")
    if set(payload) != set(_PING_KEYS) or payload["pong"] is not True:
        raise ValueError("invalid ping response schema")


# --- Terminal oracle --------------------------------------------------------

_TERMINAL_KEYS: frozenset[str] = frozenset(
    {
        "command_id",
        "stdout",
        "stderr",
        "exit_code",
        "timed_out",
        "stdout_truncated",
        "stderr_truncated",
    }
)


def validate_terminal(result: Any, *, command_id: str, expected: str) -> None:
    """Validate a ``relay_terminal_exec`` ``CallToolResult``.

    The harness passes the ``command_id`` it invoked and the exact
    ``expected`` marker the fixed allowlist entry is supposed to write
    to stdout. The oracle rejects any deviation: non-zero exit, timed
    out, non-empty stderr, command-id mismatch, missing trailing
    newline, or any extra top-level key.
    """
    payload = _structured(result, "terminal")
    if set(payload) != set(_TERMINAL_KEYS):
        raise ValueError("invalid terminal response schema")
    if (
        type(payload["command_id"]) is not str
        or payload["command_id"] != command_id
        or type(payload["stdout"]) is not str
        or payload["stdout"] != f"{expected}\n"
        or type(payload["stderr"]) is not str
        or payload["stderr"] != ""
        or type(payload["exit_code"]) is not int
        or payload["exit_code"] != 0
        or type(payload["timed_out"]) is not bool
        or payload["timed_out"] is not False
        or type(payload["stdout_truncated"]) is not bool
        or payload["stdout_truncated"] is not False
        or type(payload["stderr_truncated"]) is not bool
        or payload["stderr_truncated"] is not False
    ):
        raise ValueError("invalid terminal response schema")


# --- Generic CUA provider oracles -------------------------------------------

_CUA_WINDOW_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"window_id", "pid", "app_name", "title", "bounds", "is_on_screen"}
)
_CUA_BOUND_KEYS: frozenset[str] = frozenset({"x", "y", "width", "height"})
_FIELD_ROLES: frozenset[str] = frozenset({"textbox", "entry", "text", "edit", "editable"})
_BUTTON_ROLES: frozenset[str] = frozenset({"button", "push button"})
_CUA_MAX_WINDOWS: int = 64
_CUA_MAX_ELEMENTS: int = 256


def _bounded_int(value: Any) -> bool:
    return type(value) is int and -(2**31) <= value <= 2**31


def _exact_str(
    value: Any,
    *,
    nonempty: bool = False,
    maximum: int,
) -> bool:
    return (
        type(value) is str
        and len(value) <= maximum
        and (not nonempty or bool(value))
    )


def validate_cua_list_windows(
    result: Any,
    *,
    expected_pid: int | None = None,
    expected_app: str | None = None,
    expected_window_title: str | None = None,
) -> tuple[int, int]:
    """Validate generic ``cua.list_windows`` output and return ``(pid, xid)``."""
    payload = _structured(result, "relay_cua_list_windows")
    if set(payload) != {"windows"}:
        raise ValueError("invalid relay_cua_list_windows response schema")
    windows = payload["windows"]
    if type(windows) is not list or not 1 <= len(windows) <= _CUA_MAX_WINDOWS:
        raise ValueError("invalid relay_cua_list_windows response schema")
    candidates: list[dict[str, Any]] = []
    for window in windows:
        if type(window) is not dict or not _CUA_WINDOW_REQUIRED_KEYS.issubset(window):
            raise ValueError("invalid relay_cua_list_windows response schema")
        if (
            not _bounded_int(window["window_id"])
            or window["window_id"] <= 0
            or (window["pid"] is not None and not _bounded_int(window["pid"]))
            or not _exact_str(window["app_name"], maximum=256)
            or not _exact_str(window["title"], maximum=512)
            or type(window["is_on_screen"]) is not bool
            or type(window["bounds"]) is not dict
            or not _CUA_BOUND_KEYS.issubset(window["bounds"])
            or any(not _bounded_int(window["bounds"][key]) for key in _CUA_BOUND_KEYS)
        ):
            raise ValueError("invalid relay_cua_list_windows response schema")
        if expected_pid is None or window["pid"] == expected_pid:
            if expected_app is None or window["app_name"] == expected_app:
                if expected_window_title is None or window["title"] == expected_window_title:
                    candidates.append(window)
    if len(candidates) != 1 or candidates[0]["pid"] is None:
        raise ValueError("invalid relay_cua_list_windows fixture identity")
    return candidates[0]["pid"], candidates[0]["window_id"]


def validate_cua_window_state(
    result: Any,
    *,
    expected_pid: int,
    window_id: int,
    diagnostic_phase: list[str] | None = None,
) -> tuple[str, str, str]:
    """Validate a bounded CUA snapshot and return fresh element tokens.

    The driver owns the snapshot/token lifecycle. Relay E2E only consumes the
    opaque ``element_token`` values and never treats a DOM/AX handle as public
    data.
    """
    if diagnostic_phase is not None:
        diagnostic_phase.append("cua-snapshot-structured")
    payload = _structured(result, "relay_cua_get_window_state")
    required = {"pid", "window_id", "elements", "snapshot_id"}
    if set(payload) - {
        "pid", "window_id", "elements", "snapshot_id", "element_count",
        "elements_complete", "total_element_count", "returned_element_count",
        "tree_markdown", "_note", "degraded", "degraded_reason",
    } or not required.issubset(payload):
        raise ValueError("invalid relay_cua_get_window_state response schema")
    if payload["pid"] != expected_pid or payload["window_id"] != window_id:
        raise ValueError("invalid relay_cua_get_window_state identity")
    if not _exact_str(payload["snapshot_id"], nonempty=True, maximum=256):
        raise ValueError("invalid relay_cua_get_window_state snapshot")
    elements = payload["elements"]
    if type(elements) is not list or not 1 <= len(elements) <= _CUA_MAX_ELEMENTS:
        raise ValueError("invalid relay_cua_get_window_state response schema")
    field_tokens: list[str] = []
    button_tokens: list[str] = []
    seen_indices: set[int] = set()
    for index, element in enumerate(elements):
        if diagnostic_phase is not None:
            diagnostic_phase.append(f"cua-element-{index}")
        if (
            type(element) is not dict
            or not {"element_index", "role"}.issubset(element)
            or type(element["element_index"]) is not int
            or element["element_index"] < 0
            or element["element_index"] in seen_indices
            or not _exact_str(element["role"], nonempty=True, maximum=128)
            or not _exact_str(element.get("element_token"), nonempty=True, maximum=256)
            or ("label" in element and not _exact_str(element["label"], maximum=512))
            or ("value" in element and not _exact_str(element["value"], maximum=2048))
        ):
            raise ValueError("invalid relay_cua_get_window_state element schema")
        seen_indices.add(element["element_index"])
        label = str(element.get("label", ""))
        role = element["role"].casefold()
        if role in _FIELD_ROLES and label.casefold() == "name":
            field_tokens.append(element["element_token"])
        if role in _BUTTON_ROLES and label.casefold() == "apply":
            button_tokens.append(element["element_token"])
    if len(field_tokens) != 1 or len(button_tokens) != 1:
        raise ValueError("invalid relay_cua_get_window_state fixture controls")
    return payload["snapshot_id"], field_tokens[0], button_tokens[0]


def validate_cua_action(result: Any, *, tool_name: str) -> None:
    """Validate bounded CUA action metadata without inventing verification."""
    payload = _structured(result, tool_name)
    allowed = {"path", "verified", "effect", "characters", "escalation", "scope"}
    required = {"path", "verified", "effect"}
    if not required.issubset(payload) or set(payload) - allowed:
        raise ValueError(f"invalid {tool_name} response schema")
    if (
        not _exact_str(payload["path"], nonempty=True, maximum=128)
        or type(payload["verified"]) is not bool
        or payload["effect"] not in {"confirmed", "unverifiable", "suspected_noop"}
    ):
        raise ValueError(f"invalid {tool_name} response schema")
    if "characters" in payload and (type(payload["characters"]) is not int or payload["characters"] < 0):
        raise ValueError(f"invalid {tool_name} response schema")


def validate_cua_browser_success(result: Any, *, tool_name: str) -> None:
    """Validate a successful CUA browser lifecycle/action envelope."""
    if not hasattr(result, "isError") or result.isError is not False:
        raise ValueError(f"invalid {tool_name} response schema")
    extra = getattr(result, "model_extra", None)
    if bool(extra):
        raise ValueError(f"invalid {tool_name} response schema")


def validate_cua_browser_launch(result: Any) -> int:
    """Return the PID explicitly created by CUA 0.19 ``launch_app``."""
    payload = _structured(result, "relay_cua_launch_app")
    pid = payload.get("pid")
    if not _bounded_int(pid) or pid <= 0:
        raise ValueError("invalid relay_cua_launch_app process identity")
    name = payload.get("name")
    if not _exact_str(name, nonempty=True, maximum=256):
        raise ValueError("invalid relay_cua_launch_app process name")
    if "active" in payload and type(payload["active"]) is not bool:
        raise ValueError("invalid relay_cua_launch_app active state")
    if "windows" in payload and not isinstance(payload["windows"], list):
        raise ValueError("invalid relay_cua_launch_app windows")
    if "running" in payload and payload["running"] is not True:
        raise ValueError("invalid relay_cua_launch_app process state")
    return pid


def validate_cua_browser_prepare(result: Any) -> int:
    """Return the isolated browser PID minted by CUA ``browser_prepare``."""
    payload = _structured(result, "relay_cua_browser_prepare")
    prepared_pid = payload.get("prepared_pid")
    if (
        payload.get("status") != "ok"
        or payload.get("prepared") is not True
        or not _bounded_int(prepared_pid)
        or prepared_pid <= 0
    ):
        raise ValueError("invalid relay_cua_browser_prepare response schema")
    return prepared_pid


def validate_cua_browser_binding(
    result: Any,
    *,
    expected_pid: int,
    expected_window_id: int,
) -> tuple[str, str]:
    """Validate an exact browser binding and return one active tab."""
    payload = _structured(result, "relay_cua_get_browser_state")
    if "pid" in payload and payload["pid"] != expected_pid:
        raise ValueError("invalid relay_cua_get_browser_state process identity")
    if "window_id" in payload and payload["window_id"] != expected_window_id:
        raise ValueError("invalid relay_cua_get_browser_state window identity")
    target_id = payload.get("target_id")
    tabs = payload.get("tabs")
    if not _exact_str(target_id, nonempty=True, maximum=256) or not isinstance(tabs, list):
        raise ValueError("invalid relay_cua_get_browser_state binding schema")
    active_tabs: list[str] = []
    for tab in tabs:
        if not isinstance(tab, dict):
            raise ValueError("invalid relay_cua_get_browser_state tab schema")
        tab_id = tab.get("tab_id")
        if not _exact_str(tab_id, nonempty=True, maximum=256):
            raise ValueError("invalid relay_cua_get_browser_state tab identity")
        active = tab.get("active")
        if active is True:
            active_tabs.append(tab_id)
    if len(active_tabs) != 1:
        raise ValueError("invalid relay_cua_get_browser_state active tab")
    return target_id, active_tabs[0]


def _contains_browser_text(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return expected in value
    if isinstance(value, dict):
        return any(_contains_browser_text(child, expected) for child in value.values())
    if isinstance(value, list):
        return any(_contains_browser_text(child, expected) for child in value)
    return False


def validate_cua_browser_state(
    result: Any,
    *,
    expected_url: str,
    expected_text: str | None = None,
) -> None:
    """Prove the local page URL and optional marker through CUA state."""
    payload = _structured(result, "relay_cua_get_browser_state")
    normalized_url = expected_url.rstrip("/")
    if not (
        _contains_browser_text(payload, expected_url)
        or _contains_browser_text(payload, normalized_url)
    ) or (
        expected_text is not None
        and not _contains_browser_text(payload, expected_text)
    ):
        raise ValueError("invalid relay_cua_get_browser_state page state")


#
# These oracles read JSONL files that the CUA fixtures
# write on disk after each successful action. The files are the
# independent side-effect proof that the harness uses to detect
# "successful driver response without an actual mutation". The oracles
# are portable across Linux and Windows; they use ``os.open`` with
# optional flags guarded by ``getattr`` so Windows builds do not
# require POSIX-only ``O_NOFOLLOW``.

_MAX_EVENT_BYTES: int = 1024
_EVENT_POLL_INTERVAL_SECONDS: float = 0.05


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _read_event(
    path: _Path, expected_payload: dict[str, str], action: str
) -> bytes:
    """Read one strict JSONL event and verify it matches the expectation.

    The fixture must emit exactly one newline-terminated JSON object on
    a regular file with a single hardlink, no symlink, and a bounded
    size. The decoded payload must contain exactly the three expected
    string fields and round-trip through ``json.dumps`` to the same
    bytes that were read.
    """
    flags = (
        _os.O_RDONLY
        | getattr(_os, "O_NOFOLLOW", 0)
        | getattr(_os, "O_NONBLOCK", 0)
        | getattr(_os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = _os.open(path, flags)
        try:
            metadata = _os.fstat(descriptor)
            if (
                not _stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > _MAX_EVENT_BYTES
            ):
                raise ValueError
            raw = _os.read(descriptor, _MAX_EVENT_BYTES + 1)
        finally:
            _os.close(descriptor)
        if (
            len(raw) != metadata.st_size
            or not raw.endswith(b"\n")
            or raw.count(b"\n") != 1
        ):
            raise ValueError
        payload = _json.loads(
            raw[:-1].decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        if (
            type(payload) is not dict
            or set(payload) != set(expected_payload)
            or any(type(item) is not str for item in payload.values())
            or payload != expected_payload
        ):
            raise ValueError
        expected_bytes = (
            _json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        if raw != expected_bytes:
            raise ValueError
        return raw
    except (OSError, UnicodeError, _json.JSONDecodeError, ValueError):
        raise ValueError(f"invalid CUA event for {action}") from None


def validate_cua_event(path: _Path, *, run_id: str, value: str) -> bytes:
    """Validate a Computer ``applied`` CUA event."""
    return _read_event(
        path,
        {"run_id": run_id, "event": "applied", "value": value},
        "relay_cua_click",
    )


def assert_no_cua_event(path: _Path) -> None:
    """Refuse any pre-existing CUA event at ``path``.

    Called before the CUA action that is supposed to produce the
    event. If a stale event from a previous run is still present, the
    oracle fails closed.
    """
    try:
        _os.lstat(path)
    except FileNotFoundError:
        return
    raise ValueError(f"CUA event exists before relay_cua_click at {path}")


def poll_cua_event(
    path: _Path, *, run_id: str, value: str, timeout: float
) -> None:
    """Poll for a CUA CUA event, bounded by ``timeout`` seconds.

    Returns when two consecutive reads agree on the same valid event,
    or raises ``ValueError`` for a malformed event and ``TimeoutError``
    if no event appears before the deadline.
    """
    deadline = _time.monotonic() + timeout
    appeared = False
    previous_valid: bytes | None = None
    while _time.monotonic() < deadline:
        try:
            _os.lstat(path)
            appeared = True
        except FileNotFoundError:
            previous_valid = None
        else:
            try:
                current = validate_cua_event(
                    path, run_id=run_id, value=value
                )
            except ValueError:
                previous_valid = None
            else:
                if current == previous_valid:
                    return
                previous_valid = current
        _time.sleep(_EVENT_POLL_INTERVAL_SECONDS)
    if appeared:
        raise ValueError(
            f"invalid CUA event for relay_cua_click at {path}"
        )
    raise TimeoutError(
        f"CUA event absent for relay_cua_click at {path}"
    )
