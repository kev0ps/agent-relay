<div align="center">

# Agent Relay

**Remote brain. Local hands. Explicit permissions.**

Let a hosted AI agent use selected capabilities on a machine you control,
without turning that machine into a general-purpose remote shell.

[![CI](https://github.com/kev0ps/agent-relay/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kev0ps/agent-relay/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

## What it does

Agent Relay connects an MCP-compatible AI client to a small set of local tools.
The controlled device initiates an authenticated WebSocket connection to the
Relay Server, so it does not need to expose an inbound port.

The important bit is the boundary: the model can only call tools that the local
operator has enabled. There is no generic shell tool, arbitrary driver
passthrough, caller-supplied filesystem path, or open-ended `execute()` method.

| Capability | What the agent can do | Boundary |
|---|---|---|
| Device status | Check whether the configured device is connected | Server-local status only |
| System | Send a bounded ping through the real Relay Agent | Fixed request and response schema |
| Terminal | Run a handful of useful workspace commands | Fixed command IDs, no shell or caller-supplied arguments |
| Browser | Read and interact with an allowlisted web app | Allowed origins and opaque, short-lived element IDs |
| Computer Use | Capture semantic controls, click and type | Exact app/window identity; no screenshots or coordinates exposed |

Terminal access is deliberately narrow:

```text
pwd
whoami
python_version
git_status
git_branch
```

Browser and Computer Use stay disabled until their local configuration is
complete.

## How it works

```text
Hermes or another MCP client
             |
             | MCP Streamable HTTP
             v
        Relay Server
        /mcp  /ws/agent
             |
             | authenticated, typed JSON
             | outbound connection from the device
             v
         Relay Agent
             |
             +-- system
             +-- constrained terminal
             +-- allowlisted browser
             `-- constrained Computer Use
```

The server keeps the live device registry and exposes the MCP tools. The Relay
Agent runs beside the resources it controls and opens the connection back to the
server. Today, one configured device can process one invocation at a time.

## Quick start on Linux

Agent Relay currently targets Python 3.11 or newer and uses
[`uv`](https://docs.astral.sh/uv/) for installation. The MVP uses one Relay
Server listener for both `/mcp` and `/ws/agent`; the Agent connects outbound and
opens no listener.

```sh
git clone https://github.com/kev0ps/agent-relay.git
cd agent-relay
uv sync --group dev

STATE_DIR="$PWD/.agent-relay-state"
install -d -m 700 "$STATE_DIR"
umask 077
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$STATE_DIR/agent.token"
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$STATE_DIR/mcp.token"
chmod 600 "$STATE_DIR/agent.token" "$STATE_DIR/mcp.token"
cmp -s "$STATE_DIR/agent.token" "$STATE_DIR/mcp.token" && { echo 'tokens are identical: generate them again'; exit 1; } || :
```

Start the local-only Server in the first terminal:

```sh
STATE_DIR="$PWD/.agent-relay-state"
RELAY_SERVER_HOST=127.0.0.1 \
RELAY_SERVER_PORT=8000 \
RELAY_MCP_TOKEN="$(cat "$STATE_DIR/mcp.token")" \
RELAY_AGENT_TOKEN="$(cat "$STATE_DIR/agent.token")" \
RELAY_ALLOW_INSECURE_WS=true \
uv run agent-relay server run
```

Start the outbound Agent in a second terminal:

```sh
STATE_DIR="$PWD/.agent-relay-state"
RELAY_URL=ws://127.0.0.1:8000/ws/agent \
RELAY_AGENT_WORKSPACE="$PWD" \
RELAY_AGENT_TOKEN_FILE="$STATE_DIR/agent.token" \
uv run agent-relay client run
```

The client CLI also provides safe configuration helpers:

```bash
agent-relay client config init --output .env.client.example
agent-relay client config validate
agent-relay client config show
```

`show` masks the Agent token, and `init` refuses to overwrite an existing file.
The complete setup guide covers token checks, direct local control calls,
diagnostics and shutdown. Optional capability settings are cataloged
separately in [`.env.example`](.env.example).

**[Run Agent Relay on Linux →](docs/run-linux.md)**

For a Linux Server container with a native Windows Agent, see the
**[Docker server deployment guide →](docs/run-server-docker.md)**.

## Connect Hermes

The MCP endpoint is `http://127.0.0.1:8000/mcp`. In the shell that launches
Hermes, load the distinct MCP token without printing it or placing its value in
shell history:

```sh
STATE_DIR="$PWD/.agent-relay-state"
export RELAY_MCP_TOKEN="$(cat "$STATE_DIR/mcp.token")"
```

Reference that environment variable from the Hermes configuration rather than
writing a literal token into YAML.

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
        - relay_browser_snapshot
        - relay_browser_fill
        - relay_browser_click
        - relay_browser_scroll
        - relay_browser_type
        - relay_browser_back
        - relay_computer_capture
        - relay_computer_click
        - relay_computer_type
```

Tool availability still depends on what the Relay Agent has enabled locally.

## Security model

Agent Relay is designed around explicit local authority rather than broad remote
access:

- the canonical Server has one listener on `RELAY_SERVER_HOST:RELAY_SERVER_PORT`,
  defaulting to `0.0.0.0:8000`, for both `/mcp` and `/ws/agent`;
- the Agent opens the WebSocket connection and hosts no listener;
- `RELAY_MCP_TOKEN` and `RELAY_AGENT_TOKEN` are separate credentials;
- messages, outputs, timeouts and collections are typed and bounded;
- terminal commands come from a fixed allowlist and run without a shell;
- browser access uses an exact origin allowlist by default; the optional
  `any` browser origin policy permits only `http://` and `https://` pages and
  still rejects `file://`, `javascript:`, `data:`, `chrome://`, `edge://`,
  and other non-Web schemes;
- Computer Use exposes semantic elements instead of screenshots, coordinates or
  raw accessibility trees;
- the Docker production image builds as non-root for AMD64 and ARM64 and passes
  image-contract and CLI smoke checks.

MCP/Codex remains local at `http://127.0.0.1:8000/mcp`. For a trusted LAN/test
connection, use `ws://<LAN-IP>:8000/ws/agent` with
`RELAY_ALLOW_INSECURE_WS=true` and keep port `8000` LAN-firewalled. For WSS,
use `wss://<TLS endpoint>/ws/agent` only when an external TLS endpoint already
exists; the application does not implement TLS. With
`RELAY_ALLOW_INSECURE_WS=false`, non-loopback plaintext `ws://` is rejected but
`wss://` is accepted. Plaintext tokens are acceptable only on a trusted
LAN/test network. The MVP prescribes no specific TLS endpoint or proxy
implementation.

`RELAY_MCP_ALLOWED_HOSTS` and `RELAY_MCP_ALLOWED_ORIGINS` are deferred optional
settings, not required by the simple MVP. They do not replace a LAN firewall or
an external TLS boundary.

Read the **[security model and honest limitations](docs/security.md)** before a
private deployment. Report suspected vulnerabilities through the
[security policy](SECURITY.md), not a public issue containing exploit details.

## Current scope

Agent Relay is an early-stage experimental project, not a turnkey remote
desktop or a multi-tenant automation platform. See the
[roadmap](docs/ROADMAP.md) for priorities and exit criteria.

What is validated today:

- the packaged Linux application and its server/agent topology;
- the official MCP facade;
- constrained terminal behavior;
- native Linux Terminal, Browser and Computer Use E2E gates;
- native Windows Terminal and headless Browser E2E gates;
- Browser and Computer Use API contracts;
- AMD64/ARM64 image build and CLI smoke tests;
- strict schemas, authentication, bounded outputs and deterministic local
  fixtures.

Windows Computer Use/UI Automation remains experimental until the complete
hosted Agent Relay/MCP/UIA proof is repeatable.
A container runtime is not used as product proof for Browser or Computer Use.

Still outside the validated product boundary:

- native Windows deployment;
- multiple devices, RBAC and automatic credential rotation;
- arbitrary shell commands, files, browser profiles or desktop control;
- personal sessions, secrets, purchases, uploads and external form submissions;
- public internet exposure without a trusted private TLS/WSS layer.

## Documentation

| Guide | Use it for |
|---|---|
| [Linux setup](docs/run-linux.md) | Installation, configuration, startup and troubleshooting |
| [Docker server deployment](docs/run-server-docker.md) | Linux Relay Server container with a remote Windows Agent |
| [Security](docs/security.md) | Threat model, deployment boundaries and token rotation |
| [Protocol v1](docs/protocol-v1.md) | Relay WebSocket messages and validation rules |
| [Capability contracts](docs/e2e-client-capabilities.md) | MCP tools, fixture contracts and black-box guarantees |
| [Windows Terminal E2E](docs/run-windows-e2e.md) | Native Windows core/MCP gate and its boundaries |
| [Windows Browser E2E](docs/run-windows-browser-e2e.md) | Headless Playwright persistent-context gate and independent evidence |
| [Windows Computer Use E2E](docs/run-windows-computer-e2e.md) | Experimental full Agent Relay/MCP/UIA candidate gate |
| [Roadmap](docs/ROADMAP.md) | Current priorities, product direction and exit criteria |
| [Contributing](CONTRIBUTING.md) | Development setup, checks and pull-request expectations |
| [Security policy](SECURITY.md) | Private vulnerability-reporting process |
| [Changelog](CHANGELOG.md) | Notable unreleased and versioned changes |

## Development

The active roadmap is [`docs/ROADMAP.md`](docs/ROADMAP.md). Development and
review guidance is in [`CONTRIBUTING.md`](CONTRIBUTING.md). Obsolete dated plans
are removed; implementation history remains available through Git.

For a local check:
```sh
uv run --frozen pytest -q -m "not integration"
uv run --frozen pytest -q -m integration
uv run --frozen ruff check .
uv lock --check
git diff --check
```

## License

Agent Relay is licensed under the [MIT License](LICENSE).
