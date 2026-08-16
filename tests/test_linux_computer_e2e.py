from __future__ import annotations

import importlib.util
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.mcp_client import EXPECTED_MCP_TOOLS

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "linux_computer_e2e.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DESKTOP_FIXTURE = ROOT / "tests" / "fixtures" / "desktop_app.py"
POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="requires POSIX AF_UNIX and symlink semantics",
)


def _load_harness():
    spec = importlib.util.spec_from_file_location("linux_computer_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cua_capabilities_are_unique_and_registry_sorted() -> None:
    harness = _load_harness()

    assert len(harness.CUA_CAPABILITIES) == len(set(harness.CUA_CAPABILITIES))
    assert harness.CUA_CAPABILITIES == tuple(sorted(harness.CUA_CAPABILITIES))


def test_cua_agent_tool_order_matches_public_mcp_contract() -> None:
    harness = _load_harness()

    expected = tuple(
        name for name in EXPECTED_MCP_TOOLS if name != "relay_device_status"
    )
    assert harness.CUA_AGENT_TOOLS == expected


def test_resolve_chromium_preserves_symlinked_launcher(tmp_path, monkeypatch) -> None:
    harness = _load_harness()
    target = tmp_path / "snap"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    launcher = tmp_path / "chromium"
    launcher.symlink_to(target)
    monkeypatch.setattr(harness.shutil, "which", lambda _name: str(launcher))

    assert harness._resolve_chromium() == launcher


def test_resolve_chromium_prefers_non_snap_google_chrome(tmp_path, monkeypatch) -> None:
    harness = _load_harness()
    chrome = tmp_path / "google-chrome-stable"
    snap = tmp_path / "chromium"
    for path in (chrome, snap):
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)

    monkeypatch.setattr(
        harness.shutil,
        "which",
        lambda name: str(chrome if name == "google-chrome-stable" else snap),
    )

    assert harness._resolve_chromium() == chrome



@POSIX_ONLY
def test_snap_chromium_uses_host_user_bus_for_snapd_scope(
    tmp_path,
) -> None:
    harness = _load_harness()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    bus = runtime / "bus"
    environment = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/private/bus"}

    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(bus))
        result = harness.chromium_environment(
            Path("/snap/bin/chromium"),
            environment,
            host_runtime_dir=runtime,
            host_session_bus_address=None,
        )

    assert result["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={bus}"
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/private/bus"


def test_snap_chromium_prefers_host_bus_address_over_private_bus(tmp_path) -> None:
    harness = _load_harness()
    private_bus = tmp_path / "private-bus"
    private_bus.mkdir()
    host_bus_address = "unix:path=/run/user/1001/bus"
    environment = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/private/bus"}

    result = harness.chromium_environment(
        Path("/snap/bin/chromium"),
        environment,
        host_runtime_dir=private_bus,
        host_session_bus_address=host_bus_address,
    )

    assert result["DBUS_SESSION_BUS_ADDRESS"] == host_bus_address
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/private/bus"


@POSIX_ONLY
def test_snap_chromium_detection_follows_launcher_symlink(tmp_path, monkeypatch) -> None:
    harness = _load_harness()
    snap_bin = tmp_path / "snap" / "bin"
    snap_bin.mkdir(parents=True)
    target = snap_bin / "chromium"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    launcher = tmp_path / "usr" / "bin" / "chromium"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(target)
    monkeypatch.setattr(harness, "SNAP_CHROMIUM_BIN_DIR", snap_bin)

    assert harness._is_snap_chromium_launcher(launcher)


def test_cua_snapshot_diagnostic_is_preserved_in_stderr_hint(tmp_path) -> None:
    harness = _load_harness()
    diagnostic = tmp_path / "agent.stderr.log"
    diagnostic.write_text(
        "[DEBUG] computer CUA get_window_state rejected: "
        "reason=window-state-shape elements=4 field_roles=0 "
        "button_roles=0 name_labels=0 apply_labels=0\n",
        encoding="utf-8",
    )

    hint = harness._stderr_hint(diagnostic)

    assert hint is not None
    assert "get_window_state rejected" in hint
    assert "elements=4" in hint


def test_non_snap_chromium_keeps_private_bus(tmp_path) -> None:
    harness = _load_harness()
    environment = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/private/bus"}

    result = harness.chromium_environment(
        Path("/usr/bin/chromium"),
        environment,
        host_runtime_dir=tmp_path,
        host_session_bus_address="unix:path=/run/user/1001/bus",
    )

    assert result == environment


def test_enable_chromium_accessibility_sets_both_session_flags(monkeypatch) -> None:
    harness = _load_harness()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return None

    monkeypatch.setattr(harness.subprocess, "run", fake_run)

    assert harness._enable_chromium_accessibility({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus"})
    assert [call[0][-2] for call in calls] == ["ScreenReaderEnabled", "IsEnabled"]
    assert all(call[0][-1] == "<true>" for call in calls)
    assert all(call[0][0:3] == ["gdbus", "call", "--session"] for call in calls)


def test_linux_cua_waits_for_matching_x11_title_and_class(monkeypatch) -> None:
    harness = _load_harness()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[-2:] == [
            "--class",
            f"^{harness.re.escape(harness.COMPUTER_APP_NAME)}$",
        ]:
            return SimpleNamespace(returncode=0, stdout="0x42\n")
        if command[-2:] == [
            "--name",
            f"^{harness.re.escape(harness.COMPUTER_WINDOW_TITLE)}$",
        ]:
            return SimpleNamespace(returncode=0, stdout="0x42\n")
        raise AssertionError(command)

    monkeypatch.setattr(harness.subprocess, "run", fake_run)

    assert harness._x11_has_expected_window({"DISPLAY": ":91"})
    assert [command[-2] for command in calls] == ["--name", "--class"]


def test_linux_cua_controls_readiness_uses_public_snapshot_oracle(monkeypatch) -> None:
    harness = _load_harness()
    calls = []
    runtime = SimpleNamespace(
        mcp_url="http://127.0.0.1:9000/mcp",
        control_token="control-token",
    )

    def fake_call_tool(url, token, tool_name, arguments, **kwargs):
        calls.append((url, token, tool_name, arguments, kwargs))
        return object()

    monkeypatch.setattr(harness.portable_mcp, "call_tool", fake_call_tool)
    monkeypatch.setattr(
        harness.portable_oracles,
        "validate_cua_list_windows",
        lambda result, **kwargs: (41, 7),
    )
    monkeypatch.setattr(
        harness.portable_oracles,
        "validate_cua_window_state",
        lambda result, **kwargs: ("snapshot", "field", "button"),
    )

    assert harness._cua_controls_ready(runtime)
    assert [item[2] for item in calls] == [
        "relay_cua_list_windows",
        "relay_cua_get_window_state",
    ]
    assert calls[1][3]["pid"] == 41
    assert calls[1][3]["window_id"] == 7


def test_linux_cua_uses_production_configuration_and_fixture() -> None:
    harness = _load_harness()
    source = SCRIPT.read_text(encoding="utf-8")
    assert harness.COMPUTER_APP_NAME == "relay-desktop-fixture"
    assert harness.COMPUTER_WINDOW_TITLE == "Relay Desktop Fixture"
    assert 'DESKTOP_FIXTURE = ROOT / "tests" / "fixtures" / "desktop_app.py"' in source
    for key in (
        "RELAY_AGENT_COMPUTER_ALLOWED_APP_NAME",
        "RELAY_AGENT_COMPUTER_ALLOWED_WINDOW_TITLE",
    ):
        assert key in source
    assert "expected_cua_app=COMPUTER_APP_NAME" in source
    assert "expected_cua_window_title=COMPUTER_WINDOW_TITLE" in source
    assert "--window-name=Relay Desktop Fixture" in source


def test_linux_cua_ci_job_invokes_public_runtime_gate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "  e2e-linux-cua:" in workflow
    job = workflow.split("  e2e-linux-cua:", 1)[1].split(
        "\n  e2e-windows-terminal:", 1
    )[0]
    assert "name: Linux CUA end-to-end" in job
    assert "runs-on: ubuntu-24.04" in job
    assert "uses: ./.github/actions/setup-python" in job
    assert ("profile: " + "cua") not in job
    assert "xvfb" in job.lower()
    assert "at-spi" in job.lower()
    assert "uv run --frozen python scripts/linux_computer_e2e.py" in job
    assert "google-chrome-stable --version" in job
    assert "include_browser=True" in SCRIPT.read_text(encoding="utf-8")
    assert "python scripts/validate_e2e_evidence.py" in job
    assert "--profile linux-cua" in job
    assert "docker" not in job.lower()
    assert "spikes/computer-use-xvfb" not in job
    assert "if: always()" in job


def test_linux_cua_starts_driver_before_chromium() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.index('phase = "agent-start"') < source.index('phase = "chromium-start"')
    assert '"ACCESSIBILITY_ENABLED": "1"' in source
    assert '"NO_AT_BRIDGE": "0"' in source


def test_linux_cua_evidence_policy_is_externalized() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("  e2e-linux-cua:", 1)[1].split(
        "\n  e2e-windows-terminal:", 1
    )[0]

    assert "python scripts/validate_e2e_evidence.py" in job
    assert "--profile linux-cua" in job
    assert "computer-events.jsonl" not in job
