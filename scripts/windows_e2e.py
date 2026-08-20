#!/usr/bin/env python3
"""Run the canonical Windows Agent Relay Terminal E2E scenario."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

if __package__:
    from .e2e.platform.windows import WindowsProcessManager
    from .e2e.terminal import run_terminal_e2e
else:
    from e2e.platform.windows import WindowsProcessManager
    from e2e.terminal import run_terminal_e2e


def run_scenario(
    evidence_dir: Path | None = None,
    *,
    output_file: Path | None = None,
) -> None:
    run_terminal_e2e(
        WindowsProcessManager(),
        evidence_dir,
        output_file=output_file,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Windows Agent Relay E2E")
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
