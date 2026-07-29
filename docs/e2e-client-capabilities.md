# MCP-Driven Client End-to-End Contract

## Purpose and black-box boundary

The deterministic client E2E suite exercises Agent Relay through the same public
surface available to an integration. Its required path is:

```text
MCP -> Relay Server -> WebSocket -> Relay Agent -> local capability
```

The harness starts at the authenticated `/mcp` Streamable HTTP endpoint and uses
an **official MCP client** to initialize a session, discover tools, and call
them. The boundary ends at observable effects in the client environment. There
is **no direct plugin invocation**, no hand-built internal WebSocket frame, and
no call to the compatibility control endpoint as a substitute for an MCP tool
call.

Platform-specific native harnesses run the same Relay Agent package and public
MCP contract on the target operating system. The Dockerfile is validated
separately through production-image build and CLI smoke checks; a container
runtime is not treated as Browser or Computer Use product evidence.

The public action path and the oracle path are deliberately separate:

```text
                         public action path
Official MCP client ----------------------------------------------+
    |                                                             |
    +-> MCP -> Relay Server -> WebSocket -> Relay Agent -> local capability
                                                                  |
                                                                  v
                                                        structured MCP result

Client-local fixture -> append-only event file -> harness assertion
                         independent oracle path
```

A mutating scenario requires a **structured result plus independent fixture event**.
Reading the event file is permitted only as an oracle after the public
MCP action; it is never an alternate control path into the Relay Agent or local
capability.

## Native Linux Terminal gate

The primary Linux Terminal product E2E proof is the GitHub Actions job
`e2e-linux-native` (Native Linux Terminal end-to-end). It runs the official MCP client, Relay Server, and Relay
Agent as native processes on `ubuntu-24.04`, using loopback-only networking,
temporary credentials, a temporary agent workspace, and the shared typed
scenarios. It does not require a Docker daemon, a Docker socket, privileged
mode, a published listener, external credentials, or a personal profile.

The native harness proves the complete public path:

```text
official MCP client -> /mcp -> Relay Server -> /ws/agent -> Relay Agent
```

It verifies status, agent-executed ping, fixed terminal facts, agent stop and
offline detection, real agent reconnect, bounded process-group cleanup, and
the absence of surviving credentials/workspaces after the run. The harness
writes artifacts only through bounded, exclusive no-follow file creation:
`output.log` contains fixed success/failure diagnostics and `success.json` is
written only after lifecycle cleanup succeeds. A successful run therefore
contains only `native-evidence/output.log` and `native-evidence/success.json`,
with a seven-day retention limit in CI; a failed cleanup can never leave a
passing marker.

This gate proves Linux Terminal process/runtime behavior only. It does not prove
systemd installation, a personal desktop session, Wayland, or every Linux
distribution. Browser and Computer Use are separate capability gates.

## Native Linux Browser gate

The native Linux Browser product E2E proof is the GitHub Actions job
`e2e-linux-browser` (Native Linux Browser end-to-end). It runs the same public
MCP Browser scenario as the Windows Browser gate, with Relay Server, Relay Agent,
Chromium, and the loopback fixture as native processes on `ubuntu-24.04`.

The gate uses a fresh Playwright Chromium profile, loopback-only CDP, the
allowlisted fixture origin, structured Browser results, an independently written
fixture event, and a bounded PNG screenshot oracle. It does not use Docker,
personal profiles, headed desktop automation, or Computer Use.

## Native Linux Computer Use gate

The native Linux Computer Use product E2E proof is the GitHub Actions job
`e2e-linux-cua` (Native Linux CUA end-to-end). It installs the pinned Browser and
Computer Use extras, starts Xvfb, a private D-Bus/AT-SPI session, Openbox, and
Chromium, then configures the real Relay Agent Computer Use capability with the
pinned `cua-driver` executable. The shared scenario calls
`relay_computer_capture`, `relay_computer_click`, and `relay_computer_type` through
the authenticated public MCP endpoint and requires an independent desktop fixture
event plus stale-element rejection.

This gate proves the Linux X11/Xvfb/AT-SPI backend only. It does not prove a
personal desktop, Wayland, Windows UI Automation, or hosted Windows Computer Use.
The Windows UI Automation backend and full Agent Relay harness exist separately.
The hosted `e2e-windows-cua` candidate remains experimental
until it repeatably proves `tools/list`, capture, type, click, one correlated
fixture event and bounded cleanup on the exact reviewed commit.

## Native Windows Computer Use candidate

The `e2e-windows-cua` job runs the public MCP path through the native Relay
Server and Agent, pinned `cua-driver`, Windows UI Automation and a synthetic
WinForms fixture. It rejects Session 0 and Docker, owns all processes through a
Windows Job Object, and writes a success marker only after cleanup succeeds.

This candidate is not current product evidence merely because it exists in the
workflow or because the driver responds directly. See
[`run-windows-computer-e2e.md`](run-windows-computer-e2e.md) for its exact
acceptance and evidence boundaries.

## MCP tools and structured results

All inputs and outputs are closed, strictly typed, and bounded. Callers cannot
supply a device ID or timeout. Each operation has a dedicated tool rather than
a generic action or arbitrary passthrough. The inventory distinguishes the
server-local tool from tools that invoke Relay Agent capabilities; only the
latter traverse WebSocket.

### Server-local status

The **server-local status** tool reports the configured device's safe connection
state without dispatching work to the Relay Agent.

- `relay_device_status` returns the device status from Relay Server state. It
  does not allocate a Relay request ID or traverse WebSocket.

### Agent-executed system ping

The **agent-executed system ping** verifies dispatch through the Relay Agent.

- `relay_system_ping` invokes the fixed `system.ping` capability on the
  configured device; its result contains exactly the boolean field `pong`.

### Terminal

- `relay_terminal_exec(command_id)` accepts exactly one fixed command ID:
  `pwd`, `whoami`, `python_version`, `git_status`, or `git_branch`.
- Its result contains exactly `command_id`, `stdout`, `stderr`, `exit_code`,
  `timed_out`, `stdout_truncated`, and `stderr_truncated`.

Terminal execution is limited to the agent-only workspace and the fixed,
argument-free commands. There is no shell text, argv, environment, working
path, or arbitrary executable input.

### Browser Use

- `relay_browser_list_tabs()` returns an ordered collection of tab records;
  each record contains `tab_id`, `title`, and `url`.
- `relay_browser_navigate(url)` returns an action record containing `tab_id`,
  `element_id` (null for navigation), `url`, `title`, and `success`.
- `relay_browser_read_page()` returns `tab_id`, `title`, `url`, bounded `text`,
  and a bounded `elements` collection of typed browser element records.
- `relay_browser_fill(element_id, value)` returns the Browser action record.
- `relay_browser_click(element_id)` returns the Browser action record.

Element IDs are opaque and valid only for their captured page generation. URLs
are restricted to the allowlisted local fixture origin. Browser tools accept no
JavaScript, headers, cookies, filesystem paths, browser profiles, or arbitrary
CDP methods.

### Computer Use

- `relay_computer_capture()` returns `app`, `window_title`, `generation`, and a
  bounded `elements` collection. Every element contains `element_id`, `role`,
  `name`, `value`, and `enabled`.
- `relay_computer_click(element_id)` returns `success`, `generation`, and
  `element_id`.
- `relay_computer_type(element_id, text)` returns `success`, `generation`, and
  the targeted `element_id`.

Element IDs are opaque and scoped to the latest capture generation. Computer
Use is restricted to an allowlisted fixture application and window. The first
version does not expose coordinates, arbitrary key chords, drag, clipboard,
file dialogs, permission dialogs, system shortcuts, screenshot payloads,
driver passthrough, process/window identifiers, delivery modes, or arbitrary
fields. Text is non-empty, bounded, and rejects every Unicode category beginning
`C`; ordinary astral Unicode remains valid. After each capture, all earlier
element IDs are stale. The E2E scenario proves their rejection before using a
freshly resolved Apply target.

## Independent fixture event contract

The fixture writes newline-delimited JSON under `/artifacts`. Every line is a
closed envelope containing exactly `run_id`, `event`, and `value`; no additional
fields are accepted. A Browser form submission uses this exact shape:

```json
{
  "run_id": "linux-browser-<random>",
  "event": "submitted",
  "value": "relay-gh-browser-linux-browser-<random>"
}
```

Computer Use follows the same three-field envelope with `event` set to
`applied`. The fixture, not the Relay Server response or test harness, emits the
event as a consequence of the local UI mutation.

## Run and request correlation

- Before startup, each native harness generates one unpredictable, non-secret
  run ID with a gate-specific prefix such as `linux-browser-`, `linux-cua-` or
  `windows-cua-`. It is unique per isolated scenario and is supplied to the
  fixture through test-only setup or fixture data.
- A run ID scopes fixture state and retained diagnostics. It is never reused,
  treated as authorization, or substituted for a protocol request ID.
- Every MCP tool call that invokes a Relay Agent capability allocates a fresh
  opaque Relay request ID before dispatch validation. Offline, busy, or
  unsupported devices produce rejected calls, and rejected calls send no
  WebSocket invoke.
- For every call accepted for dispatch, its request ID correlates exactly one
  WebSocket invoke with any associated progress and either a terminal response
  or cancellation. The request ID must not be reused across calls or devices.
  Cancellation does not require or accept a terminal result.
- `relay_device_status` is a server-local status read; it does not allocate a
  Relay request ID or traverse WebSocket.
- The harness records the ordered association between the run ID, MCP tool call,
  and expected fixture event without recording credentials. MCP/Relay request
  IDs may appear in sanitized diagnostics, but fixture events retain only the
  three fields defined above.
- A valid mutation has one matching event produced after its corresponding MCP
  call begins and before the bounded scenario deadline. Missing, duplicate,
  stale, pre-existing, out-of-order, wrong-run, wrong-source, wrong-event, or
  wrong-value events fail the scenario.

## Evidence rules

The following combination is proof within this deterministic bench:

1. an official client successfully invokes a discovered typed tool through
   `/mcp`;
2. the Relay response passes the exact structured result schema and expected
   values; and
3. for a mutation, the client-local fixture emits exactly one independently
   observed, correctly correlated event (or Terminal returns an independently
   checkable agent-workspace fact).

A success boolean alone, a mocked result, a server log, capability
advertisement, direct plugin call, injected WebSocket message, event created by
the harness, fixture event without a matching MCP result, screenshot-only
comparison, or an LLM's interpretation does **not** prove client-side execution.
There is **no LLM as primary oracle**: deterministic schema assertions, exact
values, fixture events, and process/readiness checks are authoritative. An LLM
smoke test may be supplementary only after the deterministic suite passes.

The native gates prove execution by the real package in native platform
process topologies. Docker build and smoke jobs prove the production image
contract and CLI startup only; they are not Browser or Computer Use acceptance
and do not prove a personal desktop session or a production installer.

## Security and isolation boundaries

- The Relay Agent remains outbound-only: it publishes no port, receives no
  Docker socket, runs non-root, and accepts work only through the authenticated
  Relay Server WebSocket.
- The MCP endpoint uses the configured bearer credential. Tokens and raw
  Authorization headers must never be printed, persisted in images, fixture
  events, screenshots, logs, caches, or uploaded artifacts.
- Chromium uses a fresh test-only user-data directory, loopback-only control,
  disabled sync/extensions/password-store behavior, and **no personal browser profile**.
  Tests must not discover, mount, copy, or modify a personal profile.
- Browser navigation is restricted to the local fixture origin. Computer Use is
  restricted to the fixture app/window and semantic elements; password fields,
  permission prompts, unrelated windows, stale elements, and disabled elements
  fail closed.
- Fixtures contain synthetic values only. Purchases, external form submissions,
  downloads, uploads, secrets, personal desktop sessions, and arbitrary command
  execution remain out of scope.
- Every action, readiness check, and fixture wait is bounded. Unsupported
  fields, malformed results, unknown elements, offline or busy devices,
  cancellation, and timeouts fail closed.

## Artifact retention

During a native capability run, the platform harness may create bounded fixture
files and sanitized diagnostics needed by deterministic assertions. Successful
native jobs retain only their explicitly validated evidence. Docker build/smoke
jobs do not upload runtime Browser or Computer Use evidence.

On failure, CI may upload bounded-retention sanitized Relay Server, Relay Agent,
Chromium, Xvfb, and fixture logs plus safe screenshots. Artifact collection must
exclude tokens, Authorization headers, cookies, browser profiles, downloads,
clipboard contents, environment dumps, and unrelated desktop data. Artifacts
expire under the CI job's explicit retention limit and are never treated as a
long-term store.
