# Onboarding and Agent Lifecycle Plan

## Context

The current installer offers one shortcut: configure a local Server and Agent
with defaults, or skip setup. It does not guide an operator who wants a
Server-only deployment or an Agent connected to a remote Server. The Agent also
keeps successful registration and reconnect failures silent unless
`RELAY_NATIVE_DEBUG=1` is set.

The intended experience is a guided first run, safe secret handling, and useful
operator-facing lifecycle messages at the `INFO` level. Debug output remains
available for internal phases and detailed diagnostics.

## Goals

- Offer explicit `Server`, `Agent`, and `local Server + Agent` onboarding paths.
- Collect only the settings relevant to the selected role.
- Explain the distinct Agent and MCP credentials without exposing either one.
- Validate the resulting configuration before reporting success.
- Report connection, authenticated registration, disconnect, and retry events
  by default.
- Preserve non-interactive installation and configuration for automation and
  CI.
- Make Windows uninstallation reliable when the command was installed as a
  uv-managed tool.

## Non-goals

- Do not add a generic shell, unrestricted tool surface, or arbitrary remote
  configuration channel.
- Do not add remote secret storage or print credentials for convenience.
- Do not treat offline configuration validation as proof of a live connection.
- Do not add a second startup ping unless it proves something beyond the
  existing authenticated `register` / `registered` exchange.

## Proposed operator flows

### Local Server and Agent

1. Generate the Server MCP and Agent credentials.
2. Reuse the Server Agent credential in the local Agent configuration.
3. Ask for the workspace and enabled tools.
4. Validate both sections and print the two startup commands.

### Server only

1. Ask for bind host, port, and the intended local/LAN deployment policy.
2. Generate private Server credentials.
3. Validate the Server section.
4. Explain how an Agent administrator can securely receive the Agent
   credential without printing it by default.

### Agent connected to a remote Server

1. Ask for the `ws://` or `wss://` Relay URL.
2. Read the Agent credential through a masked prompt, standard input, or an
   explicitly selected private file.
3. Ask for the workspace and enabled tools.
4. Validate URL policy and Agent configuration.
5. Start or suggest a connection check and clearly report its result.

## Delivery plan

1. Audit the current CLI configuration functions, installer entry points,
   lifecycle implementation, exact downstream consumers, and tests.
2. Define an interactive onboarding command or mode that composes the existing
   configuration primitives rather than duplicating them.
3. Add failing contract tests for role selection, defaults, validation,
   cancellation, invalid input, secret redaction, and non-interactive
   compatibility.
4. Implement the smallest onboarding flow that satisfies those tests.
5. Add default operator-facing Agent lifecycle output for:
   - connection attempt;
   - WebSocket connection;
   - authenticated registration;
   - capability announcement summary;
   - disconnect or rejected registration;
   - bounded reconnect delay.
6. Keep internal phases and exception detail behind
   `RELAY_NATIVE_DEBUG=1`. Sanitize URLs and never log credentials, headers, or
   complete environments.
7. Evaluate a separate `agent-relay status` interface. It must distinguish
   local validation from live Server state and must use an authoritative state
   source. The existing Server-side `relay_device_status` MCP tool remains the
   source of truth until such a CLI contract is designed.
8. Update the PowerShell installer and user documentation for all supported
   onboarding modes.
9. Run focused tests first, followed by the full unit and integration suites,
   Ruff, lockfile validation, diff checks, and supported native Windows
   evidence.

## Windows uninstall defect

### Observed behavior

Running the installed command directly can fail as follows:

```text
agent-relay uninstall
error: failed to remove directory `...\uv\tools\agent-relay\Scripts`:
Access denied. (os error 5)
```

### Cause to validate with a regression test

The installed `agent-relay` process invokes `uv tool uninstall agent-relay`
from inside the uv tool environment that uv is then asked to remove. Windows
does not allow removal of the running executable and its containing tool
environment. Other active `agent-relay server` or `agent-relay agent` processes
can hold additional locks.

### Required behavior

- Detect or avoid self-removal from the running uv tool process on Windows.
- Stop with an actionable message if other Agent Relay processes still hold the
  environment, without terminating unrelated processes automatically.
- Delegate removal to a process that can outlive the CLI, or provide a small
  external uninstall entry point that exits before deletion begins.
- Preserve `%USERPROFILE%\.agent-relay` unless `--purge` is explicitly chosen.
- Keep purge validation and symlink/junction protections unchanged.
- Add a Windows regression test that exercises the real process-lock boundary;
  a mocked subprocess command test is insufficient evidence for this defect.

## Acceptance criteria

- A new operator can configure Server-only, Agent-only, or local combined use
  without editing YAML manually.
- Secret input is masked or file/stdin-based and never appears in logs or shell
  history through documented commands.
- A successful Agent startup visibly reaches `registered` at the `INFO` level.
- Authentication, reachability, and reconnect failures produce concise,
  actionable output without debug mode.
- Normal retry behavior remains bounded and does not exit merely because the
  Server is temporarily offline.
- `doctor` continues to describe offline validation rather than live status.
- Windows uninstallation succeeds after Agent Relay processes are stopped and
  does not require the running executable to delete itself.
- Existing scripted setup remains supported and all relevant local gates pass.
