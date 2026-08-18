from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def test_windows_installer_is_a_self_contained_user_scope_bootstrapper() -> None:
    installer = (ROOT / "scripts" / "install.ps1").read_text()

    for phrase in (
        "Set-StrictMode -Version Latest",
        "codeload.github.com/kev0ps/agent-relay",
        "--source winget",
        "python install",
        "uv tool install",
        '"config", "init", "server"',
        '"agent", "--from-server", "--no-tools"',
        '"onboard", "--role", "server", "--non-interactive"',
        "AGENT_RELAY_SETUP",
    ):
        assert phrase in installer

    assert "--id=astral-sh.uv" in installer
    assert "RELAY_AGENT_TOKEN=" not in installer
    assert "Write-Host $agentToken" not in installer
    assert "secrets\\server\\agent_token" not in installer
    assert '"3.14.4"' in installer


def _windows_function(name: str, next_name: str) -> str:
    installer = (ROOT / "scripts" / "install.ps1").read_text()
    marker = f"function {name}"
    return marker + installer.split(marker, 1)[1].split(f"function {next_name}", 1)[0]


def _windows_path_refresh_function() -> str:
    return _windows_function("Refresh-ProcessPath", "Ensure-Uv")


def test_windows_path_refresh_includes_the_current_process_path() -> None:
    refresh_function = _windows_path_refresh_function()

    assert "$processPath = $env:Path" in refresh_function
    assert "@($processPath, $userPath, $machinePath)" in refresh_function


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows PowerShell")
def test_windows_path_refresh_preserves_session_entries(tmp_path: Path) -> None:
    refresh_function = _windows_path_refresh_function()
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    assert powershell is not None

    probe = tmp_path / "path-refresh.ps1"
    probe.write_text(
        refresh_function
        + """
$marker = "C:\\agent-relay-session-only"
$env:Path = "$marker;$env:Path"
Refresh-ProcessPath
if (-not (($env:Path -split ";") -contains $marker)) {
    throw "Refresh-ProcessPath discarded the current process PATH."
}
""",
        encoding="utf-8",
    )

    subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_windows_installer_detects_existing_agent_section_without_a_fixed_secret_path() -> None:
    installer = (ROOT / "scripts" / "install.ps1").read_text()

    assert "config get agent" in installer
    assert "function Get-AgentConfigurationState" in installer
    assert "Get-AgentConfigurationState $script:agentRelayCommand" in installer
    assert "agent-relay: error: agent configuration is not initialized" in installer
    assert "Could not inspect existing Agent configuration" in installer
    assert "$agentConfigMarker" not in installer


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows PowerShell")
def test_windows_agent_configuration_probe_fails_closed(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    assert powershell is not None

    command = tmp_path / "agent-relay-probe.cmd"
    command.write_bytes(
        b"@echo off\r\n"
        b'if "%AGENT_RELAY_TEST_PROBE%"=="present" exit /b 0\r\n'
        b'if "%AGENT_RELAY_TEST_PROBE%"=="missing" (\r\n'
        b"  echo agent-relay: error: agent configuration is not initialized 1>&2\r\n"
        b"  exit /b 1\r\n"
        b")\r\n"
        b"echo unexpected Agent configuration probe failure 1>&2\r\n"
        b"exit /b 42\r\n"
    )
    command_literal = str(command).replace("'", "''")
    probe = tmp_path / "agent-configuration-state.ps1"
    probe.write_text(
        _windows_function("Get-AgentConfigurationState", "Invoke-AgentRelay")
        + f"""
$commandPath = '{command_literal}'
$env:AGENT_RELAY_TEST_PROBE = "present"
if ((Get-AgentConfigurationState $commandPath) -ne "present") {{
    throw "Existing Agent configuration was not detected."
}}
$env:AGENT_RELAY_TEST_PROBE = "missing"
if ((Get-AgentConfigurationState $commandPath) -ne "missing") {{
    throw "Missing Agent configuration was not detected."
}}
$env:AGENT_RELAY_TEST_PROBE = "unexpected"
$failedClosed = $false
try {{
    Get-AgentConfigurationState $commandPath | Out-Null
}} catch {{
    if ($_.Exception.Message -notlike "Could not inspect existing Agent configuration*") {{
        throw
    }}
    $failedClosed = $true
}}
if (-not $failedClosed) {{
    throw "Unexpected Agent probe failure did not fail closed."
}}
""",
        encoding="utf-8",
    )

    subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_linux_installer_is_a_self_contained_user_scope_bootstrapper() -> None:
    installer = (ROOT / "scripts" / "install.sh").read_text()

    for phrase in (
        "set -euo pipefail",
        "codeload.github.com/kev0ps/agent-relay",
        "python install",
        "uv_path",
        "tool install --force",
        "config init server",
        "config init agent --from-server --no-tools",
        "invoke_agent_relay onboard --role server --non-interactive",
        "AGENT_RELAY_SETUP",
    ):
        assert phrase in installer

    assert "RELAY_AGENT_TOKEN=" not in installer
    assert "printf '%s' \"$agent_token\"" not in installer
    assert "secrets/server/agent_token" not in installer
    assert 'AGENT_RELAY_PYTHON_VERSION:-3.14.4' in installer


def test_installers_request_cua_only_for_local_or_agent_roles() -> None:
    linux = (ROOT / "scripts" / "install.sh").read_text()
    windows = (ROOT / "scripts" / "install.ps1").read_text()

    assert 'if [[ "$setup_mode" == "local" || "$setup_mode" == "agent" ]]' in linux
    assert 'tool_target="${project_root}[cua]"' in linux
    assert 'if ($setupMode -in @("local", "agent"))' in windows
    assert '$toolTarget = "$projectRoot[cua]"' in windows
    assert "cua-driver==0.19.3" not in linux
    assert "cua-driver==0.19.3" not in windows


@pytest.mark.skipif(sys.platform != "linux", reason="requires the Linux installer")
def test_linux_installer_rejects_missing_archive_override_without_network(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "curl",
        '#!/bin/sh\nprintf called > "$NETWORK_MARKER"\nexit 77\n',
    )
    home = tmp_path / "home"
    home.mkdir()
    network_marker = tmp_path / "network-called"
    missing_archive = tmp_path / "missing.tar.gz"
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "NETWORK_MARKER": str(network_marker),
        "AGENT_RELAY_ARCHIVE_SOURCE": str(missing_archive),
        "AGENT_RELAY_SETUP": "skip",
        "AGENT_RELAY_SKIP_PATH_UPDATE": "1",
    }

    completed = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "install.sh")],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    assert completed.returncode != 0
    assert not network_marker.exists()
    assert "AGENT_RELAY_ARCHIVE_SOURCE is not a file" in completed.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="requires the Linux installer")
@pytest.mark.parametrize(
    ("probe_exit", "probe_output", "installer_succeeds", "initializes_agent"),
    [
        (0, "", True, False),
        (1, "agent-relay: error: agent configuration is not initialized", True, True),
        (42, "unexpected Agent configuration probe failure", False, False),
    ],
)
def test_linux_installer_handles_agent_configuration_probe_safely(
    tmp_path: Path,
    probe_exit: int,
    probe_output: str,
    installer_succeeds: bool,
    initializes_agent: bool,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "uv",
        '#!/bin/sh\nif [ "$*" = "tool dir --bin" ]; then\n    printf "%s\\n" "$HOME/.local/bin"\nfi\nexit 0\n',
    )

    home = tmp_path / "home"
    tool_bin = home / ".local" / "bin"
    tool_bin.mkdir(parents=True)
    calls = tmp_path / "agent-relay-calls"
    _write_executable(
        tool_bin / "agent-relay",
        """#!/bin/sh
printf "%s\\n" "$*" >> "$AGENT_RELAY_TEST_CALLS"
if [ "$*" = "config get agent" ]; then
    if [ -n "$AGENT_RELAY_TEST_PROBE_OUTPUT" ]; then
        printf "%s\\n" "$AGENT_RELAY_TEST_PROBE_OUTPUT" >&2
    fi
    exit "$AGENT_RELAY_TEST_PROBE_EXIT"
fi
exit 0
""",
    )
    config_dir = home / ".agent-relay"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "agent:\n  secrets:\n    agent_token_file: custom/agent-token\n",
        encoding="utf-8",
    )
    server_secret_dir = config_dir / "secrets" / "server"
    server_secret_dir.mkdir(parents=True)
    (server_secret_dir / "agent_token").write_text("server-agent-token\n", encoding="utf-8")

    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "AGENT_RELAY_PROJECT_ROOT": str(ROOT),
        "AGENT_RELAY_SETUP": "local",
        "AGENT_RELAY_SKIP_PATH_UPDATE": "1",
        "AGENT_RELAY_TEST_CALLS": str(calls),
        "AGENT_RELAY_TEST_PROBE_EXIT": str(probe_exit),
        "AGENT_RELAY_TEST_PROBE_OUTPUT": probe_output,
    }

    completed = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "install.sh")],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    assert (completed.returncode == 0) is installer_succeeds, completed.stdout
    invocations = calls.read_text(encoding="utf-8").splitlines()
    assert "config get agent" in invocations
    assert any(call.startswith("config init agent") for call in invocations) is initializes_agent


def test_windows_install_guide_matches_the_hosted_script_flow() -> None:
    guide = (ROOT / "docs" / "run-windows.md").read_text()

    for phrase in (
        "iex (irm https://raw.githubusercontent.com/kev0ps/agent-relay",
        "scripts/install.ps1",
        "AGENT_RELAY_REF",
        "allowlist",
        "Windows CUA remains experimental",
    ):
        assert phrase in guide


def test_windows_install_guide_documents_the_linux_counterpart() -> None:
    guide = (ROOT / "docs" / "run-windows.md").read_text()

    assert "curl -fsSL https://raw.githubusercontent.com/kev0ps/agent-relay" in guide
    assert "scripts/install.sh" in guide
