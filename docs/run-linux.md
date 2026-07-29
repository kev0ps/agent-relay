# Run the MVP on Linux (loopback)

This guide has been verified against the code's CLIs and variables. The two
processes may run on the same machine; the server remains on loopback.

## Installation and secrets

From the repository root, install `uv` according to its documentation, then:

```sh
uv sync --group dev
STATE_DIR="$PWD/.agent-relay-state"
WORKSPACE="$PWD"
install -d -m 700 "$STATE_DIR"
umask 077
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$STATE_DIR/agent.token"
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$STATE_DIR/control.token"
chmod 600 "$STATE_DIR/agent.token" "$STATE_DIR/control.token"
cmp -s "$STATE_DIR/agent.token" "$STATE_DIR/control.token" && { echo 'tokens are identical: generate them again'; exit 1; } || :
```

Tokens are neither displayed nor placed in the repository. The agent validates
`agent.token`: it must be a regular file owned by the current user, not a
symlink, and exactly mode `0600`.

## Start

In a first terminal, run the server with its secrets explicitly injected. This
command does not source a shared server/agent file.

```sh
AGENT_RELAY_DEVICE_ID=linux-dev-1 \
AGENT_RELAY_AGENT_TOKEN="$(cat "$STATE_DIR/agent.token")" \
AGENT_RELAY_CONTROL_TOKEN="$(cat "$STATE_DIR/control.token")" \
uv run agent-relay-server --host 127.0.0.1 --port 8000
```

In a second terminal (redefine `STATE_DIR` and `WORKSPACE` if needed):

```sh
STATE_DIR="$PWD/.agent-relay-state"
WORKSPACE="$PWD"
AGENT_RELAY_DEVICE_ID=linux-dev-1 \
AGENT_RELAY_SERVER_URL=ws://127.0.0.1:8000/ws/agent \
AGENT_RELAY_WORKSPACE="$WORKSPACE" \
AGENT_RELAY_AGENT_TOKEN_FILE="$STATE_DIR/agent.token" \
uv run agent-relay-agent
```

`WORKSPACE` must be an absolute path to an existing non-symlink directory. Wait
for the agent to connect before making the following calls.

## Control invocations

In a third terminal:

```sh
STATE_DIR="$PWD/.agent-relay-state"
CONTROL_TOKEN="$(cat "$STATE_DIR/control.token")"
curl --fail-with-body -sS \
  -H "Authorization: Bearer $CONTROL_TOKEN" -H 'Content-Type: application/json' \
  -d '{"tool":"system.ping"}' \
  http://127.0.0.1:8000/v1/devices/linux-dev-1/invoke
curl --fail-with-body -sS \
  -H "Authorization: Bearer $CONTROL_TOKEN" -H 'Content-Type: application/json' \
  -d '{"tool":"terminal.exec","command_id":"pwd"}' \
  http://127.0.0.1:8000/v1/devices/linux-dev-1/invoke
curl --fail-with-body -sS \
  -H "Authorization: Bearer $CONTROL_TOKEN" -H 'Content-Type: application/json' \
  -d '{"tool":"terminal.exec","command_id":"python_version"}' \
  http://127.0.0.1:8000/v1/devices/linux-dev-1/invoke
```

Do not put `CONTROL_TOKEN=...` in shell history; the commands above read it
from the private file. Do not use `set -x`, and do not copy `Authorization`
headers into logs.

## MCP and Hermes

MCP is hosted by the Relay server at `http://127.0.0.1:8000/mcp` and uses the
same control bearer token as the HTTP control API. It is stateless Streamable
HTTP with JSON responses. The agent does not host MCP or any listener: it keeps
only its outbound WebSocket connection to the server.

Configure Hermes with the canonical URL and an environment placeholder, not a
literal credential:

```yaml
mcp_servers:
  agent_relay:
    url: http://127.0.0.1:8000/mcp
    headers:
      Authorization: "Bearer ${AGENT_RELAY_CONTROL_TOKEN}"
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

Send `Ctrl-C` to the server and agent (SIGINT/SIGTERM stops the agent). To keep
secret-free output, redirect only the process stderr/stdout, never your
environment or curl headers:

```sh
mkdir -p "$STATE_DIR/logs"
# Example to append to a start command: 2>&1 | tee -a "$STATE_DIR/logs/server.log"
ss -ltnp '( sport = :8000 )'
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/docs
stat -c '%a %F %U %n' "$STATE_DIR/agent.token" "$STATE_DIR/control.token"
```

A `401` means a missing/invalid control token; `503` means the agent is
offline; `409` means an invocation is already in progress or a capability is
undeclared; `504` means timeout. `invalid agent configuration` indicates, among
other things, an invalid URL, workspace, or token file. Never run the server
with `--host 0.0.0.0`: it refuses it.

## Optional private access through Tailscale

Keep the Relay server bound to `127.0.0.1`. For another private machine,
terminate TLS in front of it with **Tailscale Serve HTTPS/WSS** or a TLS reverse
proxy, then configure the agent, for example:

```sh
AGENT_RELAY_SERVER_URL=wss://relay.example.ts.net/ws/agent
```

Do not recommend non-loopback `ws://` or a `0.0.0.0` bind. The syntax and
capabilities of `tailscale serve` vary by version: first check
`tailscale serve --help` locally, then configure the proxy to forward WebSocket
and HTTPS to `http://127.0.0.1:8000`. The trusted proxy MUST rewrite the
upstream `Host` header to an accepted loopback Host such as `127.0.0.1:8000`;
do not weaken the Relay process's Host and Origin validation. Also verify that
the public WSS URL keeps
the exact `/ws/agent` path and that the public HTTPS MCP URL keeps the exact
`/mcp` path. Remote MCP and agent access require a trusted HTTPS/WSS reverse
proxy; loopback HTTP/WS is for local development only.

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
