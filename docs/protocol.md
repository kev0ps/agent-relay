# Agent Relay protocol

This document describes the current wire contract between the Relay Server,
Relay Agent, direct HTTP endpoint, and MCP facade. It is a protocol reference,
not a stability promise: Agent Relay is an experimental prototype and may still
make incompatible changes before its first release.

## Connection and versioning

The Agent opens an authenticated WebSocket connection to `/ws/agent`. The
initial handshake uses version 1 frames:

```text
Agent -> register(version=1) -> Server
Agent <- registered(version=1) <- Server
Agent -> capabilities(version=1) -> Server
```

`register` identifies the configured device. `capabilities` contains only the
tools selected by the operator and the matching bounded provider descriptors.
The Server accepts one connected Agent for the configured device.

The MCP facade uses the Python MCP SDK 2.x (`mcp>=2,<3`) and its stateful
Streamable HTTP transport with JSON responses. The public MCP server name is
`Agent Relay`; authentication and Host/Origin policy remain enforced by the
Relay application around the SDK transport. This SDK migration does not change
the Agent WebSocket frames or their version numbers described above.

After registration, application and lifecycle frames use version 2:

```text
Agent -> heartbeat(version=2) -> Server
Server -> invoke(version=2) -> Agent
Agent -> progress*(version=2) -> Server
Agent -> result | error(version=2) -> Server
Server -> cancel(version=2) -> Agent
```

All frames are strict UTF-8 JSON objects. Unknown fields, binary frames,
oversized messages, invalid identifiers, and malformed descriptors or results
are rejected.

## Invocation

The provider-neutral invocation envelope is shared by direct HTTP, MCP, and
WebSocket dispatch:

```json
{
  "version": 2,
  "type": "invoke",
  "request_id": "request-1",
  "tool_name": "terminal.exec",
  "arguments": {"command_id": "pwd"}
}
```

`tool_name` must name a tool announced by the connected Agent. `arguments` is a
bounded JSON object validated against that tool's descriptor before dispatch.
A caller cannot supply a handler, module, executable, provider endpoint, schema,
credential, or arbitrary method.

Only one invocation may be in flight for the device. Authentication occurs
before tool lookup or dispatch. Offline, busy, unknown, unavailable,
unselected, malformed, and policy-blocked calls fail closed.

## Provider results

Results retain the MCP-compatible provider shape:

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

The bounded result model supports text, image, audio, embedded-resource, and
resource-link content blocks, plus structured JSON content and provider error
state. Media data, collections, metadata, JSON depth, and resource URIs are
validated. Oversized, malformed, unsafe, or non-serializable results are
rejected.

Errors returned to remote callers are sanitized. Credentials, authorization
headers, local paths, and provider connection details must not be copied into
responses, logs, or artifacts.

## Direct HTTP control

The authenticated compatibility endpoint is:

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

`timeout_seconds` is optional and bounded by Server configuration. The Server
creates the internal request ID and routes the call through the same registry
and descriptor validation used by MCP.

Status codes:

- `200`: bounded provider result returned;
- `401`: missing or invalid control credential;
- `404`: unknown device;
- `409`: device busy or tool not announced;
- `422`: descriptor argument validation failed;
- `502`: sanitized Agent/provider failure or invalid provider result;
- `503`: device offline;
- `504`: bounded invocation timeout and cancellation attempt.

The MCP facade is the normal client interface. The direct endpoint exists for
closed compatibility and diagnostics; it does not bypass policy.

## Tool contracts

The public MCP inventory and copyable Agent allowlists are documented in
[`tools.md`](tools.md).

`terminal.exec` accepts only `pwd`, `whoami`, `python_version`, `git_status`,
and `git_branch`. CUA tools are discovered dynamically from the standard
provider, exposed publicly as `relay_cua_<name>`, and selected through the
versioned `none`, `standard`, or `full` profiles or by individual name. Native
and browser descriptors use the same validation, policy, logging, and execution
path. An application or window target is required only by a descriptor that
actually acts on one.

There is no public generic invoke tool, shell text, caller-supplied path,
arbitrary JavaScript, CDP passthrough, provider endpoint, screenshot or
coordinate-based desktop control.

## Cancellation and correlation

Every Agent-executed call receives a fresh bounded request ID. An accepted call
has exactly one terminal result or error unless it is cancelled. Cancellation
does not require a terminal result, and late progress or result frames are
rejected. Sending cancellation after an HTTP timeout or client disconnect is
best effort and time-bounded.

`relay_device_status` is Server-local. It does not allocate a Relay request ID
or send a WebSocket invocation.

## Limits and security

Identifiers, schemas, arguments, messages, collections, content blocks, and
results are size- and depth-bounded. Provider processes are configured and
owned locally by the Agent. Network callers cannot choose a provider process or
pass Relay credentials to it.

The exact executable limits and validation behavior live in
`src/agent_relay/protocol.py`, `src/agent_relay/provider_tools.py`,
`src/agent_relay/output_models.py`, and the corresponding tests. When prose and
those executable contracts disagree, the documentation must be corrected.
