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
| CUA | Discover and invoke selected desktop and browser tools | Exact descriptor policy; bounded provider snapshots; no screenshots or unrestricted coordinates |

Terminal access is deliberately narrow:

```text
pwd
whoami
python_version
git_status
git_branch
```

CUA 0.19.3 is selected and installed automatically for new native Local and
Agent installations. Server-only/base installations leave the optional CUA
extra out; add it with `uv sync --locked --extra cua` when the Server process
must also host CUA.
Its complete descriptor catalogue is discovered from the bundled MCP provider;
every descriptor is visible to catalogue diagnostics but CUA access defaults to
`none` until an explicit profile or individual tool selection. A target
application or window is needed only by an operation that actually acts on one.

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
             `-- selected CUA desktop/browser tools
```

The server keeps the live device registry and exposes the MCP tools. The Relay
Agent runs beside the resources it controls and opens the connection back to the
server. Today, one configured device can process one invocation at a time.

## Install

Agent Relay requires Python 3.14 or newer. The platform installers install or
verify `uv` and a managed Python 3.14.4, install the `agent-relay` command for
the current user, and can initialize a new local Server and outbound Agent for
loopback only. Existing settings are preserved. Before starting an existing
configuration, verify `server.host`, the Agent relay URL, and the host firewall.
A newly initialized Agent starts with no enabled tools.

There is no stable release yet. These commands install the moving `main` branch
and execute a remote script that is not an immutable integrity pin. Review the
script first when appropriate; the platform guides include inspect-before-run
and reviewed release-tag examples. A Git tag alone is not a cryptographic
integrity guarantee.

### Linux

Linux requires Bash, `curl`, and `tar`:

```bash
curl -fsSL https://raw.githubusercontent.com/kev0ps/agent-relay/main/scripts/install.sh | bash
```

See **[Linux setup](docs/run-linux.md)** for manual checkout setup, custom paths,
LAN/WSS configuration, diagnostics, and shutdown.

### Windows

Run from Windows PowerShell 5.1 or newer:

```powershell
iex (irm https://raw.githubusercontent.com/kev0ps/agent-relay/main/scripts/install.ps1)
```

The bootstrapper uses WinGet to install `uv` when available. If WinGet itself is
unavailable, it uses the official `uv` installer. It does not require Git, an
MSI, or an existing Python installation. See
**[Windows setup](docs/run-windows.md)** for script inspection, immutable-tag
installation, setup modes, and current platform limits.

### Start a local relay

If you accepted the configuration prompt, start the Server and outbound Agent
in separate terminal windows:

```sh
agent-relay server
agent-relay agent
```

For a guided first run, use the onboarding command. It offers the `local`, `lan`,
and `remote` transport topologies for the Local Server + Agent, Server-only,
and Agent-connected flows. It asks only for settings relevant to the selected
role; Agent credentials are masked or read from a private `.env` file or secure
input and are never printed:

```sh
agent-relay onboard
```

The installer also accepts `AGENT_RELAY_SETUP=local`, `server`, `agent`, or
`skip`. Existing `config init` commands remain available for scripted setup;
`--non-interactive` onboarding defaults to an empty Agent allowlist and never
performs a live connection check unless `--check` is selected.

Enable only the tools the local operator wants to expose, then validate the
configuration:

```sh
agent-relay tools enable relay_system_ping
agent-relay tools enable relay_terminal_exec
agent-relay config validate server
agent-relay config validate agent
agent-relay doctor
```

`doctor` is an offline audit. Agent startup reports its connection attempt,
WebSocket connection, authenticated registration, announced capability summary,
executed tool names, disconnects, and bounded retry delays at the default INFO
level. Tool arguments, request identifiers, and results are not logged. Detailed
internal phases and exception types remain behind `RELAY_NATIVE_DEBUG=1`.

If you skipped configuration, follow the relevant platform guide before
starting the processes. The shared YAML file is stored under the current user's
`.agent-relay` directory; Server and Agent credentials are stored in the private
`.env` beside that YAML and are never written to YAML. `config get server` and
`config get agent` redact sensitive values. Canonical `RELAY_*` environment
variables override YAML values when both are present. Agent Relay's `.env`
contains only `RELAY_MCP_TOKEN` and `RELAY_AGENT_TOKEN`; URL, workspace, and
tool settings remain YAML or explicit process environment overrides.

This layout is for new installations. Agent Relay is not published yet and
does not provide a migration path for older layouts.

Native Local and Agent installations include `cua-driver`. Agent Relay resolves
the driver only through `cua_driver.get_binary_path()`; no manually configured
driver location, browser extra, or separate browser runtime is used. Select an
exact CUA profile or an individual `relay_cua_<name>` tool explicitly:

```sh
agent-relay tools cua-access standard
agent-relay tools cua-access full --yes
agent-relay tools list --all
```

For a user-scoped installation created by the bootstrapper, remove only the
`agent-relay` command and its uv tool environment with:

```sh
agent-relay uninstall
```

The default configuration, private `.env`, and workspace remain in `~/.agent-relay`.
To remove that default data too, use `agent-relay uninstall --purge`; the
command asks for confirmation (or accepts `--yes` in automation). Custom
configurations and data outside `~/.agent-relay` are always preserved.
On Windows, stop other `agent-relay server` and `agent-relay agent` processes
first; the command retries uv removal every 500 ms for up to 15 seconds until
its own executable is no longer locked. If it still fails, follow the
instruction in the status log after stopping the other Agent Relay processes.

For a Linux Server container with a native Windows Agent, see the
**[Docker server deployment guide →](docs/run-server-docker.md)**.

## Connect Hermes

The MCP endpoint is `http://127.0.0.1:8000/mcp`. In the environment that
launches Hermes, load the distinct MCP token from the private Server `.env`
without printing it or placing its value in shell history.

Linux shell:

```sh
export RELAY_MCP_TOKEN="$(
  sed -n 's/^RELAY_MCP_TOKEN=//p' "$HOME/.agent-relay/.env"
)"
```

Windows PowerShell:

```powershell
$dotenvPath = Join-Path $HOME ".agent-relay\.env"
$env:RELAY_MCP_TOKEN = (
  Get-Content -LiteralPath $dotenvPath |
    Where-Object { $_ -like 'RELAY_MCP_TOKEN=*' } |
    ForEach-Object { $_.Substring('RELAY_MCP_TOKEN='.Length).Trim() }
)
```

If you initialized a custom configuration with `--config`, read the matching
`.env` beside that configuration instead.
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
  defaulting to `127.0.0.1:8000`, for both `/mcp` and `/ws/agent`;
- the Agent opens the WebSocket connection and hosts no listener;
- `RELAY_MCP_TOKEN` and `RELAY_AGENT_TOKEN` are separate credentials; normal YAML
  deployments store them in the adjacent private `.env` with mode `0600`, and
  canonical environment overrides take precedence;
- messages, outputs, timeouts and collections are typed and bounded;
- terminal commands come from a fixed allowlist and run without a shell;
- CUA supplies both desktop and browser descriptors through one dynamic
  catalogue. New descriptors remain disabled until explicit selection and
  policy-blocked descriptors cannot be enabled;
- Relay does not expose screenshots, unrestricted coordinates, raw
  accessibility trees, process/window handles, or driver execution controls;
- the Docker production image builds as non-root for AMD64 and ARM64 and passes
  image-contract and CLI smoke checks.

### Transport topologies

The topology is an onboarding guide, not persisted runtime state. After setup,
only `server.host`, `server.port`, and `agent.relay_url` determine the transport:

- **Local** — bind the Server to `127.0.0.1:8000` and use
  `ws://127.0.0.1:8000/ws/agent`. No network port needs to be reachable from
  another machine.
- **LAN** — bind the Server explicitly to a LAN address or `0.0.0.0:8000`, and
  give the Agent a `ws://<LAN-IP>:8000/ws/agent` URL. This is an unencrypted
  WebSocket on a trusted private network: authentication remains mandatory, but
  a token does not protect against transport interception. Restrict the port
  with a LAN firewall and never publish it directly to the Internet.
- **Remote** — give the Agent a `wss://<public-host>/ws/agent` URL. A Caddy,
  Traefik, Nginx, Tailscale Serve, or equivalent reverse proxy terminates TLS,
  handles WebSocket Upgrade, and forwards only to the internal Server bind.
  Do not publish the internal port directly, put a token in a URL or proxy
  configuration, or make authentication decisions from forwarded headers.

MCP/Codex remains local at `http://127.0.0.1:8000/mcp` in the Local topology.
For LAN or public DNS access, configure explicit Host/Origin allowlists as
needed; these settings validate HTTP metadata, not client source IPs. The host
firewall remains responsible for source-IP restriction.

Read the **[security model and honest limitations](docs/security.md)** before a
private deployment. Report suspected vulnerabilities through the
[security policy](SECURITY.md), not a public issue containing exploit details.

## Project status

Agent Relay is a prototype under active development, not an MVP, a turnkey
remote desktop, or a multi-tenant automation platform. There is no stable
release or compatibility guarantee yet.

The current automated test design covers:

- Linux bootstrap execution and native Windows installer regressions; an exact
  commit Windows CI run is required before treating a change as validated;
- installed CLI Server/Agent topology gates on both platforms;
- the official MCP facade;
- constrained terminal behavior;
- Linux Terminal and unified CUA desktop/browser-subpath E2E gates;
- Windows Terminal and unified CUA desktop candidate gates;
- dynamic CUA catalogue, policy, activation, and provider API contracts;
- AMD64/ARM64 image build and CLI smoke tests;
- strict schemas, authentication, bounded outputs and deterministic local
  fixtures.

Windows Computer Use/UI Automation remains experimental until the complete
hosted Agent Relay/MCP/UIA proof is repeatable.
A container runtime is not used as product proof for CUA desktop or browser
operations.

Still outside the validated product boundary:

- a packaged, versioned native Windows release and service deployment procedure;
- multiple devices, RBAC and automatic credential rotation;
- arbitrary shell commands, files, personal sessions, or unrestricted desktop
  control;
- personal sessions, secrets, purchases, uploads and external form submissions;
- public internet exposure without a trusted private TLS/WSS layer.

The next work is driven by concrete gaps rather than a published roadmap:
release-quality versioned installation and operations, explicit credential
lifecycle, and a first coherent release and compatibility policy.

## Documentation

| Guide | Use it for |
|---|---|
| [Linux setup](docs/run-linux.md) | Installation, configuration, startup and troubleshooting |
| [Docker server deployment](docs/run-server-docker.md) | Containerized Linux Relay Server with a native remote Agent |
| [Windows setup](docs/run-windows.md) | One-line PowerShell installation and local setup |
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
uv run --frozen python -m pytest -q -m "not integration"
uv run --frozen python -m pytest -q -m integration
uv run --frozen ruff check .
uv lock --check
git diff --check
```

## License

Agent Relay is licensed under the [MIT License](LICENSE).
