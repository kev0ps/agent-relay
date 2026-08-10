from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("relative_path", ["README.md", "LICENSE", "SECURITY.md"])
def test_essential_public_document_exists(relative_path: str) -> None:
    assert (ROOT / relative_path).is_file()
