# Relay protocol v2

This document is the current direct-control and provider-invocation contract.
The older `docs/protocol-v1.md` document is retained as historical context and
is superseded for invocation semantics.

## Scope and versioning

The Agent connection still uses the version-1 control-plane messages for
registration, capability announcement, heartbeat, cancellation, progress, and
result/error correlation. The application invocation envelope is intentionally
version 2 and is shared by the direct HTTP, MCP, and WebSocket paths:

```json
{
  "version": 2,
  "type": "invoke",
  "request_id": "request-1",
  "tool_name": "terminal.exec",
  "arguments": {"command_id": "pwd"}
}
```

The strict v2 model is `InvokeMessage`; it carries the provider-neutral
`tool_name` and bounded `arguments` fields shown below.

`tool_name` is a bounded named provider route. `arguments` is a bounded JSON
object validated against the selected provider descriptor before dispatch. The
arguments object is not an authority dictionary: it cannot contain a handler,
module, executable, endpoint, or arbitrary method.
Provider metadata is represented by bounded `ProviderToolDescriptor` values;
callers cannot supply provider descriptors or schemas.

The server accepts one in-flight invocation per device. Authentication happens
before descriptor lookup or provider dispatch. Unknown, unavailable,
unselected, malformed, or policy-blocked tools fail closed.

## Provider results

The result is a bounded MCP-compatible provider result. It preserves provider
content blocks, structured JSON content, and the error flag without converting
Browser or CUA values into Relay-owned operation DTOs:

```json
{
  "request_id": "request-1",
  "result": {
    "content": [{"type": "text", "text": "provider output"}],
    "structuredContent": {"ok": true},
    "isError": false
  }
}
```

Supported content is bounded by the Relay result policy. Text and image content
are supported where the provider exposes them; image data is bounded and the
MIME type is validated. Resource URIs are subject to the safe URI policy. The
Relay rejects oversized, malformed, or non-serializable results.

A provider error is returned as an MCP error result or a sanitized HTTP/WebSocket
error. Credentials, authorization headers, local paths, and raw provider
connection details are not copied into errors or logs.

## Direct HTTP control

The direct endpoint is:

```text
POST /v2/devices/{device_id}/invoke
Authorization: Bearer [REDACTED]
Content-Type: application/json
```

Request body:

```json
{
  "tool_name": "terminal.exec",
  "arguments": {"command_id": "pwd"},
  "timeout_seconds": 5
}
```

`timeout_seconds` is optional and is clamped to the configured server range.
The request model rejects unknown top-level fields, non-object arguments, and
invalid JSON bounds. The endpoint creates a fresh internal v2 request ID and
routes through the same Registry used by MCP and the authenticated Agent
WebSocket.

Status codes:

- `200`: provider result returned with the generated `request_id`;
- `401`: missing or invalid control Bearer token;
- `404`: unknown device;
- `409`: device busy or tool not announced/selected;
- `422`: descriptor argument validation failed;
- `502`: sanitized provider/remote-agent failure or invalid provider result;
- `503`: device is offline;
- `504`: bounded invocation timeout; the server attempts cancellation.

The direct endpoint never accepts a tool handler, code string, module path,
executable path, provider endpoint, credentials, or caller-defined descriptor.

## Agent WebSocket dispatch

After authenticated registration and capability announcement, the server sends
only the selected provider tool name and validated arguments:

```json
{
  "version": 2,
  "type": "invoke",
  "request_id": "request-1",
  "tool_name": "system.ping",
  "arguments": {}
}
```

The Agent resolves the name against its selected descriptor routes, validates
the arguments defensively, and calls exactly one provider client. A provider
result is bounded before the correlated result frame is sent. Cancellation and
timeout use independent control messages; a late result is discarded and never
becomes a second terminal response.

The registration announcement contains the selected descriptors only. The
server remains authoritative for announced-tool membership and does not accept
provider definitions from an MCP caller.

## Terminal, System, Browser, and CUA

- `terminal.exec` accepts only the fixed command IDs `pwd`, `whoami`,
  `python_version`, `git_status`, and `git_branch`. It is not a shell and does
  not accept command text, arguments, environment overrides, paths, or an
  executable name.
- `system.ping` is an Agent-local provider tool. `relay_device_status` is a
  separate Server-local MCP status tool and does not dispatch to the Agent.
- Browser and CUA tools are individually named public MCP tools selected from
  bounded provider `tools/list` descriptors. Provider calls cross the local
  boundary through `tools/call`; there is no public generic invoke or execute
  tool.
- Browser origin policy, Playwright lifecycle, CUA driver lifecycle, timeout,
  cancellation, and cleanup stay in the provider boundary.

## Security invariants

The v2 migration does not widen authority. All identifiers, schemas, arguments,
content, and results remain size/depth bounded and strict. Provider processes
are locally configured and owned by the Agent; network callers cannot choose a
provider endpoint or pass Relay credentials to a provider subprocess.

Never place tokens, Bearer headers, passwords, connection strings, or personal
data in protocol examples, logs, or test artifacts. Use `[REDACTED]` in
examples when a credential-shaped value is needed.
