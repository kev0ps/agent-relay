from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_windows_installer_is_a_self_contained_user_scope_bootstrapper() -> None:
    installer = (ROOT / "scripts" / "install.ps1").read_text()

    for phrase in (
        "Set-StrictMode -Version Latest",
        "codeload.github.com/kev0ps/agent-relay",
        "python install",
        "uv tool install",
        '"config", "init", "server"',
        'config init agent --stdin --no-tools',
        "AGENT_RELAY_SETUP",
    ):
        assert phrase in installer

    assert "--id=astral-sh.uv" in installer
    assert "RELAY_AGENT_TOKEN=" not in installer
    assert "Write-Host $agentToken" not in installer


def test_linux_installer_is_a_self_contained_user_scope_bootstrapper() -> None:
    installer = (ROOT / "scripts" / "install.sh").read_text()

    for phrase in (
        "set -euo pipefail",
        "codeload.github.com/kev0ps/agent-relay",
        "python install",
        "uv_path",
        "tool install --force",
        "config init server",
        "config init agent --stdin --no-tools",
        "AGENT_RELAY_SETUP",
    ):
        assert phrase in installer

    assert "RELAY_AGENT_TOKEN=" not in installer
    assert "printf '%s' \"$agent_token\"" not in installer


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
