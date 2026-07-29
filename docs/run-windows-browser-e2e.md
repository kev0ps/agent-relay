# Native Windows Browser E2E

## Scope

This gate validates the Windows Browser capability on the hosted `windows-2025` runner
through the public Agent Relay MCP surface. It is deliberately headless and uses
Chromium over a loopback CDP endpoint.

The scenario owns four native processes in one Windows Job Object:

- Relay Server on a loopback HTTP/WebSocket port;
- the loopback Browser fixture and its independent JSONL oracle;
- Chromium with a temporary profile and loopback-only remote debugging;
- Relay Agent configured with Browser CDP and one allowlisted fixture origin.

## Evidence

The smoke calls the public Browser tools in one MCP session:

1. `relay_device_status`;
2. `relay_browser_list_tabs`;
3. `relay_browser_navigate`;
4. `relay_browser_read_page`;
5. rejection of a disallowed origin without echoing the rejected URL;
6. invalidation of a stale Browser element after navigation;
7. a fresh `relay_browser_read_page`;
8. `relay_browser_fill`;
9. absence of an event before the click;
10. `relay_browser_click`;
11. exactly one independent fixture event.

After the MCP scenario, the harness uses a bounded raw CDP
`Page.captureScreenshot` probe because the current public Browser MCP inventory does
not expose a Browser screenshot tool. The probe must find exactly the allowlisted
fixture page and produce a valid PNG no larger than 512 KiB, with a non-zero
width and height bounded to 4096 pixels.

Artifacts are bounded and limited to:

- `output.log`;
- `success.json`;
- `browser-events.jsonl`;
- `screenshot.png`.

The workflow validates the exact oracle schema, at-most-once event count, generated
run/value shapes, PNG signature, reparse-point rejection and cleanup-safe artifact
bounds.

## Local Windows invocation

```powershell
uv sync --locked --extra browser
uv run --frozen playwright install chromium
uv run --frozen python scripts/windows_browser_e2e.py `
  --evidence-dir browser-evidence `
  --output-file browser-evidence/output.log
```

The harness rejects non-Windows hosts. It does not accept arbitrary browser paths or
origins from the Relay protocol; an optional `AGENT_RELAY_CHROMIUM_PATH` is only a
local runner override for resolving the executable.

## Explicit non-goals

This gate does not validate:

- headed Chromium or desktop rendering;
- Computer Use/UI Automation;
- a personal interactive desktop session;
- a mixed Linux Server / Windows Agent deployment;
- external origins or unrestricted CDP access.
