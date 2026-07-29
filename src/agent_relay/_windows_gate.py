"""Private Windows gate that starts a locally supplied fixed command on cue."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    """Wait for the parent release byte, then execute the fixed argv without a shell."""
    command = tuple(sys.argv[1:])
    if not command or sys.stdin.buffer.read(1) != b"\x01":
        return 125
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
