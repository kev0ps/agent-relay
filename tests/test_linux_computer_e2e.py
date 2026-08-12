from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "linux_computer_e2e.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DESKTOP_FIXTURE = ROOT / "tests" / "fixtures" / "desktop_app.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("linux_computer_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_resolve_chromium_preserves_symlinked_launcher(tmp_path, monkeypatch) -> None:
    harness = _load_harness()
    target = tmp_path / "snap"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    launcher = tmp_path / "chromium"
    launcher.symlink_to(target)
    monkeypatch.setattr(harness.shutil, "which", lambda _name: str(launcher))

    assert harness._resolve_chromium() == launcher


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
    assert "chromium --version" in job
    assert "include_browser=True" in SCRIPT.read_text(encoding="utf-8")
    assert "python scripts/validate_e2e_evidence.py" in job
    assert "--profile linux-cua" in job
    assert "docker" not in job.lower()
    assert "spikes/computer-use-xvfb" not in job
    assert "if: always()" in job


def test_linux_cua_evidence_policy_is_externalized() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("  e2e-linux-cua:", 1)[1].split(
        "\n  e2e-windows-terminal:", 1
    )[0]

    assert "python scripts/validate_e2e_evidence.py" in job
    assert "--profile linux-cua" in job
    assert "computer-events.jsonl" not in job
