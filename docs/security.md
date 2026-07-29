# MVP security

For private vulnerability reporting, follow the repository
[`SECURITY.md`](../SECURITY.md) policy. This document describes the technical
threat model and deployment boundary.

## Threat model and protections

The MVP protects a local agent against a remote request seeking an arbitrary
shell, argument, path, or environment. By default it limits the control server
to loopback; a remote-test bind is an explicit opt-in that requires a non-empty
MCP Host allowlist. It requires two separate tokens (compared in constant time),
uses a strict JSON WebSocket, and permits only one device/action at a time. Commands
are a fixed allowlist executed without a shell in an existing absolute
non-symlink workspace with a reduced environment. Output, timeouts, messages,
and results are bounded; cancellation and process trees are handled best-effort.

The agent token is preferably stored in a regular non-symlink file owned by the
user and mode `0600`; configuration diagnostics do not display it. The control
token remains distinct and is used only for local POST requests. The code
offers no arbitrary shell.

## Honest limitations

This is not complete security isolation: a compromised local user, a workspace
modifiable by a third party, a process able to read server-process variables,
or a misconfigured TLS endpoint/proxy can compromise the model. Path validation
cannot eliminate every race with a privileged local actor. Command results may
contain workspace data.

There is no strong per-device authentication, automatic rotation, RBAC,
durable storage, or exhaustive structured auditing. Application logs are
minimal: do not treat them as an audit trail, and do not log tokens, URLs with
secrets, Bearer headers, or environments.

## Private deployment

For loopback, `ws://127.0.0.1/...` is appropriate. For an agent on another
machine, the preferred deployment keeps the server on `127.0.0.1` and puts
Tailscale Serve HTTPS/WSS or a TLS reverse proxy in front of it. For the direct
remote-test compose, `AGENT_RELAY_HOST=0.0.0.0` is allowed only together with
`AGENT_RELAY_ALLOW_NON_LOOPBACK_BIND=true` and an explicit
`AGENT_RELAY_MCP_ALLOWED_HOSTS` value. Restrict the published port with the
server firewall and still use `wss://`; never use non-loopback plaintext `ws://`.
The proxy must preserve WebSocket Upgrade, the exact path, and TLS validation.
Check the installed `tailscale serve` syntax before deployment.

## Manual rotation

Stop the agent and server, generate distinct new agent and control tokens,
replace the agent `0600` file and server variables, then restart both.
Invalidate/delete old files according to your local policy. Rotation necessarily
terminates the current session.
