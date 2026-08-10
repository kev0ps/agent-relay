# End-to-end validation

This document describes the developer-facing E2E gates for Linux and Windows.
They exercise Agent Relay through the authenticated public MCP surface and use
synthetic fixtures. They do not validate personal profiles, desktops, accounts,
or external websites.

## Required path and evidence

Every product-level scenario follows the same public path:

```text
official MCP client -> Relay Server -> WebSocket -> Relay Agent -> local capability
```

The harness must not invoke a provider directly or inject internal WebSocket
frames as a substitute. A mutating Browser or CUA scenario passes only when it
returns a valid structured MCP result and the independent fixture records
exactly one correlated event.

Artifacts and diagnostics are synthetic, sanitized, explicitly allowlisted,
size-bounded, regular files, and retained for a bounded period. A driver reply,
process exit code, unit test, screenshot, or LLM interpretation is not an
independent side-effect oracle.

## Request and fixture correlation

Each Agent-executed MCP call receives a fresh opaque Relay request ID. A call
rejected before dispatch sends no WebSocket `invoke`. Every call accepted for
dispatch has exactly one terminal result or error unless it is cancelled;
cancellation does not require or accept a late terminal result.

`relay_device_status` is Server-local. It allocates no Relay request ID and does
not traverse the Agent WebSocket.

Browser and CUA fixtures use a separate unpredictable run ID. A valid mutation
requires one matching fixture event produced after its MCP call begins and
before the bounded scenario deadline. Missing, duplicate, stale, pre-existing,
out-of-order, wrong-run, wrong-event, or wrong-value records fail the scenario.

Tests use temporary workspaces, credentials, profiles, displays, and fixture
state. They must not discover or reuse a personal browser profile, desktop,
workspace, credential, account, or external website. Processes and cleanup are
owned explicitly, and a failed cleanup cannot leave a passing marker.

## Current matrix

| Platform | Terminal | Browser | CUA | Current interpretation |
|---|---|---|---|---|
| Linux | E2E | Headless Chromium E2E | X11/Xvfb/AT-SPI E2E | Current repeatable CI paths |
| Windows | E2E | Headless Chromium E2E | Hosted UI Automation candidate | CUA remains experimental until the complete fixture-backed gate is repeatable |
| Docker image | CLI smoke only | Not validated | Not validated | Packaging evidence, not capability E2E |

The workflow definitions in `.github/workflows/ci.yml` are authoritative for
runner versions, installation commands, exact test selections, and uploaded
artifacts.

The Linux and Windows Terminal jobs use the platform installation bootstrapper
as their Agent Relay runtime path. The test harness dependencies remain locked
in the shared setup action, while the Server and Agent children are launched
from the user-installed `agent-relay` command.

## Linux Terminal

The `e2e-linux` job runs the official MCP client, Relay Server, and Relay
Agent as native processes on the hosted Linux runner. It verifies status, ping,
the fixed terminal commands, Agent stop/offline detection, reconnect, Server
restart, re-registration, and bounded process cleanup.

The successful evidence set contains only bounded `output.log` and
`success.json` files. This gate does not prove systemd installation, every Linux
distribution, Browser, or CUA.

Relevant local contract checks:

```sh
uv run --frozen pytest -q tests/test_linux_e2e.py tests/test_runner.py
uv run --frozen pytest -q -m integration
```

## Linux Browser

The `e2e-linux-browser` job starts a loopback web fixture, Relay Server, Relay
Agent, and a fresh Playwright Chromium persistent context. The Agent owns the
browser lifecycle and uses an ephemeral test-only profile.

The scenario covers tab listing, allowed navigation, snapshot, stale-locator
rejection, back, type, fill, scrolling, click, disallowed-origin rejection, and
exactly one independent fixture event.

Relevant local contract checks:

```sh
uv run --frozen pytest -q tests/test_linux_browser_e2e.py tests/test_browser_capability.py
```

## Linux CUA

The `e2e-linux-cua` job runs Xvfb, a private D-Bus/AT-SPI session, a small window
manager, Chromium, the pinned CUA provider, and the real Relay processes. The
selected public surface is:

```text
relay_cua_list_windows
relay_cua_get_window_state
relay_cua_click
relay_cua_type_text
```

The scenario verifies descriptor discovery, exact application/window identity,
snapshot-token refresh, stale-element rejection, type, click, one correlated
fixture event, and cleanup. It proves only the hosted X11/Xvfb/AT-SPI setup, not
Wayland, a personal desktop, or Windows UI Automation.

Relevant local contract checks:

```sh
uv run --frozen pytest -q tests/test_linux_computer_e2e.py tests/test_computer_capability.py tests/test_desktop_fixture.py
```

## Windows Terminal

The `e2e-windows-terminal` job runs the Server and Agent directly on the hosted
Windows runner. A Windows Job Object owns the process tree. The scenario covers
the same core MCP status, ping, terminal, stop, reconnect, restart, and cleanup
behavior as the Linux Terminal gate.

It does not use Docker, Browser, or CUA. A mixed Linux Server and Windows Agent
deployment is outside this gate.

Relevant portable contract checks:

```sh
uv run --frozen pytest -q tests/test_windows_e2e.py tests/test_runner.py
```

The runtime harness itself refuses non-Windows hosts.

## Windows Browser

The `e2e-windows-browser` job uses a native Relay Server and Agent, a loopback
fixture, and headless Playwright Chromium with an ephemeral persistent profile.
The processes are owned by a Windows Job Object, and the Browser scenario and
independent event contract match the Linux gate.

Local Windows invocation:

```powershell
uv sync --locked --extra browser
uv run --frozen playwright install chromium
uv run --frozen python scripts/windows_browser_e2e.py `
  --evidence-dir browser-evidence `
  --output-file browser-evidence/output.log
```

This does not prove headed rendering, personal profiles, external origins,
Computer Use, or a mixed-platform deployment.

## Windows CUA candidate

The `e2e-windows-cua` workflow job is an experimental candidate, not a support
claim. It attempts this complete path:

```text
tools/list -> window inventory -> snapshot -> type -> click
           -> one independent fixture event -> cleanup
```

The candidate uses the native Server and Agent, pinned CUA provider, Windows UI
Automation, and a synthetic fixture. It rejects Session 0 and requires exact
application/window identity, fresh snapshot tokens, stale-element rejection,
bounded evidence, and owned-process cleanup.

Starting the runner, installing the provider, registering the Agent, or
receiving a direct provider reply does not close the gate. Until the full
sequence passes repeatably for the reviewed commit, Windows CUA remains experimental.

Portable contract checks:

```sh
uv run --frozen pytest -q \
  tests/test_windows_computer_e2e.py \
  tests/test_windows_e2e.py \
  tests/test_runner.py \
  -k "not git_search_skips_relative_default_path_entries"
```

## Docker image smoke

The Docker matrix builds the production image for `linux/amd64` and
`linux/arm64`, checks its non-root user and entrypoint contract, then runs CLI
help/version smoke commands. It does not run Browser or CUA and must not be
reported as capability E2E evidence.
