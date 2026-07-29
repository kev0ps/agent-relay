# Run the Relay Server with Docker

This compose deployment runs **only the Relay Server** on a Linux host. The
Relay Agent remains on the controlled Windows laptop and opens an outbound
`wss://` connection to the server. The MCP client connects to the server's
`/mcp` endpoint.

This is a private-pilot deployment recipe for the current core MCP surface. It
is not a complete Browser or Computer Use deployment.

## Topology

```text
MCP client
    | HTTPS /mcp + control token
    v
Linux host / Docker port 8000
    |
    +-- TLS/WSS proxy -> /ws/agent
    |                    ^
    |                    | outbound from the laptop
    |                    |
    |                Windows laptop: Relay Agent
    |
    +-- HTTPS reverse proxy -> /mcp
```

The remote-test compose deliberately opts into `AGENT_RELAY_HOST=0.0.0.0` and
publishes port `8000` on all host interfaces. Without this bind, a Windows
laptop cannot reach the server directly. The opt-in also requires an explicit
MCP Host allowlist; the default application configuration remains loopback-only.

Port `8000` is plain HTTP/WebSocket and should be restricted by the server
firewall to the private test network or placed behind a trusted TLS/WSS reverse
proxy. The Windows Agent rejects non-loopback plaintext `ws://`, so a real
remote Agent connection must use `wss://` even when Docker publishes port 8000.

## Prerequisites

- Linux host with Docker Engine and Docker Compose v2;
- a checked-out, reviewed Agent Relay revision;
- a firewall rule limiting the test port, or a trusted HTTPS/WSS reverse proxy
  such as Tailscale Serve;
- a private DNS name for the Relay Server;
- Python/uv on the Windows laptop for the native Agent installation.

The local Docker daemon is not required to prepare this file, but the actual
image build and container smoke test must run on a Linux Docker host.

## Checkout `main` on the server

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
control_token="$(openssl rand -base64 32)"
if [ "$agent_token" = "$control_token" ]; then
  echo "token collision; generate again" >&2
  exit 1
fi
cat > .env <<EOF
AGENT_RELAY_DEVICE_ID=windows-laptop-1
AGENT_RELAY_AGENT_TOKEN=$agent_token
AGENT_RELAY_CONTROL_TOKEN=$control_token
AGENT_RELAY_MCP_ALLOWED_HOSTS=relay.example.invalid:*
AGENT_RELAY_MCP_ALLOWED_ORIGINS=https://relay.example.invalid
EOF
unset agent_token control_token
chmod 600 .env
```

Do not commit `.env`, print its contents, use `set -x`, or place the token
values directly in a shell command or proxy configuration.

## Build and start

Validate the rendered Compose model without printing interpolated secrets:

```bash
docker compose -f docker-compose.yml config --quiet
```

Build and start the server:

```bash
docker compose -f docker-compose.yml build --pull
docker compose -f docker-compose.yml up -d
```

Check the container state:

```bash
docker compose -f docker-compose.yml ps
```

The generated container name can differ if the Compose project name is
changed; use the name shown by `docker compose -f docker-compose.yml ps` when querying it.

The compose service publishes `0.0.0.0:8000` by default. Restrict that port in
the Linux firewall. The server's MCP Host protection requires the incoming Host
header to match `AGENT_RELAY_MCP_ALLOWED_HOSTS`; use a real hostname or address
instead of the example value. The allowed-host patterns may include `:*` for
the port.

If a TLS/WSS reverse proxy is used, it must preserve these exact paths:

- MCP: `/mcp`
- Agent WebSocket: `/ws/agent`

The proxy must preserve WebSocket Upgrade and validate TLS. It may forward to
`http://127.0.0.1:8000` or the container's published host port. Do not use
non-loopback plaintext `ws://` for the laptop connection.

## Configure the Windows Agent

Install the reviewed repository revision on the laptop with Python 3.11+ and
`uv`, then create a private token file containing the **same agent token** as
the server. The file must be a private regular file. Configure the Agent with
values equivalent to:

```powershell
$env:AGENT_RELAY_DEVICE_ID = "windows-laptop-1"
$env:AGENT_RELAY_SERVER_URL = "wss://relay.example.invalid/ws/agent"
$env:AGENT_RELAY_WORKSPACE = Join-Path $env:USERPROFILE "agent-relay-workspace"
$env:AGENT_RELAY_AGENT_TOKEN_FILE = Join-Path $env:USERPROFILE ".agent-relay\agent.token"
uv run agent-relay client run
```

Replace the example hostname and paths with the private deployment values. Do
not put the control token on the laptop: the Agent needs only the agent token.
The laptop does not need an inbound firewall rule or a published port.

## Configure the MCP client

Point the MCP client at the HTTPS endpoint exposed by the reverse proxy:

```text
https://relay.example.invalid/mcp
```

Authenticate it with the distinct control token from the server's private
`.env`. Do not put the control token in the repository, a public proxy setting,
or logs.

## Stop and rotate credentials

```bash
docker compose -f docker-compose.yml down
```

To rotate credentials, stop the Agent and server, generate distinct new agent
and control tokens, update the server `.env` and the Agent token file, then
restart both:

```bash
docker compose -f docker-compose.yml build --pull
docker compose -f docker-compose.yml up -d
```

Delete or revoke the old values according to the server's secret-management
policy.

## Current limitations

- This compose has not been built or executed in the current environment because the
  local Docker daemon/CLI is unavailable; validate it on the Linux deployment
  host with `docker compose -f docker-compose.yml config --quiet`, `docker compose -f docker-compose.yml build`, and a real
  startup smoke test.
- The current Windows CI proof is loopback/native on a Windows runner, not this
  mixed Linux-server/Windows-laptop topology.
- Browser Windows E2E and Computer Use Windows are not covered by this recipe.
- There is no packaged Windows service installer or Linux systemd unit yet;
  process supervision is currently provided by Compose on the server and the
  operator's chosen Windows startup/service mechanism on the laptop.
