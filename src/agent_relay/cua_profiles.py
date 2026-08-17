"""Versioned, explicit CUA access profiles.

The profile is deliberately kept separate from catalog risk classification.  A
new CUA descriptor may be visible in the catalog, but it is not part of an
access profile until this module is updated explicitly (or an operator enables
that individual public name).
"""

from __future__ import annotations

import re
from typing import Literal

CuaAccessLevel = Literal["none", "standard", "full"]
ReportedCuaAccess = Literal["none", "standard", "full", "custom"]

STANDARD_CUA_TOOL_NAMES: tuple[str, ...] = (
    "list_apps",
    "list_windows",
    "get_window_state",
    "verify_state",
    "get_browser_state",
    "launch_app",
    "bring_to_front",
    "set_window_frame",
    "invoke_menu",
    "click",
    "double_click",
    "right_click",
    "drag",
    "type_text",
    "press_key",
    "hotkey",
    "set_value",
    "scroll",
    "move_cursor",
    "zoom",
    "browser_prepare",
    "browser_navigate",
    "browser_click",
    "browser_type",
)

FULL_CUA_TOOL_NAMES: tuple[str, ...] = STANDARD_CUA_TOOL_NAMES + (
    "get_config",
    "get_recording_state",
    "get_agent_cursor_state",
    "health_report",
    "check_for_update",
    "kill_app",
    "clipboard_write",
    "browser_dialog",
    "browser_set_input_files",
    "browser_download",
    "start_recording",
    "stop_recording",
    "replay_trajectory",
    "set_config",
    "start_session",
    "end_session",
    "escalate_session",
    "get_session_state",
    "check_permissions",
    "install_ffmpeg",
    "set_agent_cursor_enabled",
    "set_agent_cursor_motion",
    "set_agent_cursor_theme",
)

CUA_PROFILE_TOOL_NAMES: dict[CuaAccessLevel, tuple[str, ...]] = {
    "none": (),
    "standard": STANDARD_CUA_TOOL_NAMES,
    "full": FULL_CUA_TOOL_NAMES,
}

CUA_PROFILE_PUBLIC_NAMES: dict[CuaAccessLevel, tuple[str, ...]] = {
    level: tuple(f"relay_cua_{name}" for name in names)
    for level, names in CUA_PROFILE_TOOL_NAMES.items()
}

ALL_PROFILE_PUBLIC_NAMES = frozenset(
    name for names in CUA_PROFILE_PUBLIC_NAMES.values() for name in names
)

_CUA_PUBLIC_NAME_RE = re.compile(r"relay_cua_[A-Za-z0-9_]+")


def is_cua_public_name(value: object) -> bool:
    return isinstance(value, str) and bool(_CUA_PUBLIC_NAME_RE.fullmatch(value))


def cua_public_name(tool_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", tool_name):
        raise ValueError("invalid CUA tool name")
    return f"relay_cua_{tool_name}"


def profile_public_names(level: CuaAccessLevel) -> tuple[str, ...]:
    try:
        return CUA_PROFILE_PUBLIC_NAMES[level]
    except KeyError as exc:
        raise ValueError(f"unknown CUA access level: {level}") from exc


def cua_access_for_allowlist(allowlist: tuple[str, ...] | list[str]) -> ReportedCuaAccess:
    """Classify the exact ordered CUA portion of an allowlist.

    The non-CUA portion is intentionally ignored.  Profile matching is
    sequence-based so manually reordering a profile is reported as ``custom``
    and the stable YAML order can be restored by selecting a profile again.
    """

    cua_names = tuple(name for name in allowlist if is_cua_public_name(name))
    if not cua_names:
        return "none"
    for level in ("standard", "full"):
        if cua_names == CUA_PROFILE_PUBLIC_NAMES[level]:
            return level
    return "custom"


__all__ = [
    "ALL_PROFILE_PUBLIC_NAMES",
    "CUA_PROFILE_PUBLIC_NAMES",
    "CUA_PROFILE_TOOL_NAMES",
    "CuaAccessLevel",
    "FULL_CUA_TOOL_NAMES",
    "ReportedCuaAccess",
    "STANDARD_CUA_TOOL_NAMES",
    "cua_access_for_allowlist",
    "cua_public_name",
    "is_cua_public_name",
    "profile_public_names",
]
