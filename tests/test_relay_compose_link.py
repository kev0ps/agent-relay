from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "relay_compose_link.py"


def _load_harness() -> Any:
    spec = importlib.util.spec_from_file_location("relay_compose_link", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_agent_uses_installed_relay_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _load_harness()
    installed = str(tmp_path / "agent-relay")
    captured: dict[str, Any] = {}
    monkeypatch.setenv("RELAY_E2E_AGENT_RELAY_COMMAND", installed)

    def fake_popen(command: list[str], **kwargs: Any) -> object:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return object()

    monkeypatch.setattr(harness.subprocess, "Popen", fake_popen)

    harness._spawn_agent(tmp_path / "workspace", "agent-token", tmp_path / "home")

    assert captured["command"] == [installed, "agent"]
    assert "PYTHONPATH" not in captured["environment"]
