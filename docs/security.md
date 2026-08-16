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
selected descriptors from the connected Agent catalogue. All desktop and
browser descriptors come from the same CUA MCP provider. The Relay validates
the executable returned by `cua_driver.get_binary_path()`, the environment,
JSON-RPC frames, descriptors, arguments, and bounded results. Raw browser DOM
handles, native accessibility/window handles, cookies, headers, and profile
paths do not cross the Relay boundary.

The browser protocol does carry bounded opaque identifiers needed for a
session-scoped follow-up: `target_id`, `tab_id`, and provider snapshot `ref`
values. They are validated as opaque CUA identifiers and are not raw DOM,
accessibility, process, or native window handles; stale or mismatched values
fail closed.

CUA tools are discovered dynamically, but discovery is not authorization:
newly discovered descriptors are disabled until explicitly selected, while
security-blocked descriptors remain blocked. The Relay does not pass its
credentials to the provider and does not expose screenshots, unrestricted
coordinates, raw accessibility trees, process/window handles, arbitrary
driver commands, or tools that policy rejected. Provider snapshot tokens are
opaque and stale tokens fail closed.

The authenticated direct control endpoint is the generic v2 route documented in
[`protocol.md`](protocol.md). It accepts only a strict `InvokeMessage`
contract and never bypasses registry policy or provider validation.

The default YAML configuration stores no credentials. The adjacent
`~/.agent-relay/.env` contains only `RELAY_MCP_TOKEN` and
`RELAY_AGENT_TOKEN`; it must remain a regular, non-symlink file with mode `0600`
and is parsed directly without shell expansion or injection into the Agent's
child-process environment. The MCP token is distinct and is used by the local
MCP client at `/mcp`. Explicit process environment values override `.env`.
Configuration output redacts sensitive values, and legacy `AGENT_RELAY_*`
variables are not supported. The code offers no arbitrary shell.

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

By default, MCP/Codex remains local and uses:

```text
http://127.0.0.1:8000/mcp
```

The Docker example requires no MCP network setting for `localhost` or direct IP
URLs. After successful Bearer authentication, the Server accepts loopback and
IP-literal Host values automatically, while rejecting arbitrary DNS names. If
a request includes `Origin`, the automatic policy requires it to be same-origin.
Explicit host and origin allowlists remain available for DNS names and reverse
proxies. These checks validate HTTP metadata, not the client source IP; the host
firewall remains responsible for the client-IP allowlist.

For a trusted LAN or test network, the Agent may connect directly to
`ws://<LAN-IP>:8000/ws/agent` when `RELAY_ALLOW_INSECURE_WS=true`. Port `8000`
must be LAN-firewalled. Plaintext tokens are acceptable only on a trusted
LAN/test network; do not expose this listener to the public Internet.

For WSS, use `wss://<TLS endpoint>/ws/agent` only when an external TLS endpoint
already exists. `RELAY_ALLOW_INSECURE_WS=false` rejects non-loopback `ws://` but
accepts `wss://`; `true` permits both `ws://` and `wss://`. The Relay
application does not terminate TLS, and the project prescribes no particular TLS
endpoint or proxy implementation.

`RELAY_MCP_ALLOWED_HOSTS` and `RELAY_MCP_ALLOWED_ORIGINS` remain advanced
settings for DNS names and reverse proxies. They validate HTTP metadata and
are not client-IP allowlists. Neither replaces a LAN firewall or external TLS
boundary.

## Manual rotation

Stop the Agent and Server, generate distinct new MCP and Agent tokens, replace
the corresponding keys in the adjacent `.env` (or use explicit `RELAY_*`
environment overrides), then restart both. Preserve mode `0600` and the shared
Agent token value.
Invalidate or delete old values according to your local policy. Rotation
necessarily terminates the current session.
