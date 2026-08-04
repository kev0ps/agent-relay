# Native Windows CUA E2E

## Status

`e2e-windows-cua` is an **experimental candidate gate** on the GitHub-hosted
`windows-2025` runner. Its presence in `.github/workflows/ci.yml` is not a claim
that Windows CUA is currently supported.

The gate must prove the Agent Relay Server, Agent and public MCP path. A direct
driver or environment check is insufficient.

## Scope

The gate runs this native topology without Docker:

```text
official MCP client
  -> native Relay Server on loopback
  -> authenticated WebSocket
  -> native Windows Relay Agent
  -> pinned cua-driver / Windows UI Automation
  -> synthetic WinForms fixture
```

It rejects Session 0, creates separate temporary credentials and a temporary
workspace, pins the driver installer source and release, and owns the Server,
Agent, driver and fixture processes through a Windows Job Object.

The required scenario is:

```text
tools/list -> window inventory -> snapshot -> type -> click -> independent fixture event -> cleanup
```

The scenario also validates status, the selected descriptor inventory,
provider snapshot-token refresh, stale-element rejection and exact app/window identity. A successful driver
response alone is not sufficient; the fixture must emit exactly one correlated
`applied` event.

## Evidence

The job permits only these bounded files:

- `output.log`;
- `computer-events.jsonl`;
- `success.json`.

`success.json` is written only after the scenario and owned-process cleanup
succeed. On success, `computer-events.jsonl` must contain exactly one closed
record with `event`, `run_id` and `value`. Reparse points, extra files, oversized
files and success markers after failure are rejected.

## Interpretation

Windows CUA remains experimental until the complete sequence passes
repeatably on the declared hosted runner and the evidence is tied to the exact
reviewed commit. Starting the runner, installing the driver, opening the
fixture, receiving a direct driver response or reaching Agent registration does
not close this gate.

If the hosted runner cannot provide a repeatable interactive session or the
complete fixture-backed sequence fails, classify Windows CUA as
experimental or unsupported. Do not replace the missing proof with Browser
CDP, loopback core MCP, Docker, a skipped job or a self-hosted fallback in the
current CI scope.

## Local contract checks

The runtime harness intentionally refuses non-Windows hosts. Its command,
evidence and lifecycle contracts can be checked locally with:

```sh
uv run --frozen pytest -q \
  tests/test_windows_computer_e2e.py \
  tests/test_windows_e2e.py \
  tests/test_runner.py \
  -k "not git_search_skips_relative_default_path_entries"
uv run --frozen ruff check .
```

The actual runtime command and pinned driver installation are defined in the
`e2e-windows-cua` job in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
