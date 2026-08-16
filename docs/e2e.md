# End-to-end validation

This document describes the developer-facing E2E gates for Linux and Windows.
They exercise Agent Relay through the authenticated public MCP surface and use
synthetic fixtures. They do not validate personal desktops, accounts, or
external websites.

## Required path and evidence

Every product-level scenario follows the same path:

```text
official MCP client -> Relay Server -> WebSocket -> Relay Agent -> CUA
```

The harness must not invoke CUA directly or inject internal WebSocket frames as
a substitute. A mutating scenario passes only when it returns a valid
structured MCP result and the independent fixture records exactly one
correlated event.

Each Agent-executed call receives a fresh opaque Relay request ID. A call
rejected before dispatch sends no WebSocket `invoke`. Every call accepted for
dispatch has exactly one terminal result or error unless it is cancelled.

Fixtures use an unpredictable run ID. A valid mutation requires one matching
fixture event produced after its MCP call begins and before the bounded
scenario deadline. Missing, duplicate, stale, wrong-run, wrong-event, and
wrong-value records fail the scenario. Tests use temporary workspaces,
credentials, displays, and fixture state; they never reuse a personal desktop
or browser session.

## Current matrix

| Platform | Terminal | CUA operations | Interpretation |
|---|---|---|---|
| Linux | E2E | X11/Xvfb/AT-SPI desktop + local browser subpath | CI-gated native path; local contract tests are not product E2E proof |
| Windows | E2E | Hosted UI Automation desktop candidate | CUA remains experimental until the full fixture gate is repeatable |
| Docker image | CLI smoke only | Not a product CUA path | Packaging evidence, not capability E2E |

The workflow definitions in `.github/workflows/ci.yml` are authoritative for
runner versions, installation commands, exact test selections, and artifacts.

## Linux Terminal

The `e2e-linux` job runs the official MCP client, Relay Server, and Relay Agent
as native processes. It verifies status, ping, fixed terminal commands,
offline detection, reconnect, Server restart, re-registration, and bounded
cleanup.

Relevant local checks:

```sh
uv run --frozen python -m pytest -q \
  tests/test_linux_e2e.py tests/test_windows_e2e.py tests/test_runner.py
uv run --frozen python -m pytest -q -m integration
```

## Linux CUA

The `e2e-linux-cua` job runs Xvfb, a private D-Bus/AT-SPI session, a small
window manager, Chromium, the standard `cua-driver` dependency, and the real
Relay processes. The CUA catalogue is discovered at runtime and the
representative surface includes native desktop operations:

```text
relay_cua_list_windows
relay_cua_get_window_state
relay_cua_click
relay_cua_type_text
```

The same CUA scenario then runs one bounded browser subpath against the local
page served by that harness. CUA owns the browser process from launch through
cleanup; `browser_prepare` attaches to the launched process with an existing
profile rather than starting a second browser:

```text
relay_cua_launch_app -> relay_cua_start_session
  -> relay_cua_list_windows (launch PID/window anchor)
  -> relay_cua_browser_prepare (existing profile, same PID)
  -> relay_cua_list_windows (prepared PID)
  -> relay_cua_get_browser_state (target/tab binding)
  -> relay_cua_browser_navigate -> relay_cua_get_browser_state
  -> relay_cua_browser_type -> relay_cua_browser_click
  -> relay_cua_get_browser_state / correlated fixture event
  -> relay_cua_end_session -> relay_cua_kill_app
```

The scenario verifies automatic driver resolution, provider startup, complete
catalogue discovery, explicit activation, policy blocking, representative
desktop operations, browser URL/state, browser typing and clicking, correlated
fixture evidence, and cleanup. This is an integrated browser subpath of the
general Linux CUA job, not a separate scenario or job. Chromium is an explicit
prerequisite of that job. Windows keeps the desktop candidate path until a
hosted browser prerequisite is guaranteed. A green contract/probe run or a
local mock does not replace a successful graphical Linux CUA run at the same
SHA; the latter remains the product-level proof.

Relevant local checks:

```sh
uv run --frozen python -m pytest -q \
  tests/test_cua_catalog.py tests/test_cua_profiles.py \
  tests/test_computer_capability.py tests/test_desktop_fixture.py \
  tests/test_e2e_kernel.py tests/test_e2e_mcp_client.py \
  tests/test_e2e_oracles.py \
  tests/test_linux_computer_e2e.py tests/test_windows_computer_e2e.py \
  tests/test_linux_e2e.py tests/test_windows_e2e.py tests/test_runner.py
```

## Windows Terminal

The `e2e-windows-terminal` job runs the Server and Agent directly on the hosted
Windows runner. A Windows Job Object owns the process tree. The scenario
covers the same core MCP status, ping, terminal, stop, reconnect, restart,
and cleanup behavior as the Linux Terminal gate.

Relevant local checks:

```sh
uv run --frozen python -m pytest -q \
  tests/test_linux_e2e.py tests/test_windows_e2e.py tests/test_runner.py
```

The runtime harness refuses non-Windows hosts.

## Windows CUA candidate

The `e2e-windows-cua` job is an experimental candidate, not a support claim.
It attempts the complete path:

```text
automatic driver resolution -> tools/list -> native inventory
                           -> native descriptor calls -> fixture event -> cleanup
```

The candidate uses the standard CUA provider, Windows UI Automation, and a
synthetic fixture. It requires exact application/window identity only for
operations that need a target, fresh descriptor validation, bounded evidence,
and owned-process cleanup. Starting the provider or receiving a direct reply
does not close the gate.

Portable checks:

```sh
uv run --frozen python -m pytest -q \
  tests/test_cua_catalog.py tests/test_cua_profiles.py \
  tests/test_computer_capability.py tests/test_desktop_fixture.py \
  tests/test_e2e_kernel.py tests/test_e2e_mcp_client.py \
  tests/test_e2e_oracles.py \
  tests/test_linux_computer_e2e.py tests/test_windows_computer_e2e.py \
  tests/test_linux_e2e.py tests/test_windows_e2e.py tests/test_runner.py
```

## Docker image smoke

The Docker matrix builds the production image for `linux/amd64` and
`linux/arm64`, checks its non-root user and entrypoint contract, then runs CLI
help/version smoke commands. It is packaging evidence and does not claim a
desktop operation path. A local Linux CUA contract/probe can use a separate
ephemeral test container with Xvfb/Openbox/D-Bus/AT-SPI and Chromium; those
desktop prerequisites must never enter the product image.
