#!/usr/bin/env python3
"""Run the canonical Linux Agent Relay browser CUA E2E scenario."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

if __package__:
    from .e2e.cua import run_cua_e2e
    from .e2e.platform.linux_graphics import LinuxGraphicalSession
    from .e2e.platform.posix import PosixProcessManager
else:
    from e2e.cua import run_cua_e2e
    from e2e.platform.linux_graphics import LinuxGraphicalSession
    from e2e.platform.posix import PosixProcessManager


def run_scenario(
    evidence_dir: Path | None = None,
    *,
    output_file: Path | None = None,
) -> None:
    run_cua_e2e(
        PosixProcessManager(),
        LinuxGraphicalSession(),
        evidence_dir,
        output_file=output_file,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--output-file", type=Path)
    args = parser.parse_args(argv)
    try:
        run_scenario(args.evidence_dir, output_file=args.output_file)
    except BaseException:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
