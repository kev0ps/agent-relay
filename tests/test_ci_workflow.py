from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
SETUP = ROOT / ".github/actions/setup-python/action.yml"


def test_ci_is_read_only_and_checkouts_drop_credentials() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    assert workflow["permissions"] == {"contents": "read"}

    checkouts = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkouts
    assert all(
        step.get("with", {}).get("persist-credentials") is False
        for step in checkouts
    )


def test_external_github_actions_are_pinned_to_commits() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    setup = yaml.safe_load(SETUP.read_text(encoding="utf-8"))
    actions = [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]
    actions.extend(
        step["uses"]
        for step in setup["runs"]["steps"]
        if "uses" in step
    )

    external_actions = [action for action in actions if not action.startswith("./")]
    assert external_actions
    assert all(
        re.fullmatch(r"[^@\s]+@[0-9a-fA-F]{40}", action)
        for action in external_actions
    )


def test_dependabot_does_not_enable_automatic_merges() -> None:
    dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
    assert "automerge" not in dependabot.casefold()
