# Run Agent Relay on Linux

This guide runs the experimental Relay Server and outbound Relay Agent on one
Linux machine.
The shared configuration lives at `~/.agent-relay/config.yaml`; the Agent opens
no listener and connects outbound to the Server.

## One-line installation

For the moving development branch, the user-level bootstrapper is:

```bash
curl -fsSL https://raw.githubusercontent.com/kev0ps/agent-relay/main/scripts/install.sh | bash
```

It installs or verifies `uv` and managed Python, installs the Agent Relay
runtime dependencies through `uv tool install`, and asks whether to initialize
a local Server and Agent. A new Agent starts with an empty allowlist; an
existing Agent section keeps its identity and allowlist. For a release,
use an immutable tag for both the script and source. Inspect a downloaded script
before executing it when the source is not trusted; a mutable `main` URL is not
an integrity pin.

## Uninstall

For a user-scoped bootstrapper installation, remove the Agent Relay command and
its uv tool environment while keeping the local configuration:

```sh
agent-relay uninstall
```

To also remove the default configuration, private `.env`, and workspace under
`~/.agent-relay`, request an explicit purge:

```sh
agent-relay uninstall --purge
```

The purge prompts for confirmation; use `--yes` only for non-interactive
automation. Custom configurations and any `.env` files or workspaces outside
`~/.agent-relay` are preserved. The command does not remove `uv`, managed
Python, or the shared uv tool bin directory.

## Installation and initialization

For a checkout-based development environment, run the following from the
repository root; this installs the locked development dependencies used by
tests and tooling:

```sh
uv sync --locked --group dev
uv run --frozen agent-relay config init server
uv run --frozen agent-relay config set server host 127.0.0.1

# Reuse the effective Server Agent token source without printing it.
# New installations default to CUA access none.
uv run --frozen agent-relay config init agent --from-server --cua-access none
```

`config init server` creates the shared YAML file and a private `.env` beside it.
`config init agent` creates or reuses the persistent Agent identity, creates
`./workspace` relative to the configuration file, and reads the effective
Server Agent token without echoing it. CUA is disabled by default. Select
`none`, `standard`, or `full` with `--cua-access`; `full` requires confirmation
or `--yes` in automation.

The same setup can be guided by the onboarding command:

```sh
uv run --frozen agent-relay onboard
```

It offers Local Server + Agent, Server-only, and Agent connected to a remote
Server. `--role server`, `--role agent`, and `--role local` select a flow for
automation or installer integration. Use `--cua-access none|standard|full` to
choose the explicit CUA profile; the normal onboarding default is `none`.
Use `--non-interactive` with an explicit role and one-time secret-file/stdin
import options; values are copied into `.env` and never printed. Validation is
offline unless `--check` is explicitly selected for the remote Agent flow.

The initial CUA allowlist is empty. Select non-CUA Relay tools interactively or
enable individual tools/profiles explicitly:

```sh
uv run --frozen agent-relay tools enable relay_system_ping
uv run --frozen agent-relay tools enable relay_terminal_exec
uv run --frozen agent-relay tools cua-access standard
uv run --frozen agent-relay tools list --all
uv run --frozen agent-relay config validate server
uv run --frozen agent-relay config validate agent
uv run --frozen agent-relay doctor
```

For complete copyable Terminal and CUA allowlists, see
[`tools.md`](tools.md).

Tokens are neither displayed nor placed in the repository. They are stored in a
private `.env` beside `config.yaml`, with mode `0600` and no shell expansion.
The Server's `RELAY_AGENT_TOKEN` and the Agent's `RELAY_AGENT_TOKEN` use the
same value. Process environment variables override `.env` when explicitly set.

Use `--config PATH` when the default path is not appropriate:

```sh
uv run --frozen agent-relay --config /etc/agent-relay/config.yaml config validate server
```

## Start

In a first terminal, run the Server:

```sh
uv run --frozen agent-relay server
```

In a second terminal, run the outbound Agent:

```sh
uv run --frozen agent-relay agent
```

The `relay_url` value is a syntactically valid `ws://` or `wss://` URL. The
runtime accepts both schemes and validates the scheme, hostname, optional port,
userinfo, fragment, and general URL structure. The current protocol example
uses `/ws/agent`.

## Configuration examples

`config init server` defaults to the loopback bind `127.0.0.1:8000`. A
same-machine Local configuration looks like:

```yaml
server:
  host: 127.0.0.1
  port: 8000

agent:
  identity:
    id: 00000000-0000-4000-8000-000000000001
  relay_url: ws://127.0.0.1:8000/ws/agent
  workspace: ./workspace
  tools:
    allowlist: []
```

Relative paths are resolved from the directory containing `config.yaml`. Token
values never belong in this file; they belong in the adjacent `.env`, using
only `RELAY_MCP_TOKEN` and `RELAY_AGENT_TOKEN`. For Docker, canonical
`RELAY_*` environment variables override `.env`; legacy `AGENT_RELAY_*`
variables are not supported.

The standard CUA provider is initialized and discovered automatically. The
catalogue is target-independent; set the optional `computer` target fields
only when a selected descriptor acts on a particular application or window.
Each discovered CUA descriptor is exposed as `relay_cua_<name>` and starts
disabled.

## Control invocations

The MCP endpoint is hosted by the Relay Server at `http://127.0.0.1:8000/mcp`.
```sh
uv run --frozen agent-relay tools list
```

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
`git_status`, or `git_branch`. CUA desktop and browser descriptors are
advertised after provider discovery and are routable only when explicitly
enabled.

## Stop, logs, and diagnostics

Send `Ctrl-C` to the Server and Agent. Before startup, `doctor` performs the
combined offline audit without connecting to the network:

```sh
uv run --frozen agent-relay doctor
uv run --frozen agent-relay config get server
uv run --frozen agent-relay config get agent
```

`config get` prints YAML with secret values redacted. Do not use `set -x`, and
do not copy Authorization headers into logs. A `401` from `/mcp` means a
missing/invalid MCP token; `503` means the Agent is offline; `409` means a tool
is unavailable or an invocation is already in progress; `504` means timeout.

At the default INFO level the Agent also reports connection attempts, the
WebSocket connection, authenticated `registered`, the capability summary,
disconnects, and bounded reconnect delays. Internal phases and exception detail
remain behind `RELAY_NATIVE_DEBUG=1`.

## Local, LAN, and Remote topologies

Onboarding uses three topology names to generate explicit values; the topology
itself is not stored in YAML.

For **Local**, keep the defaults shown above: Server bind `127.0.0.1` and Agent
URL `ws://127.0.0.1:8000/ws/agent`.

For **LAN**, provide the LAN address explicitly (do not guess it) and keep the
port on a trusted private network:

```sh
uv run --frozen agent-relay config set server host 0.0.0.0
uv run --frozen agent-relay config set agent relay_url ws://192.168.1.10:8000/ws/agent
uv run --frozen agent-relay config validate server
uv run --frozen agent-relay config validate agent
```

`ws://` is unencrypted. Authentication is still required, but a token does not
prevent interception; use a LAN firewall and never publish port `8000` directly
to the Internet.

For **Remote**, use a public WSS URL and terminate TLS in Caddy, Nginx, Traefik,
Tailscale Serve, or an equivalent external proxy:

```sh
uv run --frozen agent-relay config set server host 127.0.0.1
uv run --frozen agent-relay config set agent relay_url wss://relay.example.com/ws/agent
uv run --frozen agent-relay config validate server
uv run --frozen agent-relay config validate agent
```

The proxy must preserve WebSocket Upgrade and long-lived connections. It is
responsible for certificates and public exposure; Agent Relay does not trust
forwarded headers and no token belongs in the proxy configuration or URL. For
MCP through a DNS name, configure explicit `RELAY_MCP_ALLOWED_HOSTS` and
`RELAY_MCP_ALLOWED_ORIGINS` values matching that public host.

## Docker image CI validation

GitHub Actions builds the production image for both `linux/amd64` and
`linux/arm64`. Each image is checked for the non-root `relay` user, the expected
`agent-relay` entrypoint, and the absence of published ports, then smoke-tested
with the root `--help` and `--version` commands.

These jobs validate packaging and startup only. They do not create a
two-container desktop topology or upload runtime UI evidence. Linux Terminal
and unified Xvfb/AT-SPI CUA jobs are the current repeatable Linux paths.
Windows CUA has a hosted candidate job, but remains experimental until its
complete fixture-backed UI Automation sequence is repeatable. See
[`e2e.md`](e2e.md) for the full Linux and Windows validation matrix.
