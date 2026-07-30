# MVP security

For private vulnerability reporting, follow the repository
[`SECURITY.md`](../SECURITY.md) policy. This document describes the technical
threat model and the deliberately small deployment boundary.

## Threat model and protections

The MVP protects a local Agent against a remote request seeking an arbitrary
shell, argument, path, or environment. It uses two distinct credentials:
`RELAY_MCP_TOKEN` authenticates the MCP/control plane and `RELAY_AGENT_TOKEN`
authenticates the outbound Agent. Credentials are compared safely, messages are
strictly typed and bounded, and only one configured Agent invocation runs at a
time. Commands are a fixed allowlist executed without a shell in an existing
absolute non-symlink workspace with a reduced environment. Output, timeouts,
messages, and results are bounded; cancellation and process trees are handled
best-effort.

The Agent token is preferably stored in a regular non-symlink file owned by the
user and mode `0600`; configuration diagnostics do not display it. The MCP token
is distinct and is used by the local MCP client at `/mcp`. The code offers no
arbitrary shell.

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

## Simple one-listener deployment

The canonical MVP Server listens once on
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
application does not terminate TLS, and this MVP prescribes no particular TLS
endpoint or proxy implementation.

`RELAY_MCP_ALLOWED_HOSTS` and `RELAY_MCP_ALLOWED_ORIGINS` are deferred optional
settings. They are not required by this simple MVP and are intentionally left
unset in the Docker example; they do not replace a LAN firewall or external TLS
boundary.

## Manual rotation

Stop the Agent and Server, generate distinct new MCP and Agent tokens, replace
the Agent `0600` file and the Server variables, then restart both. Invalidate or
delete old files according to your local policy. Rotation necessarily
terminates the current session.
