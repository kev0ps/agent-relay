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
        "docs/ROADMAP.md",
    ],
)
def test_public_repository_document_exists(relative_path: str) -> None:
    assert (ROOT / relative_path).is_file()


def test_repository_uses_one_active_roadmap_without_dated_plans() -> None:
    assert not (ROOT / "docs/plans").exists()
    assert not (ROOT / ".hermes/plans").exists()
    assert "only active roadmap" in (ROOT / "docs/ROADMAP.md").read_text()


def test_package_metadata_declares_mit_license() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert metadata["license"] == "MIT"
    assert metadata["license-files"] == ["LICENSE"]


@pytest.mark.parametrize("relative_path", ["README.md", "docs/run-linux.md"])
def test_hermes_examples_use_tools_include_mapping(relative_path: str) -> None:
    text = (Path(__file__).parents[1] / relative_path).read_text()
    snippet = next(
        block.split("```", 1)[0]
        for block in text.split("```yaml")[1:]
        if "mcp_servers:" in block
    )

    assert "    supports_parallel_tool_calls: false\n" in snippet
    assert "    tools:\n      include:\n" in snippet
    assert "    tools:\n      - relay_device_status\n" not in snippet


CONTRACT_PATH = Path(__file__).parents[1] / "docs/e2e-client-capabilities.md"


def _contract_text() -> str:
    return CONTRACT_PATH.read_text()


def test_e2e_client_capability_contract_records_black_box_invariants() -> None:
    contract = _contract_text()

    for invariant in (
        "MCP -> Relay Server -> WebSocket -> Relay Agent -> local capability",
        "official MCP client",
        "no direct plugin invocation",
        "structured result plus independent fixture event",
        "same Relay Agent package",
        "no personal browser profile",
    ):
        assert invariant in contract


def test_e2e_request_correlation_distinguishes_dispatch_outcomes() -> None:
    contract = " ".join(_contract_text().split())

    for semantic_phrase in (
        "before dispatch validation",
        "rejected calls",
        "no WebSocket invoke",
        "accepted for dispatch",
        "terminal response or cancellation",
        "does not require or accept a terminal result",
        "server-local status",
        "does not allocate a Relay request ID",
    ):
        assert semantic_phrase in contract


def test_e2e_mcp_tool_inventory_identifies_execution_scope() -> None:
    inventory = _contract_text().split("## Independent fixture event contract", 1)[0]

    scoped_entries = (
        "server-local status",
        "`relay_device_status`",
        "agent-executed system ping",
        "`relay_system_ping`",
        "### Terminal",
        "### Browser Use",
        "### Computer Use",
    )
    positions = [inventory.index(entry) for entry in scoped_entries]

    assert positions == sorted(positions)


def test_e2e_contract_distinguishes_native_primary_from_docker_image_smoke() -> None:
    """The contract must not present Docker runtime UI as product evidence."""
    contract = " ".join(_contract_text().split())

    for phrase in (
        "e2e-linux-native",
        "primary Linux Terminal product E2E proof",
        "native-evidence",
        "success.json",
        "production-image build",
        "CLI smoke checks",
        "not treated as Browser or Computer Use product evidence",
    ):
        assert phrase in contract


def test_readme_and_linux_guide_do_not_promote_container_ui_e2e() -> None:
    """User-facing docs must distinguish image smoke from native UI proof."""
    root = Path(__file__).parents[1]
    readme = (root / "README.md").read_text()
    linux_guide = (root / "docs/run-linux.md").read_text()

    assert "AMD64/ARM64 image build and CLI smoke" in readme
    assert "container test bench" not in readme
    assert "AMD64 end-to-end container runs" not in readme
    assert "Docker image CI validation" in linux_guide
    assert "do not run Browser or" in linux_guide
    assert "CI-only two-container topology" not in linux_guide
