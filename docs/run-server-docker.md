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
- Python 3.11+ and `uv` on the Agent machine.

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
RELAY_ALLOW_INSECURE_WS=true
EOF
unset agent_token mcp_token
chmod 600 .env
```

The Agent token must later be copied to a private token file on the controlled
machine. The distinct MCP token stays on the Server host and authenticates the
local MCP client.

Never commit `.env`, print it, enable shell tracing, or copy its values into
logs. The Compose file supplies the Server host and port.

## Build and start

```sh
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
```

Confirm that the host firewall exposes port `8000` only to the intended Agent
network. MCP clients on the Server host use:

```text
http://127.0.0.1:8000/mcp
```

with the private MCP token as a Bearer credential.

## Connect the native Agent

Install the same reviewed revision on the controlled machine. Configure its
private token file with the Server's Agent token, then set values equivalent to:

```text
RELAY_URL=ws://<SERVER-LAN-IP>:8000/ws/agent
RELAY_AGENT_TOKEN_FILE=<PRIVATE-AGENT-TOKEN-FILE>
RELAY_AGENT_WORKSPACE=<PRIVATE-WORKSPACE>
RELAY_AGENT_TOOLS=relay_system_ping,relay_terminal_exec
RELAY_ALLOW_INSECURE_WS=true
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
tokens, update the Server `.env` and Agent token file, then restart both. Old
values must be deleted or revoked according to the host's secret-management
policy.

## Limits of this guide

- It does not configure TLS, a reverse proxy, systemd, or a Windows service.
- It does not validate a mixed-platform deployment in CI.
- Docker image checks cover packaging and CLI startup, not Browser or CUA.
- Browser and CUA require their native Agent dependencies and configuration.

Read [`security.md`](security.md) before using a networked deployment.
