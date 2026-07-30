# Run the MVP on Linux

This guide runs the Relay Server and outbound Relay Agent on one Linux machine
using one Server listener. The canonical Server defaults to
`0.0.0.0:8000`; this local-only example explicitly binds it to `127.0.0.1`.
MCP/Codex uses the local `/mcp` endpoint and the Agent opens no listener.

## Installation and secrets

From the repository root, install `uv` according to its documentation, then:

```sh
uv sync --group dev
STATE_DIR="$PWD/.agent-relay-state"
WORKSPACE="$PWD"
install -d -m 700 "$STATE_DIR"
umask 077
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$STATE_DIR/agent.token"
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$STATE_DIR/mcp.token"
chmod 600 "$STATE_DIR/agent.token" "$STATE_DIR/mcp.token"
cmp -s "$STATE_DIR/agent.token" "$STATE_DIR/mcp.token" && { echo 'tokens are identical: generate them again'; exit 1; } || :
```

Tokens are neither displayed nor placed in the repository. The Agent validates
`agent.token`: it must be a regular file owned by the current user, not a
symlink, and exactly mode `0600`.

## Start

In a first terminal, run the Server with its secrets explicitly injected. This
command does not source a shared Server/Agent file.

```sh
STATE_DIR="$PWD/.agent-relay-state"
RELAY_SERVER_HOST=127.0.0.1 \
RELAY_SERVER_PORT=8000 \
RELAY_MCP_TOKEN="$(cat "$STATE_DIR/mcp.token")" \
RELAY_AGENT_TOKEN="$(cat "$STATE_DIR/agent.token")" \
RELAY_ALLOW_INSECURE_WS=true \
uv run agent-relay server run
```

In a second terminal (redefine `STATE_DIR` and `WORKSPACE` if needed):

```sh
STATE_DIR="$PWD/.agent-relay-state"
WORKSPACE="$PWD"
RELAY_URL=ws://127.0.0.1:8000/ws/agent \
RELAY_AGENT_WORKSPACE="$WORKSPACE" \
RELAY_AGENT_TOKEN_FILE="$STATE_DIR/agent.token" \
RELAY_AGENT_ID=linux-dev-1 \
uv run agent-relay client run
```

`RELAY_AGENT_ID` is optional. If omitted, the Agent persists a stable identity
under the workspace's private `.agent-relay` state directory. `WORKSPACE` must
be an absolute path to an existing non-symlink directory. Wait for the Agent to
connect before making the following calls.

## Control invocations

In a third terminal:

```sh
STATE_DIR="$PWD/.agent-relay-state"
MCP_TOKEN="$(cat "$STATE_DIR/mcp.token")"
curl --fail-with-body -sS \
  -H "Authorization: Bearer $MCP_TOKEN" -H 'Content-Type: application/json' \
  -d '{"tool":"system.ping"}' \
  http://127.0.0.1:8000/v1/devices/linux-dev-1/invoke
curl --fail-with-body -sS \
  -H "Authorization: Bearer $MCP_TOKEN" -H 'Content-Type: application/json' \
  -d '{"tool":"terminal.exec","command_id":"pwd"}' \
  http://127.0.0.1:8000/v1/devices/linux-dev-1/invoke
curl --fail-with-body -sS \
  -H "Authorization: Bearer $MCP_TOKEN" -H 'Content-Type: application/json' \
  -d '{"tool":"terminal.exec","command_id":"python_version"}' \
  http://127.0.0.1:8000/v1/devices/linux-dev-1/invoke
```

Do not put `MCP_TOKEN=...` in shell history; the commands above read it from
the private file. Do not use `set -x`, and do not copy `Authorization` headers
into logs.

## MCP and Hermes

MCP is hosted by the Relay Server at `http://127.0.0.1:8000/mcp` and uses the
MCP bearer token as the HTTP control credential. It is stateless Streamable HTTP
with JSON responses. The Agent does not host MCP or any listener: it keeps only
its outbound WebSocket connection to the Server.

Configure Hermes with the canonical URL and an environment placeholder, not a
literal credential:

```yaml
mcp_servers:
  agent_relay:
    url: http://127.0.0.1:8000/mcp
    headers:
      Authorization: "Bearer ${RELAY_MCP_TOKEN}"
    supports_parallel_tool_calls: false
    tools:
      include:
        - relay_device_status
        - relay_system_ping
        - relay_terminal_exec
        - relay_browser_list_tabs
        - relay_browser_navigate
        - relay_browser_read_page
        - relay_browser_fill
        - relay_browser_click
```

The allowlist matches the current MCP facade. Terminal execution accepts only
`pwd`, `whoami`, `python_version`, `git_status`, or `git_branch`. Browser calls
are advertised by the agent only when its optional CDP URL and allowed origins
are configured as shown in `.env.example`; otherwise they fail as unsupported.
Parallel calls are disabled because the configured single device supports only
one invocation at a time.

## Stop, logs, and diagnostics

Send `Ctrl-C` to the Server and Agent (SIGINT/SIGTERM stops the Agent). To keep
secret-free output, redirect only the process stderr/stdout, never your
environment or curl headers:

```sh
mkdir -p "$STATE_DIR/logs"
# Example to append to a start command: 2>&1 | tee -a "$STATE_DIR/logs/server.log"
ss -ltnp '( sport = :8000 )'
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/docs
stat -c '%a %F %U %n' "$STATE_DIR/agent.token" "$STATE_DIR/mcp.token"
```

A `401` means a missing/invalid MCP token; `503` means the Agent is offline;
`409` means an invocation is already in progress or a capability is undeclared;
`504` means timeout. `invalid agent configuration` indicates, among other
things, an invalid URL, workspace, or token file. If the Server uses its
canonical `0.0.0.0:8000` default, keep that listener LAN-firewalled.

## Private LAN or external WSS

For a trusted LAN/test Agent connection, configure the Agent with the Server's
LAN address and explicitly allow plaintext WebSockets:

```sh
RELAY_URL=ws://<LAN-IP>:8000/ws/agent
RELAY_ALLOW_INSECURE_WS=true
```

Port `8000` must remain LAN-firewalled, and plaintext tokens are acceptable only
on that trusted LAN/test network. For WSS, use an externally provided TLS
endpoint and the exact Agent path:

```sh
RELAY_URL=wss://<TLS endpoint>/ws/agent
```

The application does not implement TLS. With `RELAY_ALLOW_INSECURE_WS=false`,
non-loopback `ws://` is rejected while `wss://` is accepted; `true` permits
both. `RELAY_MCP_ALLOWED_HOSTS` and `RELAY_MCP_ALLOWED_ORIGINS` are deferred
optional settings and are not required for this MVP. MCP/Codex remains local at
`http://127.0.0.1:8000/mcp`.

## Docker image CI validation

GitHub Actions builds the production image for both `linux/amd64` and
`linux/arm64`. Each image is checked for the non-root `relay` user, the expected
`agent-relay` entrypoint, and the absence of published ports, then smoke-tested
with `--help`, `server --help`, and `agent --help`.

These jobs validate packaging and startup only. They do not run Browser or
Computer Use, do not create a two-container desktop topology, and do not upload
runtime UI evidence. Native Linux Terminal, Browser, and Xvfb/AT-SPI Computer Use
jobs are the product acceptance path for Linux capabilities. Windows Computer Use
remains outside hosted CI until its native UI Automation backend and interactive
runner are available.
