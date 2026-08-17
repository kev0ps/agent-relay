# Agent Relay tools

CUA is Agent Relay's only desktop and browser provider. Agent Relay installs
`cua-driver` as a standard dependency, resolves its bundled executable through
`cua_driver.get_binary_path()`, starts the provider, and discovers its complete
MCP catalogue at runtime. Discovery does not require an application, window,
or manually supplied path.

The Agent starts with CUA access `none`. Every discovered CUA tool is visible
to local catalogue and diagnostics commands, but remains disabled until the
operator explicitly enables it. A tool rejected by security policy remains
blocked and cannot be enabled. A newer driver catalogue therefore never grants
new authority by itself.

## Public names

Built-in Relay tools keep their stable names:

```yaml
agent:
  tools:
    allowlist:
      - relay_system_ping
      - relay_terminal_exec
```

Every CUA descriptor named `<name>` is exposed as:

```text
relay_cua_<name>
```

This applies equally to native desktop and browser operations. For example,
when the installed driver advertises them, the public names are:

```text
relay_cua_list_windows
relay_cua_get_window_state
relay_cua_click
relay_cua_type_text
relay_cua_browser_prepare
relay_cua_browser_navigate
relay_cua_get_browser_state
relay_cua_browser_click
relay_cua_browser_type
```

`browser_*` here is a CUA descriptor name, not an Agent Relay capability or a
separate browser implementation. No legacy Browser-prefixed aliases exist.

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

### Representative CUA desktop and browser tools

```yaml
agent:
  tools:
    allowlist:
      - relay_cua_list_windows
      - relay_cua_get_window_state
      - relay_cua_click
      - relay_cua_type_text
      - relay_cua_browser_prepare
      - relay_cua_browser_navigate
      - relay_cua_get_browser_state
      - relay_cua_browser_click
      - relay_cua_browser_type
```

The browser tools are ordinary CUA tools. They share the same catalogue,
activation, schema validation, policy, logging, and execution path as native
desktop tools. A target application or window is supplied only for a CUA
operation whose descriptor needs one; catalogue discovery itself is target
independent.

`relay_device_status` is deliberately absent. It is implemented by the Relay
Server and cannot be selected in the Agent allowlist.

## Tool reference

| Public MCP tool | Internal route | Boundary |
|---|---|---|
| `relay_device_status` | Server-local | Read connection and invocation state; no Agent dispatch |
| `relay_system_ping` | `system.ping` | Fixed bounded Agent round trip |
| `relay_terminal_exec` | `terminal.exec` | Fixed command IDs; no shell or caller arguments |
| `relay_cua_<name>` | `cua.<name>` | The exact descriptor, schema, and policy returned by CUA |

The catalogue may contain tools that are disabled or blocked. Only selected,
available descriptors are announced to the MCP client and routable by the
Agent. Dynamic descriptor names and schemas are never copied into a static
Relay catalogue.

Use the CLI to inspect the effective catalogue:

```sh
agent-relay tools list
agent-relay tools list --all
```

The compact form lists Relay's non-CUA tools individually and summarizes CUA
access, enabled/available/blocked counts, and newly discovered tools. `--all`
expands the individual CUA entries. The maintained exact profiles are selected
without adding a YAML level field:

```sh
agent-relay tools cua-access none
agent-relay tools cua-access standard
agent-relay tools cua-access full --yes
```

`full` asks for confirmation interactively and requires `--yes` when stdin is
not interactive. `none` removes only CUA names; `standard` and `full` replace
only the CUA portion and preserve the non-CUA allowlist. A manual CUA change
that does not exactly match a maintained profile is shown as `custom`. Newly
discovered CUA tools stay disabled and do not enter a profile automatically.

Enable individual names returned by `tools list --all`, then validate:

```sh
agent-relay tools enable relay_cua_browser_navigate
agent-relay config validate agent
```

The standard installation installs no separate browser dependency and asks for
no manually configured driver location. The CUA package owns driver resolution
and Agent Relay only accepts the executable path returned by its package API.

For first-run configuration, use the same access choice directly:

```sh
agent-relay config init agent --cua-access standard
agent-relay onboard --role local --cua-access standard
```

`--tools` may accompany `--cua-access` only when it contains no
`relay_cua_*` name. `--no-tools` is exclusive with `--cua-access`.
