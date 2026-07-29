# Native Windows Terminal E2E

This runbook describes the `e2e-windows-native` (Native Windows Terminal end-to-end)
gate. It validates the portable
MCP path and Windows process lifecycle; it is deliberately not a UI test.

## Scope

The gate runs exclusively on a hosted `windows-2025` runner with:

```text
official MCP client
  -> native Relay Server package on 127.0.0.1
  -> WebSocket loopback
  -> native Windows Relay Agent
  -> fixed terminal capability commands
```

The harness uses a Windows Job Object with kill-on-close semantics. It creates
separate ephemeral agent/control tokens, an isolated temporary workspace, and
bounded `output.log`/`success.json` evidence (4 KiB maximum per file). Child stderr is
capped temporary diagnostics (16 KiB maximum per child), and only closed diagnostic
categories are emitted after child cleanup; raw stderr is never included in evidence.
It exercises status, ping,
terminal markers, agent stop/offline detection, agent reconnect, the core scenario after
reconnect, server stop/unavailability detection, server restart, agent re-registration,
and the core scenario after server restart. The public MCP cancellation contract is
covered by the integration test in `tests/test_mcp_facade.py`.

There is **No Docker**, **No Browser**, and **No Computer Use** in this gate.

## Why the server is native on this gate

The hosted Windows runner has Docker installed, but its Docker daemon reports a
Windows Docker engine (`windowsfilter`). It cannot build the production Linux image
(`python:3.13.5-slim-bookworm`). Therefore this runner cannot prove a mixed
Windows-client/Linux-server container topology.

The Linux Relay Server wire path is covered by `e2e-linux-native`. A mixed
Linux Server / Windows Agent topology is not part of this gate or the current
CI scope. The hosted Windows gate must not claim that proof.

## Local contract checks

On Linux, run the command-contract and security checks without attempting the
Windows runtime:

```bash
uv run --frozen pytest tests/test_windows_e2e.py -q
uv run --frozen ruff check .
```

`run_scenario()` intentionally refuses to execute when `os.name != "nt"`.

## CI evidence

The workflow performs `uv lock --check`, `uv sync --locked`, Ruff, the
Windows-compatible runner and harness test selection, the explicit integration suite, and
`scripts/windows_e2e.py`. It then validates that the evidence directory contains only
bounded regular files, and uploads the evidence for seven days only when the
validation step succeeds.

No credentials, personal profiles, personal files, external websites, Browser,
or Computer Use are used by this gate.
