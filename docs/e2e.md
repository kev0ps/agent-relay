# End-to-end validation

This document describes the developer-facing E2E gates for Linux and Windows.
They exercise Agent Relay through the authenticated public MCP surface and use
synthetic fixtures. They do not validate personal desktops, accounts, or
external websites.

## Required path and evidence

Every product-level scenario follows the same path:

```text
official MCP client -> Relay Server -> WebSocket -> Relay Agent -> capability
```

The harness must not invoke a capability directly or inject internal WebSocket
frames as a substitute. A mutating CUA scenario passes only when it returns a
valid structured MCP result and the independent fixture records exactly one
correlated event.

Fixtures use an unpredictable run ID. Tests use temporary workspaces,
credentials, displays, Chrome profiles, and fixture state; they never reuse a
personal desktop or browser session. Missing, duplicate, stale, wrong-run,
wrong-event, and wrong-value records fail the scenario.

## Current matrix

| Platform | Terminal | CUA | Interpretation |
|---|---|---|---|
| Linux | Shared lifecycle and core scenario | Shared browser scenario in an isolated X11/AT-SPI session | Native CI gate |
| Windows | Shared lifecycle and core scenario | Shared browser scenario in the interactive runner session | Native CI gate |
| Docker image | CLI smoke only | Not a product CUA path | Packaging evidence only |

The workflow definitions in `.github/workflows/ci.yml` are authoritative for
runner versions, installation commands, exact test selections, and artifacts.

## Shared Terminal lifecycle

Linux and Windows use the same lifecycle runner and the same
`run_core_scenario()` implementation. The shared runner owns:

```text
prepare -> Server start/readiness -> Agent start/readiness -> scenario
        -> Agent stop/reconnect -> scenario
        -> Server stop/restart -> Agent readiness -> scenario
        -> evidence collection -> cleanup
```

The scenario verifies `tools/list`, `relay_device_status`, `system.ping`,
`terminal_exec("pwd")`, and `terminal_exec("git_branch")` with the same
functional oracles on both platforms. POSIX process groups and Windows Job
Objects remain isolated process primitives rather than lifecycle forks.

Relevant contract checks:

```sh
uv run --frozen python -m pytest -q \
  tests/test_e2e_harness.py \
  tests/test_linux_e2e_adapter.py \
  tests/test_windows_e2e_adapter.py
```

The native entrypoints are:

```text
scripts/linux_e2e.py
scripts/windows_e2e.py
```

## Shared browser CUA scenario

Linux and Windows run the same shared browser scenario, fixture, actions, and
oracles. The deterministic fixture is:

```text
scripts/e2e/fixtures/cua/index.html
scripts/e2e/fixtures/cua/server.py
```

Both jobs use preinstalled Google Chrome directly. They perform an explicit
Chrome sanity check and fail with a clear message when it is unavailable. The
jobs do not download, install, or provision a browser.

The canonical scenario owns a fresh temporary Chrome profile and follows this
bounded path:

```text
relay_cua_launch_app -> relay_cua_start_session
  -> relay_cua_list_windows
  -> relay_cua_browser_prepare
  -> relay_cua_get_browser_state
  -> relay_cua_browser_navigate
  -> relay_cua_browser_type
  -> relay_cua_browser_click
  -> relay_cua_get_browser_state / correlated fixture event
  -> relay_cua_end_session -> relay_cua_kill_app
```

Linux prepares an isolated `LinuxGraphicalSession` with Xvfb, private D-Bus,
Openbox, and AT-SPI. Windows validates an interactive
`WindowsGraphicalSession`; the process tree remains owned by a Windows Job
Object. These session adapters prepare only graphical primitives. Server and
Agent lifecycle, Chrome discovery, fixture serving, scenario execution,
evidence, and cleanup stay in the shared CUA harness.

Relevant contract checks:

```sh
uv run --frozen python -m pytest -q \
  tests/test_chrome_e2e.py \
  tests/test_cua_entrypoints.py \
  tests/test_cua_harness.py \
  tests/test_graphical_sessions.py \
  tests/test_desktop_fixture.py \
  tests/test_e2e_kernel.py \
  tests/test_e2e_oracles.py
```

The native entrypoints are:

```text
scripts/linux_computer_e2e.py
scripts/windows_computer_e2e.py
```

A green contract test or provider probe does not replace a successful native
CUA run at the same SHA.

## Docker image smoke

The Docker matrix builds the production image for `linux/amd64` and
`linux/arm64`, checks its non-root user and entrypoint contract, then runs CLI
help/version smoke commands. It is packaging evidence and does not claim a
desktop operation path. Graphical prerequisites never enter the product image.
