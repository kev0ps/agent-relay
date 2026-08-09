import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "relative_path",
    [
        "README.md",
        "LICENSE",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "docs/security.md",
        "docs/run-linux.md",
        "docs/run-server-docker.md",
        "docs/protocol.md",
        "docs/tools.md",
        "docs/e2e.md",
    ],
)
def test_public_repository_document_exists(relative_path: str) -> None:
    assert (ROOT / relative_path).is_file()


@pytest.mark.parametrize(
    "relative_path",
    [
        "docs/ROADMAP.md",
        "docs/protocol-v1.md",
        "docs/protocol-v2.md",
        "docs/cua-driver-tools-50.md",
        "docs/e2e-client-capabilities.md",
        "docs/run-windows-e2e.md",
        "docs/run-windows-browser-e2e.md",
        "docs/run-windows-computer-e2e.md",
    ],
)
def test_superseded_document_is_absent(relative_path: str) -> None:
    assert not (ROOT / relative_path).exists()


def test_repository_does_not_publish_a_speculative_roadmap() -> None:
    assert not (ROOT / "docs/plans").exists()
    readme = (ROOT / "README.md").read_text()
    assert "not an MVP" in readme
    assert "rather than a published roadmap" in readme


def test_hermes_is_an_ignored_internal_directory() -> None:
    assert ".hermes/" in (ROOT / ".gitignore").read_text().splitlines()


def test_package_metadata_declares_mit_license() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert metadata["license"] == "MIT"
    assert metadata["license-files"] == ["LICENSE"]


@pytest.mark.parametrize("relative_path", ["README.md", "docs/run-linux.md"])
def test_hermes_examples_disable_parallel_tool_calls(relative_path: str) -> None:
    text = (ROOT / relative_path).read_text()
    snippet = next(
        block.split("```", 1)[0]
        for block in text.split("```yaml")[1:]
        if "mcp_servers:" in block
    )

    assert "    supports_parallel_tool_calls: false\n" in snippet
    if relative_path == "README.md":
        assert "    tools:\n" not in snippet
    else:
        assert "    tools:\n      include:\n" in snippet
        assert "    tools:\n      - relay_device_status\n" not in snippet


def test_protocol_documents_actual_wire_version_split() -> None:
    protocol = (ROOT / "docs/protocol.md").read_text()

    for phrase in (
        "register(version=1)",
        "capabilities(version=1)",
        "heartbeat(version=2)",
        "invoke(version=2)",
        "result | error(version=2)",
        "POST /v2/devices/{device_id}/invoke",
        "Provider results",
    ):
        assert phrase in protocol


def test_tools_document_contains_complete_tested_allowlist() -> None:
    tools = (ROOT / "docs/tools.md").read_text()
    expected = (
        "relay_system_ping",
        "relay_terminal_exec",
        "relay_browser_list_tabs",
        "relay_browser_navigate",
        "relay_browser_snapshot",
        "relay_browser_fill",
        "relay_browser_click",
        "relay_browser_scroll",
        "relay_browser_type",
        "relay_browser_back",
        "relay_cua_list_windows",
        "relay_cua_get_window_state",
        "relay_cua_click",
        "relay_cua_type_text",
    )

    complete_block = tools.split("## Complete tested allowlist", 1)[1].split(
        "## Copyable profiles", 1
    )[0]
    for name in expected:
        assert f"      - {name}\n" in complete_block

    assert "relay_device_status" in tools
    assert "deliberately absent" in tools
    assert "tools/list" in tools


def test_e2e_document_covers_linux_windows_and_docker_boundaries() -> None:
    contract = (ROOT / "docs/e2e.md").read_text()

    for phrase in (
        "official MCP client -> Relay Server -> WebSocket -> Relay Agent",
        "structured MCP result",
        "independent fixture",
        "rejected before dispatch sends no WebSocket `invoke`",
        "exactly one terminal result or error unless it is cancelled",
        "allocates no Relay request ID",
        "personal browser profile",
        "e2e-linux-native",
        "e2e-linux-browser",
        "e2e-linux-cua",
        "e2e-windows-native",
        "e2e-windows-browser",
        "e2e-windows-cua",
        "Docker image smoke",
        "Windows CUA remains experimental",
    ):
        assert phrase in contract


def test_readme_and_linux_guide_document_shared_agent_token_flow() -> None:
    for relative_path in ("README.md", "docs/run-linux.md"):
        text = (ROOT / relative_path).read_text()
        assert "config init server" in text
        assert "config init agent --stdin --no-tools" in text
        assert "secrets/server/agent_token" in text

    readme = (ROOT / "README.md").read_text()
    assert ".agent-relay-state/mcp.token" not in readme
    assert "uv run --frozen agent-relay server" in readme
    assert "uv run --frozen agent-relay agent" in readme
    assert "Coverage: 83%" not in readme


def test_linux_guide_explains_bind_default_and_windows_cua_status() -> None:
    guide = (ROOT / "docs/run-linux.md").read_text()

    assert "network-capable default bind" in guide
    assert "0.0.0.0:8000" in guide
    assert "change it to loopback" in guide
    assert "hosted candidate job" in guide
    assert "remains outside hosted CI" not in guide


def test_docker_guide_requires_an_exact_reviewed_revision() -> None:
    guide = (ROOT / "docs/run-server-docker.md").read_text()

    assert "git checkout --detach <REVIEWED-COMMIT-SHA>" in guide
    assert "git rev-parse HEAD" in guide
    assert "git pull --ff-only origin main" not in guide
