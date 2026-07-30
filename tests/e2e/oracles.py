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
    "browser.click",
    "browser.fill",
    "browser.list_tabs",
    "browser.navigate",
    "browser.read_page",
    "computer.capture",
    "computer.click",
    "computer.type",
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


# --- List tabs oracle -------------------------------------------------------

_LIST_TABS_KEYS: frozenset[str] = frozenset({"tabs"})
_TAB_KEYS: frozenset[str] = frozenset({"tab_id", "title", "url"})


def _exact_str(value: Any, *, nonempty: bool = False, maximum: int = 4096) -> bool:
    """Strict string check: bounded length, exact ``str`` type.

    The portable oracle does NOT permit ``str`` subclasses (no
    ``StringView`` etc.) and bounds the length so a malicious payload
    cannot grow the validation memory.
    """
    return (
        type(value) is str
        and len(value) <= maximum
        and (not nonempty or bool(value))
    )


def validate_list_tabs(result: Any) -> str:
    """Validate a ``relay_browser_list_tabs`` ``CallToolResult``.

    The current product contract requires exactly one tab whose URL is
    ``about:blank``. The oracle returns the opaque ``tab_id`` so the
    caller can chain subsequent Browser actions.
    """
    payload = _structured(result, "relay_browser_list_tabs")
    if set(payload) != set(_LIST_TABS_KEYS):
        raise ValueError("invalid relay_browser_list_tabs response schema")
    tabs = payload["tabs"]
    if type(tabs) is not list or len(tabs) != 1:
        raise ValueError("invalid relay_browser_list_tabs response schema")
    tab = tabs[0]
    if (
        type(tab) is not dict
        or set(tab) != set(_TAB_KEYS)
        or not _exact_str(tab["tab_id"], nonempty=True, maximum=128)
        or not _exact_str(tab["title"], maximum=256)
        or tab["url"] != "about:blank"
    ):
        raise ValueError("invalid relay_browser_list_tabs response schema")
    return tab["tab_id"]


# --- Action oracle ----------------------------------------------------------

_ACTION_KEYS: frozenset[str] = frozenset(
    {"tab_id", "element_id", "url", "title", "success"}
)


def validate_action(
    result: Any,
    *,
    tool_name: str,
    tab_id: str,
    element_id: str | None,
    fixture_url: str,
    fixture_title: str,
) -> None:
    """Validate a Browser ``relay_browser_*`` ``CallToolResult``.

    The harness passes the ``fixture_url`` and ``fixture_title`` the
    Browser fixture serves. The oracle rejects any URL outside the
    allowlist, any title drift, any ``success=False`` (which would
    normally be an MCP error), and any extra top-level key.

    ``element_id`` is opaque and short-lived; the oracle enforces only
    that the wire value matches what the caller supplied (or is
    ``None`` for navigation).
    """
    payload = _structured(result, tool_name)
    if set(payload) != set(_ACTION_KEYS):
        raise ValueError(f"invalid {tool_name} response schema")
    if (
        not _exact_str(payload["tab_id"], nonempty=True, maximum=128)
        or payload["tab_id"] != tab_id
        or (
            element_id is not None
            and (
                type(payload["element_id"]) is not str
                or payload["element_id"] != element_id
            )
        )
        or (element_id is None and payload["element_id"] is not None)
        or type(payload["url"]) is not str
        or payload["url"] != fixture_url
        or type(payload["title"]) is not str
        or payload["title"] != fixture_title
        or payload["success"] is not True
    ):
        raise ValueError(f"invalid {tool_name} response schema")


# --- Computer capture oracle ------------------------------------------------

_COMPUTER_CAPTURE_KEYS: frozenset[str] = frozenset(
    {"app", "window_title", "generation", "elements"}
)
_COMPUTER_ELEMENT_KEYS: frozenset[str] = frozenset(
    {"element_id", "role", "name", "value", "enabled"}
)
_COMPUTER_DECOY_NAMES: frozenset[str] = frozenset(
    {"dormant override", "vault password", "grant camera permission"}
)
# Recognized Computer-Use field/button roles, normalized to lowercase.
_FIELD_ROLES: frozenset[str] = frozenset({"textbox", "entry", "text", "edit"})
_BUTTON_ROLES: frozenset[str] = frozenset({"button", "push button"})


def _mark(phase: list[str] | None, value: str) -> None:
    if phase is not None:
        phase.append(value)


def validate_computer_capture(
    result: Any,
    *,
    diagnostic_phase: list[str] | None = None,
    expected_app: str,
    expected_window_title: str,
) -> tuple[str, str]:
    """Validate a ``relay_computer_capture`` ``CallToolResult``.

    Returns ``(field_element_id, button_element_id)``. The fixture must
    expose exactly one enabled textbox-like field and exactly one
    enabled ``Apply`` button. Decoys and out-of-fixture window/app
    identities fail closed. Platform harnesses must provide their synthetic
    fixture identity; equality remains exact.

    ``diagnostic_phase`` (optional) receives short string markers on
    every internal step the oracle traverses, both successful and
    failed. This lets the harness classify failures without leaking
    payload internals.
    """
    _mark(diagnostic_phase, "capture-structured")
    payload = _structured(result, "relay_computer_capture")
    _mark(diagnostic_phase, "capture-top-level")
    if set(payload) != set(_COMPUTER_CAPTURE_KEYS):
        _mark(diagnostic_phase, "capture-top-level-mismatch")
        raise ValueError("invalid relay_computer_capture response schema")
    _mark(diagnostic_phase, "capture-identity")
    elements = payload["elements"]
    if (
        payload["app"] != expected_app
        or payload["window_title"] != expected_window_title
        or not _exact_str(payload["generation"], nonempty=True, maximum=128)
        or type(elements) is not list
    ):
        _mark(diagnostic_phase, "capture-identity-mismatch")
        raise ValueError("invalid relay_computer_capture response schema")
    _mark(diagnostic_phase, "capture-count")
    if not 1 <= len(elements) <= 12:
        _mark(diagnostic_phase, "capture-count-out-of-range")
        raise ValueError("invalid relay_computer_capture response schema")
    field_ids: list[str] = []
    button_ids: list[str] = []
    seen_ids: set[str] = set()
    for index, element in enumerate(elements):
        _mark(diagnostic_phase, f"capture-element-{index}")
        if (
            type(element) is not dict
            or set(element) != set(_COMPUTER_ELEMENT_KEYS)
            or not _exact_str(element["element_id"], nonempty=True, maximum=128)
            or not _exact_str(element["role"], nonempty=True, maximum=64)
            or not _exact_str(element["name"], maximum=128)
            or (
                element["value"] is not None
                and not _exact_str(element["value"], maximum=256)
            )
            or type(element["enabled"]) is not bool
        ):
            _mark(diagnostic_phase, "capture-element-shape-mismatch")
            raise ValueError("invalid relay_computer_capture response schema")
        if element["element_id"] in seen_ids:
            _mark(diagnostic_phase, "capture-duplicate-id")
            raise ValueError("invalid relay_computer_capture response schema")
        seen_ids.add(element["element_id"])
        if element["enabled"] and element["role"].casefold() in _FIELD_ROLES:
            field_ids.append(element["element_id"])
        if (
            element["enabled"]
            and element["name"].casefold() == "apply"
            and element["role"].casefold() in _BUTTON_ROLES
        ):
            button_ids.append(element["element_id"])
    _mark(diagnostic_phase, "capture-decoy")
    for element in elements:
        if element["name"].casefold() in _COMPUTER_DECOY_NAMES:
            _mark(diagnostic_phase, "capture-decoy-detected")
            raise ValueError("invalid relay_computer_capture controls")
    _mark(diagnostic_phase, "capture-controls")
    if len(field_ids) != 1:
        if not field_ids:
            for element in elements:
                if (
                    element["role"].casefold() == "editable control"
                    or element["role"].casefold() == "editable"
                ):
                    _mark(diagnostic_phase, "capture-controls-field-role-edit-variant")
                    break
                if (
                    element["role"].casefold() == "label"
                    and element["name"] == "Name"
                ):
                    _mark(diagnostic_phase, "capture-controls-field-name-role-label")
                    break
        _mark(diagnostic_phase, "capture-controls-field-mismatch")
        raise ValueError("invalid relay_computer_capture controls")
    if len(button_ids) != 1:
        _mark(diagnostic_phase, "capture-controls-button-mismatch")
        raise ValueError("invalid relay_computer_capture controls")
    _mark(diagnostic_phase, "capture-success")
    return field_ids[0], button_ids[0]


# --- Computer action oracle -------------------------------------------------

_COMPUTER_ACTION_KEYS: frozenset[str] = frozenset(
    {"success", "generation", "element_id"}
)


def validate_computer_action(
    result: Any,
    *,
    tool_name: str,
    generation: str,
    element_id: str,
) -> None:
    """Validate a Computer-Use action ``CallToolResult``.

    The wire payload must echo back the exact ``generation`` token and
    ``element_id`` the harness issued. This is the Computer-Use
    equivalent of the Browser ``tab_id`` echo and prevents a stale or
    replayed response from being accepted.
    """
    payload = _structured(result, tool_name)
    if (
        set(payload) != set(_COMPUTER_ACTION_KEYS)
        or payload["success"] is not True
        or payload["generation"] != generation
        or payload["element_id"] != element_id
    ):
        raise ValueError(f"invalid {tool_name} response schema")


# --- Read page oracle -------------------------------------------------------

_READ_PAGE_KEYS: frozenset[str] = frozenset(
    {"tab_id", "url", "title", "text", "elements"}
)
_READ_PAGE_ELEMENT_KEYS: frozenset[str] = frozenset(
    {"element_id", "role", "name", "value", "editable", "enabled"}
)
_READ_PAGE_TEXT_REQUIRED_MARKERS: tuple[str, ...] = (
    "Relay Browser Fixture",
    "Name",
    "Submit",
)
_READ_PAGE_MAX_TEXT_LEN: int = 4096
_READ_PAGE_MAX_ELEMENTS: int = 12


def validate_read_page(
    result: Any,
    *,
    tab_id: str,
    fixture_url: str,
    fixture_title: str,
    diagnostic_phase: list[str] | None = None,
) -> tuple[str, str]:
    """Validate a ``relay_browser_read_page`` ``CallToolResult``.

    Returns ``(textbox_element_id, button_element_id)``. The page
    must come from the fixture origin, its text must contain the
    required markers (``title``, ``Name``, ``Submit``), and there must
    be exactly one editable textbox and exactly one enabled button.
    """
    _mark(diagnostic_phase, "read-page-structured")
    payload = _structured(result, "relay_browser_read_page")
    _mark(diagnostic_phase, "read-page-top-level")
    if set(payload) != set(_READ_PAGE_KEYS):
        raise ValueError("invalid relay_browser_read_page response schema")
    _mark(diagnostic_phase, "read-page-identity")
    if (
        type(payload["tab_id"]) is not str
        or payload["tab_id"] != tab_id
        or payload["url"] != fixture_url
        or payload["title"] != fixture_title
    ):
        raise ValueError("invalid relay_browser_read_page response schema")
    _mark(diagnostic_phase, "read-page-text")
    text = payload["text"]
    if (
        type(text) is not str
        or len(text) > _READ_PAGE_MAX_TEXT_LEN
        or any(marker not in text for marker in _READ_PAGE_TEXT_REQUIRED_MARKERS)
    ):
        raise ValueError("invalid relay_browser_read_page response schema")
    _mark(diagnostic_phase, "read-page-elements")
    elements = payload["elements"]
    if type(elements) is not list or len(elements) > _READ_PAGE_MAX_ELEMENTS:
        raise ValueError("invalid relay_browser_read_page response schema")
    textbox_ids: list[str] = []
    button_ids: list[str] = []
    for index, element in enumerate(elements):
        _mark(diagnostic_phase, f"read-page-element-{index}")
        if (
            type(element) is not dict
            or set(element) != set(_READ_PAGE_ELEMENT_KEYS)
            or not _exact_str(element["element_id"], nonempty=True, maximum=128)
            or not _exact_str(element["role"], nonempty=True, maximum=64)
            or not _exact_str(element["name"], maximum=128)
            or (
                element["value"] is not None
                and not _exact_str(element["value"], maximum=256)
            )
            or type(element["editable"]) is not bool
            or type(element["enabled"]) is not bool
        ):
            _mark(diagnostic_phase, "read-page-element-schema")
            raise ValueError("invalid relay_browser_read_page response schema")
        if (
            element["role"] == "textbox"
            and element["name"] == "Name"
            and element["enabled"]
            and element["editable"]
        ):
            textbox_ids.append(element["element_id"])
        if (
            element["role"] == "button"
            and element["name"] == "Submit"
            and element["enabled"]
            and not element["editable"]
        ):
            button_ids.append(element["element_id"])
    _mark(diagnostic_phase, "read-page-controls")
    if len(textbox_ids) != 1:
        if len(textbox_ids) > 1:
            _mark(diagnostic_phase, "read-page-textbox-ambiguous")
        elif not any(
            type(element) is dict
            and element.get("role") == "textbox"
            for element in elements
        ):
            _mark(diagnostic_phase, "read-page-textbox-role")
        elif not any(
            type(element) is dict
            and element.get("role") == "textbox"
            and element.get("name") == "Name"
            for element in elements
        ):
            _mark(diagnostic_phase, "read-page-textbox-name")
        else:
            _mark(diagnostic_phase, "read-page-textbox-state")
        raise ValueError("invalid relay_browser_read_page controls")
    if len(button_ids) != 1:
        if len(button_ids) > 1:
            _mark(diagnostic_phase, "read-page-button-ambiguous")
        elif not any(
            type(element) is dict
            and element.get("role") == "button"
            for element in elements
        ):
            _mark(diagnostic_phase, "read-page-button-role")
        elif not any(
            type(element) is dict
            and element.get("role") == "button"
            and element.get("name") == "Submit"
            for element in elements
        ):
            _mark(diagnostic_phase, "read-page-button-name")
        else:
            _mark(diagnostic_phase, "read-page-button-state")
        raise ValueError("invalid relay_browser_read_page controls")
    _mark(diagnostic_phase, "read-page-success")
    return textbox_ids[0], button_ids[0]


# --- Fixture event oracles --------------------------------------------------
#
# These oracles read JSONL files that the Browser/Computer fixtures
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
        raise ValueError(f"invalid fixture event for {action}") from None


def validate_fixture_event(path: _Path, *, run_id: str, value: str) -> bytes:
    """Validate a Browser ``submitted`` fixture event."""
    return _read_event(
        path,
        {"run_id": run_id, "event": "submitted", "value": value},
        "relay_browser_click",
    )


def validate_computer_event(path: _Path, *, run_id: str, value: str) -> bytes:
    """Validate a Computer ``applied`` fixture event."""
    return _read_event(
        path,
        {"run_id": run_id, "event": "applied", "value": value},
        "relay_computer_click",
    )


def assert_no_fixture_event(path: _Path) -> None:
    """Refuse any pre-existing fixture event at ``path``.

    Called before the Browser action that is supposed to produce the
    event. If a stale event from a previous run is still present, the
    oracle fails closed.
    """
    try:
        _os.lstat(path)
    except FileNotFoundError:
        return
    raise ValueError(f"fixture event exists before relay_browser_click at {path}")


def poll_fixture_event(
    path: _Path, *, run_id: str, value: str, timeout: float
) -> None:
    """Poll for a Browser fixture event, bounded by ``timeout`` seconds.

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
                current = validate_fixture_event(
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
            f"invalid fixture event for relay_browser_click at {path}"
        )
    raise TimeoutError(
        f"fixture event absent for relay_browser_click at {path}"
    )