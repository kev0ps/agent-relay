from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from scripts import linux_computer_e2e, windows_computer_e2e


@pytest.mark.parametrize(
    ("entrypoint", "manager_name", "session_name"),
    [
        (linux_computer_e2e, "PosixProcessManager", "LinuxGraphicalSession"),
        (windows_computer_e2e, "WindowsProcessManager", "WindowsGraphicalSession"),
    ],
)
def test_cua_entrypoint_delegates_to_the_shared_browser_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entrypoint: ModuleType,
    manager_name: str,
    session_name: str,
) -> None:
    manager = object()
    session = object()
    calls: list[tuple[object, object, Path | None, Path | None]] = []
    monkeypatch.setattr(entrypoint, manager_name, lambda: manager)
    monkeypatch.setattr(entrypoint, session_name, lambda: session)
    monkeypatch.setattr(
        entrypoint,
        "run_cua_e2e",
        lambda actual_manager, actual_session, evidence_dir, *, output_file: calls.append(
            (actual_manager, actual_session, evidence_dir, output_file)
        ),
    )
    evidence = tmp_path / "evidence"
    output = evidence / "output.log"

    entrypoint.run_scenario(evidence, output_file=output)

    assert calls == [(manager, session, evidence, output)]
