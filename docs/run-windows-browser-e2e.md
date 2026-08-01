# Native Windows Browser E2E

## Scope

This gate validates the Windows Browser capability on the hosted `windows-2025`
runner through the public Agent Relay MCP surface. It runs headless Chromium
through Playwright's persistent-context API. The Agent, not the harness, owns the
Chromium lifecycle.

The scenario owns three native processes in one Windows Job Object:

- Relay Server on a loopback HTTP/WebSocket port;
- the loopback Browser fixture and its independent JSONL oracle;
- Relay Agent, which launches Chromium with an ephemeral persistent User Data Dir.

The harness does not start Chromium separately, use `connect_over_cdp()`, open a
remote-debugging port, or attach to an existing browser. A profile lock or a
Playwright startup error is a failure; the harness never kills a user's browser.

## Evidence

The smoke calls the public Browser tools in one MCP session:

1. `relay_device_status`;
2. `relay_browser_list_tabs`;
3. `relay_browser_navigate`;
4. `relay_browser_snapshot`;
5. rejection of a disallowed origin without echoing the rejected URL;
6. invalidation of a stale Browser element after navigation;
7. `relay_browser_back` to the fixture page;
8. a fresh `relay_browser_snapshot`;
9. `relay_browser_type` and `relay_browser_fill`;
10. `relay_browser_scroll` in both directions;
11. a fresh snapshot followed by `relay_browser_click`;
12. exactly one independent fixture event.

Artifacts are bounded and limited to:

- `output.log`;
- `success.json`;
- `browser-events.jsonl`.

The workflow validates the exact oracle schema, at-most-once event count,
generated run/value shapes, reparse-point rejection, and cleanup-safe artifact
bounds.

## Local Windows invocation

```powershell
uv sync --locked --extra browser
uv run --frozen playwright install chromium
uv run --frozen python scripts/windows_browser_e2e.py `
  --evidence-dir browser-evidence `
  --output-file browser-evidence/output.log
```

The harness rejects non-Windows hosts. It creates a temporary profile and passes
that path to the Agent as `RELAY_AGENT_BROWSER_USER_DATA_DIR`. Local configuration
can instead point the Agent at an explicitly selected Chromium User Data Dir;
that directory must not already be locked by another Chromium instance.

## Explicit non-goals

This gate does not validate:

- headed Chromium or desktop rendering;
- Computer Use/UI Automation;
- a personal interactive desktop session;
- a mixed Linux Server / Windows Agent deployment;
- external origins, arbitrary JavaScript, or unrestricted CDP access.
