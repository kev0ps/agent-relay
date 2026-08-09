# Agent Relay tools

This document lists the complete public tool surface currently implemented and
tested by Agent Relay. The configuration examples are ready to copy into the
`agent.tools.allowlist` section of `config.yaml`.

Agent Relay starts with an empty allowlist. Enabling a tool grants local
authority, so select the smallest profile that meets the deployment's needs.
Run `agent-relay config validate agent` after changing the list.

## Complete tested allowlist

This is the complete set of Agent-executed tools covered by the current
repository contracts:

```yaml
agent:
  tools:
    allowlist:
      - relay_system_ping
      - relay_terminal_exec
      - relay_browser_list_tabs
      - relay_browser_navigate
      - relay_browser_snapshot
      - relay_browser_fill
      - relay_browser_click
      - relay_browser_scroll
      - relay_browser_type
      - relay_browser_back
      - relay_cua_list_windows
      - relay_cua_get_window_state
      - relay_cua_click
      - relay_cua_type_text
```

This block is exhaustive for the surface Agent Relay currently exercises as a
project. It is not a promise that every tool is available on every machine:
Browser and CUA have additional runtime prerequisites described below.
Replace the existing `agent.tools.allowlist` mapping while preserving the other
keys in the Agent section.

`relay_device_status` is deliberately absent. It is always implemented by the
Relay Server and cannot be selected in the Agent allowlist.

## Copyable profiles

### Minimal health check

```yaml
agent:
  tools:
    allowlist:
      - relay_system_ping
```

### Constrained terminal

```yaml
agent:
  tools:
    allowlist:
      - relay_system_ping
      - relay_terminal_exec
```

### Browser

```yaml
agent:
  tools:
    allowlist:
      - relay_browser_list_tabs
      - relay_browser_navigate
      - relay_browser_snapshot
      - relay_browser_fill
      - relay_browser_click
      - relay_browser_scroll
      - relay_browser_type
      - relay_browser_back
```

The Browser profile requires the `browser` dependency extra, a Playwright
Chromium installation, a dedicated User Data Dir, and either an explicit HTTP(S)
origin allowlist or the deliberate `any` origin policy. Non-Web schemes remain
blocked.

### Computer Use

```yaml
agent:
  tools:
    allowlist:
      - relay_cua_list_windows
      - relay_cua_get_window_state
      - relay_cua_click
      - relay_cua_type_text
```

The CUA profile requires a configured compatible MCP stdio driver, an exact
allowed application name and window title, and matching descriptors returned by
the driver's runtime `tools/list`. Windows CUA remains experimental.

## Tool reference

| Public MCP tool | Internal route | Availability | Purpose and boundary |
|---|---|---|---|
| `relay_device_status` | Server-local | Always | Read safe connection and invocation state; no Agent dispatch |
| `relay_system_ping` | `system.ping` | Built in, selectable | Verify a real bounded Agent round trip |
| `relay_terminal_exec` | `terminal.exec` | Built in, selectable | Run one fixed command ID without a shell or arguments |
| `relay_browser_list_tabs` | `browser.list_tabs` | Browser configured | List bounded tab title and URL records |
| `relay_browser_navigate` | `browser.navigate` | Browser configured | Navigate to a URL accepted by the origin policy |
| `relay_browser_snapshot` | `browser.snapshot` | Browser configured | Read bounded page text and structured locators |
| `relay_browser_fill` | `browser.fill` | Browser configured | Fill a freshly resolved structured locator |
| `relay_browser_click` | `browser.click` | Browser configured | Click a freshly resolved structured locator |
| `relay_browser_scroll` | `browser.scroll` | Browser configured | Scroll only `up` or `down` |
| `relay_browser_type` | `browser.type` | Browser configured | Type bounded text into a structured locator |
| `relay_browser_back` | `browser.back` | Browser configured | Navigate one history entry backward |
| `relay_cua_list_windows` | `cua.list_windows` | Driver descriptor selected | List bounded windows visible to the configured provider |
| `relay_cua_get_window_state` | `cua.get_window_state` | Driver descriptor selected | Read a bounded semantic snapshot and fresh element tokens |
| `relay_cua_click` | `cua.click` | Driver descriptor selected | Click one semantic element in the exact allowed window |
| `relay_cua_type_text` | `cua.type_text` | Driver descriptor selected | Type bounded text into one semantic element |

`relay_terminal_exec` accepts exactly these command IDs:

```text
pwd
whoami
python_version
git_status
git_branch
```

It does not accept command text, arguments, environment variables, a working
directory, or an executable path.

## Dynamically discovered CUA tools

The configured CUA provider may return additional descriptors through
`tools/list`. Agent Relay maps a selected descriptor named `<name>` to the
public form `relay_cua_<name>`, but discovery is not authorization. A descriptor
must be available, accepted by local policy, and explicitly selected before it
is announced.

Additional provider tools are therefore not included in the copyable tested
allowlist above. Their names and schemas depend on the installed provider
version, and some categories—arbitrary code execution, process administration,
configuration changes, recording, unrestricted coordinates, screenshots, or
generic passthrough—remain blocked by policy.

Use the CLI to inspect the effective local catalogue:

```sh
uv run --frozen agent-relay tools list
```

Only the tools announced by the connected Agent appear dynamically through the
MCP facade.
