from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("relative_path", ["README.md", "LICENSE", "SECURITY.md"])
def test_essential_public_document_exists(relative_path: str) -> None:
    assert (ROOT / relative_path).is_file()


def test_e2e_docs_describe_the_shared_cross_platform_chrome_scenario() -> None:
    e2e = (ROOT / "docs/e2e.md").read_text(encoding="utf-8")
    windows = (ROOT / "docs/run-windows.md").read_text(encoding="utf-8")
    linux = (ROOT / "docs/run-linux.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "shared browser scenario" in e2e
    assert "scripts/e2e/fixtures/cua/index.html" in e2e
    assert "preinstalled Google Chrome" in e2e
    assert "Linux and Windows" in e2e
    assert "Windows CUA candidate" not in e2e
    assert "Windows keeps the desktop candidate path" not in e2e
    assert "Windows CUA remains experimental" not in windows
    assert "Windows CUA has a hosted candidate job" not in linux
    assert "repeatable Windows CUA evidence" not in readme
    assert "experimental Windows CUA candidate gate" not in changelog
