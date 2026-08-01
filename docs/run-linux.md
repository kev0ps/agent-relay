# Run the MVP on Linux

This guide runs the Relay Server and outbound Relay Agent on one Linux machine.
The shared configuration lives at `~/.agent-relay/config.yaml`; the Agent opens
no listener and connects outbound to the Server.

## Installation and initialization

From the repository root, install `uv` according to its documentation, then:

```sh
uv sync --group dev
agent-relay config init server
agent-relay config init agent
```

`config init server` creates the YAML section and two private Server secret
files. `config init agent` creates or reuses the persistent Agent identity,
creates `./workspace` relative to the configuration file, and prompts for the
Agent token without echoing it. In automation, pipe the token through stdin:

```sh
printf '%s\n' "$RELAY_AGENT_TOKEN" | \
  agent-relay config init agent --stdin
```

The initial Agent tool allowlist is empty. Select tools interactively during
initialization or enable them explicitly:

```sh
agent-relay tools enable relay_system_ping
agent-relay tools enable relay_terminal_exec
agent-relay config validate server
agent-relay config validate agent
agent-relay doctor
```

Tokens are neither displayed nor placed in the repository. They are separate
files with mode `0600`, owned by the current user, and rejected if they are
symlinks. The Server's `agent_token` and the Agent's `agent_token` files contain
the same value but are physically distinct files.

Use `--config PATH` when the default path is not appropriate:

```sh
agent-relay --config /etc/agent-relay/config.yaml config validate server
```

## Start

In a first terminal, run the Server:

```sh
agent-relay server
```

In a second terminal, run the outbound Agent:

```sh
agent-relay agent
```

The `relay_url` value is a generic `ws://` or `wss://` URL. The configuration
validator does not impose a fixed WebSocket path so the transport protocol can
evolve independently. The current protocol example uses `/ws/agent`.

## Configuration examples

The public YAML shape is:

```yaml
server:
  host: 127.0.0.1
  port: 8000
  allow_insecure_ws: true
  secrets:
    mcp_token_file: ./secrets/server/mcp_token
    agent_token_file: ./secrets/server/agent_token

agent:
  identity:
    id: 00000000-0000-4000-8000-000000000001
  relay_url: ws://127.0.0.1:8000/ws/agent
  workspace: ./workspace
  tools:
    allowlist: []
  secrets:
    agent_token_file: ./secrets/agent/agent_token
  browser:
    origin_policy: allowlist
    allowed_origins: []
```

Relative paths are resolved from the directory containing `config.yaml`. Token
values never belong in this file. For Docker, canonical `RELAY_*` environment
variables override YAML values, including `RELAY_MCP_TOKEN` and
`RELAY_AGENT_TOKEN`; legacy `AGENT_RELAY_*` variables are not supported.

The Browser `origin_policy` defaults to `allowlist`; `any` is an explicit,
warning-producing choice during configuration and permits only HTTP(S) pages.
It still rejects `file://`, `javascript:`, `data:`, `chrome://`, `edge://`,
`about:` navigation, malformed URLs, and other non-Web schemes.

## Control invocations

The MCP endpoint is hosted by the Relay Server at `http://127.0.0.1:8000/mcp`.
Use `agent-relay tools list` to see the complete inventory. The server-local
`relay_device_status` entry is always available from the facade but cannot be
selected in the Agent allowlist. Agent-executed tools are advertised only after
the Agent announces them over the authenticated connection.

Configure Hermes with the MCP URL and a secret supplied by the environment, not
a literal credential:

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
```

Terminal execution accepts only `pwd`, `whoami`, `python_version`,
`git_status`, or `git_branch`. Browser and Computer Use are advertised only
when their local configuration and provider prerequisites are complete.

## Stop, logs, and diagnostics

Send `Ctrl-C` to the Server and Agent. Before startup, `doctor` performs the
combined offline audit without connecting to the network:

```sh
agent-relay doctor
agent-relay config get server
agent-relay config get agent
```

`config get` prints YAML with secret values redacted. Do not use `set -x`, and
do not copy Authorization headers into logs. A `401` from `/mcp` means a
missing/invalid MCP token; `503` means the Agent is offline; `409` means a tool
is unavailable or an invocation is already in progress; `504` means timeout.

## Private LAN or external WSS

For a trusted LAN/test connection, set the Server policy and the Agent URL in
YAML or with canonical environment overrides:

```sh
agent-relay config set server host 0.0.0.0
agent-relay config set server allow_insecure_ws true
agent-relay config set agent relay_url ws://192.168.1.10:8000/ws/agent
agent-relay config validate server
agent-relay config validate agent
```

Keep port `8000` LAN-firewalled. Plaintext tokens are acceptable only on that
trusted LAN/test network. For WSS, use an externally provided TLS endpoint;
the application does not implement TLS:

```sh
agent-relay config set agent relay_url wss://tls.example.test/relay
```

## Docker image CI validation

GitHub Actions builds the production image for both `linux/amd64` and
`linux/arm64`. Each image is checked for the non-root `relay` user, the expected
`agent-relay` entrypoint, and the absence of published ports, then smoke-tested
with the root `--help` and `--version` commands.

These jobs validate packaging and startup only. They do not run Browser or
Computer Use, do not create a two-container desktop topology, and do not upload
runtime UI evidence. Native Linux Terminal, Browser, and Xvfb/AT-SPI Computer Use
jobs are the product acceptance path for Linux capabilities. Windows Computer Use
remains outside hosted CI until its native UI Automation backend and interactive
runner are available.
