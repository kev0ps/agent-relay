"""Windows interactive desktop-session primitives for native CUA E2E."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

from ..common import E2EError
from ..terminal import ProcessPlatform


def current_session_id() -> int:
    if os.name != "nt":
        raise E2EError("Windows graphical session requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ProcessIdToSessionId.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.ProcessIdToSessionId.restype = ctypes.c_int
    session_id = ctypes.c_uint32()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
        raise E2EError("could not inspect Windows session")
    return int(session_id.value)


class WindowsGraphicalSession:
    """Validate and expose the hosted runner's interactive desktop."""

    def prepare(
        self,
        platform: ProcessPlatform,
        *,
        root: Path,
        home: Path,
        repository: Path,
    ) -> dict[str, str]:
        del root, repository
        if current_session_id() == 0:
            raise E2EError("Windows runner is in Session 0")
        values = {
            "CUA_DRIVER_TELEMETRY": "0",
            "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
        }
        if driver_home := os.environ.get("CUA_DRIVER_RS_HOME"):
            values["CUA_DRIVER_RS_HOME"] = driver_home
        return platform.minimal_environment(home, values)
