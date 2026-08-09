# Current security model

For private vulnerability reporting, follow the repository
[`SECURITY.md`](../SECURITY.md) policy. This document describes the technical
threat model and the deliberately small boundary of the current prototype.

## Threat model and protections

Agent Relay protects a local Agent against a remote request seeking an arbitrary
shell, argument, path, or environment. It uses two distinct credentials:
`RELAY_MCP_TOKEN` authenticates the MCP/control plane and `RELAY_AGENT_TOKEN`
authenticates the outbound Agent. Credentials are compared safely, messages are
strictly typed and bounded, and only one configured Agent invocation runs at a
time. Commands are a fixed allowlist executed without a shell in an existing
absolute non-symlink workspace with a reduced environment. Output, timeouts,
messages, and results are bounded; cancellation and process trees are handled
best-effort.

## Provider and public-tool boundaries

The MCP facade publishes the server-local `relay_device_status` tool plus only
selected descriptors from the connected Agent catalogue. Browser descriptors use
structured locators resolved by Playwright; no DOM handles, element IDs, CDP
methods, cookies, headers, or profile paths cross the Relay boundary.

CUA descriptors come from a configured MCP stdio driver. The Relay validates the
driver path, environment, JSON-RPC frames, descriptors, arguments, and bounded
results. It does not pass Relay credentials to the driver and does not expose
screenshots, coordinates, raw accessibility trees, process/window handles,
arbitrary driver commands, or tools that were not selected by policy. Provider
snapshot tokens are treated as opaque and stale tokens fail closed.

The authenticated direct control endpoint is the generic v2 route documented in
[`protocol.md`](protocol.md). It accepts only a strict `InvokeMessage`
contract and never bypasses registry policy or provider validation.

The default YAML configuration stores the Server secrets at
`~/.agent-relay/secrets/server/mcp_token` and
`~/.agent-relay/secrets/server/agent_token`, and the Agent secret at
`~/.agent-relay/secrets/agent/agent_token`. These token files must remain regular,
non-symlink files with mode `0600`. The MCP token is distinct and is used by the
local MCP client at `/mcp`. Canonical `RELAY_*` environment values override YAML
and secret files when explicitly provided. Configuration output redacts sensitive
values, and legacy `AGENT_RELAY_*` variables are not supported. The code offers
no arbitrary shell.

## Honest limitations

This is not complete security isolation: a compromised local user, a workspace
modifiable by a third party, a process able to read server-process variables,
or a misconfigured external TLS endpoint can compromise the model. Path
validation cannot eliminate every race with a privileged local actor. Command
results may contain workspace data.

There is no strong per-device authentication, automatic rotation, RBAC, durable
storage, or exhaustive structured auditing. Application logs are minimal: do
not treat them as an audit trail, and do not log tokens, URLs with secrets,
Bearer headers, or environments. The application does not implement TLS.

## One-listener deployment

The current Server listens once on
`RELAY_SERVER_HOST:RELAY_SERVER_PORT`, defaulting to `0.0.0.0:8000`. The same
listener serves the local MCP endpoint `/mcp` and the Agent WebSocket endpoint
`/ws/agent`. The Agent has no inbound listener; it connects outbound.

MCP/Codex remains local and uses:

```text
http://127.0.0.1:8000/mcp
```

For a trusted LAN or test network, the Agent may connect directly to
`ws://<LAN-IP>:8000/ws/agent` when `RELAY_ALLOW_INSECURE_WS=true`. Port `8000`
must be LAN-firewalled. Plaintext tokens are acceptable only on a trusted
LAN/test network; do not expose this listener to the public Internet.

For WSS, use `wss://<TLS endpoint>/ws/agent` only when an external TLS endpoint
already exists. `RELAY_ALLOW_INSECURE_WS=false` rejects non-loopback `ws://` but
accepts `wss://`; `true` permits both `ws://` and `wss://`. The Relay
application does not terminate TLS, and the project prescribes no particular TLS
endpoint or proxy implementation.

`RELAY_MCP_ALLOWED_HOSTS` and `RELAY_MCP_ALLOWED_ORIGINS` are deferred optional
settings. They are not required by the current deployment and are intentionally
left unset in the Docker example; they do not replace a LAN firewall or external
TLS boundary.

## Manual rotation

Stop the Agent and Server, generate distinct new MCP and Agent tokens, replace
the corresponding private secret files (or the explicit `RELAY_*` environment
overrides), then restart both. For the YAML-first layout, rotate
`secrets/server/mcp_token`, `secrets/server/agent_token`, and the matching Agent
token file while preserving mode `0600` and the shared Agent token value.
Invalidate or delete old values according to your local policy. Rotation
necessarily terminates the current session.
