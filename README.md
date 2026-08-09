<div align="center">

# Agent Relay

**Remote brain. Local hands. Explicit permissions.**

Let a hosted AI agent use selected capabilities on a machine you control,
without turning that machine into a general-purpose remote shell.

[![CI](https://github.com/kev0ps/agent-relay/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kev0ps/agent-relay/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

## What it does

Agent Relay is an experimental prototype that connects an MCP-compatible AI
client to a small set of local tools.
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
| Browser | Read and interact with an allowlisted web app | Allowed origins and bounded structured locators; no Playwright handles |
| CUA | Discover and invoke selected desktop-driver tools | Exact app/window identity; bounded provider snapshots; no screenshots or coordinates |

Terminal access is deliberately narrow:

```text
pwd
whoami
python_version
git_status
git_branch
```

Browser and CUA stay disabled until their local configuration is complete. CUA
descriptors are discovered from the configured MCP stdio driver; only selected
descriptors are indexed, announced and routable.

The complete tested tool list and copyable `config.yaml` profiles are in
**[Agent Relay tools](docs/tools.md)**.

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
             `-- selected CUA provider tools
```

The server keeps the live device registry and exposes the MCP tools. The Relay
Agent runs beside the resources it controls and opens the connection back to the
server. Today, one configured device can process one invocation at a time.

## Quick start on Linux

Agent Relay currently targets Python 3.11 or newer and uses
[`uv`](https://docs.astral.sh/uv/) for installation. The CLI stores the shared
YAML configuration at `~/.agent-relay/config.yaml` by default. The Server and
Agent use separate secret files below `~/.agent-relay/secrets/` and never put
token values in YAML.

```sh
git clone https://github.com/kev0ps/agent-relay.git
cd agent-relay
uv sync --locked --group dev
uv run --frozen agent-relay config init server
uv run --frozen agent-relay config set server host 127.0.0.1

# Reuse the Server's Agent token without printing it or putting it in history.
# The Agent still starts with zero enabled tools.
uv run --frozen agent-relay config init agent --stdin --no-tools \
  < "$HOME/.agent-relay/secrets/server/agent_token"
uv run --frozen agent-relay config validate server
uv run --frozen agent-relay config validate agent
uv run --frozen agent-relay tools list
```

`config init agent` can also be run without `--no-tools` for interactive tool
selection; submitting an empty selection is valid. The shared YAML file is
`~/.agent-relay/config.yaml` by default. Relative workspace and secret paths are
resolved from that file, and secret values remain in separate private files.
Canonical `RELAY_*` environment variables override YAML values when both are
present; legacy `AGENT_RELAY_*` variables are not supported.

Start the Server in the first terminal:

```sh
uv run --frozen agent-relay server
```

Start the outbound Agent in a second terminal:

```sh
uv run --frozen agent-relay agent
```

Enable only the tools the local operator wants to expose:

```bash
uv run --frozen agent-relay tools enable relay_system_ping
uv run --frozen agent-relay tools enable relay_terminal_exec
uv run --frozen agent-relay config validate agent
```

`config get server` and `config get agent` print YAML with sensitive values
redacted. `doctor` performs the combined offline audit. The complete setup
guide covers token checks, direct local control calls, diagnostics and shutdown.
Optional capability settings are part of the Agent YAML section.

**[Run Agent Relay on Linux →](docs/run-linux.md)**

For a Linux Server container with a native Windows Agent, see the
**[Docker server deployment guide →](docs/run-server-docker.md)**.

## Connect Hermes

The MCP endpoint is `http://127.0.0.1:8000/mcp`. In the shell that launches
Hermes, load the distinct MCP token from the private Server secret file without
printing it or placing its value in shell history:

```sh
export RELAY_MCP_TOKEN="$(
  cat "$HOME/.agent-relay/secrets/server/mcp_token"
)"
```

If you initialized a custom configuration with `--config`, read the matching
`secrets/server/mcp_token` path relative to that configuration instead.
Reference that environment variable from the Hermes configuration rather than
writing a literal token into YAML.

```yaml
mcp_servers:
  agent_relay:
    url: http://127.0.0.1:8000/mcp
    headers:
      Authorization: "Bearer ${RELAY_MCP_TOKEN}"
    supports_parallel_tool_calls: false
```

Hermes discovers the currently announced tools through MCP. Tool availability
still depends on what the Relay Agent has enabled locally.

## Security model

Agent Relay is designed around explicit local authority rather than broad remote
access:

- the canonical Server has one listener on `RELAY_SERVER_HOST:RELAY_SERVER_PORT`,
  defaulting to `0.0.0.0:8000`, for both `/mcp` and `/ws/agent`;
- the Agent opens the WebSocket connection and hosts no listener;
- `RELAY_MCP_TOKEN` and `RELAY_AGENT_TOKEN` are separate credentials; normal YAML
  deployments store them in private `0600` secret files and canonical environment
  overrides take precedence;
- messages, outputs, timeouts and collections are typed and bounded;
- terminal commands come from a fixed allowlist and run without a shell;
- browser access uses an exact origin allowlist by default; the optional
  `any` browser origin policy permits only `http://` and `https://` pages and
  still rejects `file://`, `javascript:`, `data:`, `chrome://`, `edge://`,
  and other non-Web schemes;
- CUA uses selected generic driver descriptors and bounded snapshot metadata;
  Relay does not expose screenshots, coordinates, raw accessibility trees,
  process/window handles, or driver execution controls;
- the Docker production image builds as non-root for AMD64 and ARM64 and passes
  image-contract and CLI smoke checks.

By default, MCP/Codex remains local at `http://127.0.0.1:8000/mcp`. For a
trusted LAN/test MCP client in the Docker deployment, use the Server LAN IP
directly; IP-literal Host values are accepted automatically after Bearer
authentication. Restrict port `8000` to the intended client and Agent source
IPs with the host firewall.
For an Agent connection, use `ws://<LAN-IP>:8000/ws/agent` with
`RELAY_ALLOW_INSECURE_WS=true` and keep port `8000` LAN-firewalled. For WSS,
use `wss://<TLS endpoint>/ws/agent` only when an external TLS endpoint already
exists; the application does not implement TLS. With
`RELAY_ALLOW_INSECURE_WS=false`, non-loopback plaintext `ws://` is rejected but
`wss://` is accepted. Plaintext tokens are acceptable only on a trusted
LAN/test network. The project prescribes no specific TLS endpoint or proxy
implementation.

`RELAY_MCP_ALLOWED_HOSTS` and `RELAY_MCP_ALLOWED_ORIGINS` remain advanced
settings for DNS names and reverse proxies. `ALLOWED_ORIGINS` applies to Web
browser origins, not client source IPs. Neither setting replaces a LAN firewall
or an external TLS boundary.

Read the **[security model and honest limitations](docs/security.md)** before a
private deployment. Report suspected vulnerabilities through the
[security policy](SECURITY.md), not a public issue containing exploit details.

## Project status

Agent Relay is a prototype under active development, not an MVP, a turnkey
remote desktop, or a multi-tenant automation platform. There is no stable
release or compatibility guarantee yet.

What is validated today:

- the packaged Linux application and its server/agent topology;
- the official MCP facade;
- constrained terminal behavior;
- native Linux Terminal, Browser and CUA-provider E2E gates;
- native Windows Terminal and headless Browser E2E gates;
- Browser locator and generic CUA-provider API contracts;
- AMD64/ARM64 image build and CLI smoke tests;
- strict schemas, authentication, bounded outputs and deterministic local
  fixtures.

Windows Computer Use/UI Automation remains experimental until the complete
hosted Agent Relay/MCP/UIA proof is repeatable.
A container runtime is not used as product proof for Browser or Computer Use.

Still outside the validated product boundary:

- a packaged and documented native Windows deployment procedure;
- multiple devices, RBAC and automatic credential rotation;
- arbitrary shell commands, files, browser profiles or desktop control;
- personal sessions, secrets, purchases, uploads and external form submissions;
- public internet exposure without a trusted private TLS/WSS layer.

The next work is driven by concrete gaps rather than a published roadmap:
repeatable Windows CUA evidence, simpler third-party installation and
operations, explicit credential lifecycle, and a first coherent release and
compatibility policy.

## Documentation

| Guide | Use it for |
|---|---|
| [Linux setup](docs/run-linux.md) | Installation, configuration, startup and troubleshooting |
| [Docker server deployment](docs/run-server-docker.md) | Containerized Linux Relay Server with a native remote Agent |
| [Security](docs/security.md) | Threat model, deployment boundaries and token rotation |
| [Tools](docs/tools.md) | Complete tested tool inventory and copyable allowlist profiles |
| [Protocol](docs/protocol.md) | Wire versioning, invocation, direct control and result rules |
| [End-to-end validation](docs/e2e.md) | Linux/Windows capability matrix, commands, evidence and limits |
| [Contributing](CONTRIBUTING.md) | Development setup, checks and pull-request expectations |
| [Security policy](SECURITY.md) | Private vulnerability-reporting process |
| [Changelog](CHANGELOG.md) | Notable unreleased and versioned changes |

## Development

Development and review guidance is in [`CONTRIBUTING.md`](CONTRIBUTING.md).
Current behavior is documented from executable contracts rather than a
speculative roadmap; implementation history remains available through Git.

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
