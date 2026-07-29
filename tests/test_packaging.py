from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_docker_runtime_contract_has_no_implicit_role_or_secret_build_inputs() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    instructions = [line.strip() for line in dockerfile.splitlines() if line.strip()]

    assert any(line == 'ENTRYPOINT ["agent-relay"]' for line in instructions)
    assert any(line == "USER relay" for line in instructions)
    assert any(line == "WORKDIR /workspace" for line in instructions)
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "ca-certificates" in dockerfile
    assert re.search(r"\bapt-get install\b[^\n]*\bgit\b", dockerfile)
    assert "uv sync --frozen" in dockerfile
    assert "FROM python:3.13.5-slim-bookworm AS builder" in dockerfile
    assert "FROM python:3.13.5-slim-bookworm AS runtime" in dockerfile
    assert "COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/" in dockerfile
    assert not any(line.startswith("EXPOSE ") for line in instructions)
    assert not any(
        re.match(r"(?:ARG|ENV)\s+.*(?:SECRET|TOKEN|PASSWORD|API_KEY)", line, re.I)
        for line in instructions
    )


def test_docker_builder_copies_package_metadata_before_project_install() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    metadata_copy = "COPY README.md LICENSE ./"
    project_install = "RUN uv sync --frozen --no-dev --no-editable"

    assert dockerfile.count(metadata_copy) == 1
    assert dockerfile.count(project_install) == 1
    assert dockerfile.index(metadata_copy) < dockerfile.index(project_install)


def test_docker_build_context_keeps_lockfiles_and_excludes_local_state() -> None:
    ignored = set((ROOT / ".dockerignore").read_text().splitlines())

    assert {".git", ".env", ".venv", "tests/"} <= ignored
    assert "pyproject.toml" not in ignored
    assert "uv.lock" not in ignored
