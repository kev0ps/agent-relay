# Run the Relay Server with Docker

This recipe runs the **Relay Server only** in Docker on a Linux host. The
Relay Agent remains on the controlled Windows laptop and opens an outbound
connection. MCP/Codex remains local to the Linux host and connects to the
Server's `/mcp` endpoint.

This is the simple one-listener MVP deployment. The application exposes one
TCP listener on port `8000`; that listener serves both `/mcp` and `/ws/agent`.
This recipe does not configure TLS termination, labels, or an additional network
component.

## Topology

```text
Linux host: MCP/Codex
    |
    | http://127.0.0.1:8000/mcp + Bearer RELAY_MCP_TOKEN
    v
Docker relay-server: 0.0.0.0:8000
    ^
    |
    | outbound Agent connection:
    | ws://<LAN-IP>:8000/ws/agent  (trusted LAN/test)
    | or wss://<TLS endpoint>/ws/agent (external TLS already exists)
    |
Windows laptop: Relay Agent
```

Port `8000` is plain HTTP/WebSocket in the application and must be
LAN-firewalled. The Agent has no inbound listener. The application does not
implement TLS. Plaintext tokens are acceptable only on a trusted LAN/test
network.

## Prerequisites

- Linux host with Docker Engine and Docker Compose v2;
- a checked-out, reviewed Agent Relay revision;
- a firewall rule limiting port `8000` to the trusted LAN/test network;
- Python 3.11+ and `uv` on the Windows laptop for the native Agent;
- if WSS is required, an externally provided TLS endpoint that already serves
  `/ws/agent` and reaches the published listener. This guide does not prescribe
  a particular TLS endpoint implementation.

The local Docker daemon is not required to prepare this file, but the actual
image build and container smoke test must run on a Linux Docker host.

## Checkout the reviewed revision on the server

The image is built locally from the Dockerfile and source in the checked-out
repository. Nothing is pulled from a prebuilt Agent Relay image.

```bash
git clone --branch main --depth 1 https://github.com/kev0ps/agent-relay.git /opt/agent-relay
cd /opt/agent-relay
sudo git -C /opt/agent-relay pull --ff-only origin main
```

Run the remaining commands from `/opt/agent-relay`, or replace that path with
the directory used by the server administrator.

## Create the server environment

Run this on the Linux server from the repository root. Keep the generated file
private; `.env` is ignored by Git.

```bash
umask 077
agent_token="$(openssl rand -base64 32)"
mcp_token="$(openssl rand -base64 32)"
if [ "$agent_token" = "$mcp_token" ]; then
  echo "token collision; generate again" >&2
  exit 1
fi
cat > .env <<EOF
RELAY_SERVER_HOST=0.0.0.0
RELAY_SERVER_PORT=8000
RELAY_MCP_TOKEN=$mcp_token
RELAY_AGENT_TOKEN=$agent_token
RELAY_ALLOW_INSECURE_WS=true
EOF
unset agent_token mcp_token
chmod 600 .env
```

Do not commit `.env`, print its contents, use `set -x`, or place token values
directly in a shell command. `RELAY_MCP_ALLOWED_HOSTS` and
`RELAY_MCP_ALLOWED_ORIGINS` are deferred optional settings for a later
configuration step; leave them unset for this MVP.

## Build and start

Validate the rendered Compose model without printing interpolated secrets:

```bash
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml build --pull
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml ps
```

The compose service publishes the single listener as `0.0.0.0:8000` by
default. Confirm that the Linux firewall allows only the intended trusted
LAN/test peers. Do not expose port `8000` to the public Internet.

## Configure the Windows Agent

Install the reviewed repository revision on the laptop with Python 3.11+ and
`uv`, then create a private token file containing the **same Agent token** as
the Server. The file must be a private regular file. Configure the Agent with
values equivalent to:

```powershell
$env:RELAY_URL = "ws://<LAN-IP>:8000/ws/agent"
$env:RELAY_AGENT_WORKSPACE = Join-Path $env:USERPROFILE "agent-relay-workspace"
$env:RELAY_AGENT_TOKEN_FILE = Join-Path $env:USERPROFILE ".agent-relay\agent.token"
$env:RELAY_AGENT_ID = "00000000-0000-4000-8000-000000000001"
$env:RELAY_ALLOW_INSECURE_WS = "true"
uv run agent-relay agent
```

Replace `<LAN-IP>` and the paths with the private deployment values. The
explicit `RELAY_AGENT_ID` is optional; when omitted, the Agent persists a stable
identity in its private workspace state. The laptop does not need an inbound
firewall rule or a published port.

For an externally provided TLS endpoint, use the exact Agent path and leave the
Agent URL policy false or unset:

```powershell
$env:RELAY_URL = "wss://<TLS endpoint>/ws/agent"
# RELAY_ALLOW_INSECURE_WS is not needed for wss://
uv run agent-relay agent
```

`RELAY_ALLOW_INSECURE_WS=true` permits both `ws://` and `wss://` URLs. With
`false`, non-loopback `ws://` is rejected while `wss://` is accepted.

## Configure MCP/Codex

MCP/Codex stays on the Linux host and uses the local endpoint:

```text
http://127.0.0.1:8000/mcp
```

Authenticate it with the distinct `RELAY_MCP_TOKEN` from the Server's private
`.env` as a Bearer token. Do not put the MCP token in the repository, display
it in logs, or send it to the Windows laptop.

## Stop and rotate credentials

```bash
docker compose -f docker-compose.yml down
```

To rotate credentials, stop the Agent and Server, generate distinct new Agent
and MCP tokens, update the Server `.env` and the Agent token file, then restart
both:

```bash
docker compose -f docker-compose.yml build --pull
docker compose -f docker-compose.yml up -d
```

Delete or revoke the old values according to the Server's secret-management
policy.

## Current limitations

- This recipe does not provide TLS; WSS depends on an externally provided TLS
  endpoint, and the application does not prescribe its implementation.
- The current Windows CI proof is loopback/native on a Windows runner, not this
  mixed Linux-server/Windows-laptop topology.
- Browser Windows E2E and Computer Use Windows are not covered by this recipe.
- There is no packaged Windows service installer or Linux systemd unit yet;
  process supervision is currently provided by Compose on the Server and the
  operator's chosen Windows startup/service mechanism on the laptop.
