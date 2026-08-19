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

## One-listener deployment and transport topologies

The current Server listens once on
`RELAY_SERVER_HOST:RELAY_SERVER_PORT`, defaulting to `127.0.0.1:8000`. The same
listener serves the local MCP endpoint `/mcp` and the Agent WebSocket endpoint
`/ws/agent`. The Agent has no inbound listener; it connects outbound.

The topology is an onboarding choice, not persisted runtime state. Once the
configuration is written, `server.host`, `server.port`, and `agent.relay_url`
are the complete transport contract:

### Local

Use the loopback bind and an unencrypted loopback WebSocket:

```yaml
server:
  host: 127.0.0.1
  port: 8000
agent:
  relay_url: ws://127.0.0.1:8000/ws/agent
```

MCP/Codex uses `http://127.0.0.1:8000/mcp`. No network port needs to be
reachable from another machine.

### LAN

Bind explicitly to a LAN address or wildcard and use the LAN address in the
Agent URL:

```yaml
server:
  host: 0.0.0.0
  port: 8000
agent:
  relay_url: ws://192.168.1.20:8000/ws/agent
```

`ws://` is unencrypted and is intended only for a trusted private network,
Docker network, or CI fixture. Authentication remains mandatory, but the Agent
token does not protect against transport interception. Restrict port `8000`
with a host/LAN firewall and never expose it directly to the public Internet.
The onboarding flow asks for the LAN address; it does not guess one.

### Remote

TLS is terminated outside Agent Relay by a reverse proxy or secure tunnel. The
internal Server port is reachable only by that boundary, and the Agent uses a
public `wss://` URL:

```yaml
server:
  host: 127.0.0.1
  port: 8000
agent:
  relay_url: wss://relay.example.com/ws/agent
```

For example, a minimal Caddy site is:

```caddyfile
relay.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

The proxy must terminate TLS, preserve WebSocket Upgrade, and use long-lived
read/write timeouts appropriate for an Agent session. Nginx, Traefik, Tailscale
Serve, and equivalent products provide the same boundary when configured
accordingly. Agent Relay does not configure certificates, trust `X-Forwarded-*`
or `Forwarded`, or make authentication decisions from proxy headers. Do not
publish the internal port, put a token in a URL or proxy configuration, or add
an alternate public path that bypasses authentication.

For MCP over a public DNS name, set explicit `RELAY_MCP_ALLOWED_HOSTS` and
`RELAY_MCP_ALLOWED_ORIGINS` values matching the proxy hostname and HTTPS origin.
These validate HTTP metadata, not client source IPs; the host firewall remains
responsible for source-IP restriction.

## Manual rotation

Stop the Agent and Server, generate distinct new MCP and Agent tokens, replace
the corresponding keys in the adjacent `.env` (or use explicit `RELAY_*`
environment overrides), then restart both. Preserve mode `0600` and the shared
Agent token value.
Invalidate or delete old values according to your local policy. Rotation
necessarily terminates the current session.
