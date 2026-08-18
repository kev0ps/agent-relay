# Run the Relay Server with Docker

This guide runs only the Relay Server in Docker on a Linux host. The controlled
machine runs the Relay Agent natively and opens an outbound connection to the
Server.

```text
local MCP client -> http://127.0.0.1:8000/mcp
                         |
                  Docker Relay Server
                         ^
                         |
        outbound ws:// or wss:// connection
                         |
                  native Relay Agent
```

The application has one listener on port `8000` for both `/mcp` and
`/ws/agent`. It does not implement TLS. A plaintext deployment must stay on a
trusted, firewalled LAN; do not expose port `8000` directly to the Internet.

## Prerequisites

- Linux with Docker Engine and Docker Compose v2;
- a reviewed Agent Relay commit;
- a firewall restricting port `8000` to the intended network;
- Python 3.14+ and `uv` on the Agent machine.

Check out the exact reviewed revision rather than following a moving branch:

```sh
git clone https://github.com/kev0ps/agent-relay.git /opt/agent-relay
cd /opt/agent-relay
git checkout --detach <REVIEWED-COMMIT-SHA>
git rev-parse HEAD
```

Replace the placeholder with the commit that was actually reviewed.

## Create private Server secrets

From the repository root, generate an ignored `.env` file:

```sh
umask 077
agent_token="$(openssl rand -base64 32)"
mcp_token="$(openssl rand -base64 32)"
if [ "$agent_token" = "$mcp_token" ]; then
  echo "token collision; generate again" >&2
  exit 1
fi
cat > .env <<EOF
RELAY_MCP_TOKEN=$mcp_token
RELAY_AGENT_TOKEN=$agent_token
EOF
unset agent_token mcp_token
chmod 600 .env
```

The Agent token must later be copied to the private `.env` beside the Agent
YAML on the controlled machine. The distinct MCP token stays on the Server host
and authenticates the local MCP client.

Never commit `.env`, print it, enable shell tracing, or copy its values into
logs. The Compose file supplies the bind host and port. No MCP host or origin
setting is required when clients use `localhost` or a direct IP address such as
`http://192.168.1.41:8000/mcp`. After Bearer authentication, the Server accepts
IP-literal Host values automatically and rejects arbitrary DNS names. A DNS
name or reverse proxy remains an advanced deployment and requires explicit
`RELAY_MCP_ALLOWED_HOSTS` and `RELAY_MCP_ALLOWED_ORIGINS` settings.

These HTTP checks do not identify or allowlist the client IP. Restrict source
IPs with the Server host firewall and do not expose the plaintext listener to
the Internet.

## Build and start

```sh
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
```

## CI smoke coverage

The GitHub Actions job `Relay Compose Link - Server / Linux Agent` runs this
same Compose example on an Ubuntu runner. It starts the Server in the
container, installs the Linux Agent client through `scripts/install.sh`, starts
the installed `agent-relay agent` command against the published WebSocket
endpoint, and calls only `relay_device_status` through MCP. The check requires
the status response to report that the expected Agent is connected with no
optional capabilities enabled.

The job generates distinct temporary credentials, keeps them in private `.env`
files,
and removes the Compose stack and credentials in an always-run cleanup step.

Confirm that the host firewall exposes port `8000` only to the intended Agent
and MCP-client addresses. MCP clients on the Server host use:

```text
http://127.0.0.1:8000/mcp
```

with the private MCP token as a Bearer credential.

## Connect the native Agent

Install the same reviewed revision on the controlled machine. Create the
private `.env` beside the Agent YAML with only the Server's Agent token:

```text
RELAY_AGENT_TOKEN=<SERVER-AGENT-TOKEN>
```

Set the non-secret Agent options in the process environment before starting it:

```sh
export RELAY_URL=ws://<SERVER-LAN-IP>:8000/ws/agent
export RELAY_AGENT_WORKSPACE=<PRIVATE-WORKSPACE>
export RELAY_AGENT_TOOLS=relay_system_ping,relay_terminal_exec
export RELAY_ALLOW_INSECURE_WS=true
```

Start the Agent from that checkout:

```sh
uv run --frozen agent-relay agent
```

`RELAY_AGENT_TOOLS` is optional; omitting it leaves all Agent-executed tools
disabled. See [`tools.md`](tools.md) for copyable tool profiles. The
Server-local `relay_device_status` tool never belongs in the Agent allowlist.

For Windows PowerShell, the same variables can be assigned with `$env:NAME =
"value"` before starting the Agent.

For WSS, point `RELAY_URL` at an externally provided TLS endpoint and leave
`RELAY_ALLOW_INSECURE_WS` false or unset. Agent Relay does not configure or
terminate TLS itself.

## Stop and rotate

```sh
docker compose down
```

To rotate credentials, stop the Agent and Server, generate two new distinct
tokens, update the Server and Agent `.env` files, then restart both. Old
values must be deleted or revoked according to the host's secret-management
policy.

## Limits of this guide

- It does not configure TLS, a reverse proxy, systemd, or a Windows service.
- CI validates the Linux Server container plus native Linux Agent status path;
  it does not validate a mixed-platform deployment.
- Docker image checks cover packaging and CLI startup, not desktop or browser
  operations.
- The native Agent installs the standard CUA dependency and discovers its
  catalogue automatically; only selected CUA tools require configuration.

Read [`security.md`](security.md) before using a networked deployment.
