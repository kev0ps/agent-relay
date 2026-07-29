# Relay protocol v1

Transport: UTF-8 JSON text WebSocket, path `/ws/agent`. Objects are strict
(`version: 1`, unknown fields rejected); binary frames and oversized messages
are rejected. The configurable server/agent limit is 1 KiB to 1 MiB, default
128 KiB. JSON results are also limited to 64 KiB, depth 16, and 4,096 nodes.

## Sequence

```text
agent -> register -> server -> registered
agent -> capabilities -> server
agent -> heartbeat (every 15 s by default) -> server
control HTTP -> invoke -> server -> agent
agent -> progress* -> server
agent -> result | error -> server -> control HTTP response
server -> cancel (HTTP timeout/cancellation, best effort) -> agent
```

`register` must be the first frame: `device_id` (1–128,
`[A-Za-z0-9._-]+`) and token (1–256). The server compares the agent token and
accepts only one socket for the device. It responds:

```json
{"version":1,"type":"registered","device_id":"linux-dev-1"}
```

The agent then declares up to 16 tools in `capabilities`. The protocol permits
`system.ping`, `terminal.exec`, the five closed Browser operations below, and
the three closed Computer operations `computer.capture`, `computer.click`, and
`computer.type`.
The default pre-Browser agent advertises exactly `system.ping` and
`terminal.exec`. Browser names are advertised only when operator-enabled
backend readiness is confirmed in Task 6. `heartbeat` has no other field. Every
request has a `request_id` of 1–128 characters
`[A-Za-z0-9._:-]+`.

## Invocation and response

Authenticated HTTP control creates an `invoke` sent to the agent:

```json
{"version":1,"type":"invoke","request_id":"[REDACTED]","tool":"terminal.exec","command_id":"pwd"}
```

`system.ping` has no `command_id`. For `terminal.exec`, only `pwd`, `whoami`,
`python_version`, `git_status`, and `git_branch` are valid. Only one invocation
may be in flight for the device; a second receives `409`.

The Browser invoke messages are exactly:

```json
{"version":1,"type":"invoke","request_id":"[REDACTED]","tool":"browser.list_tabs"}
{"version":1,"type":"invoke","request_id":"[REDACTED]","tool":"browser.navigate","url":"https://example.test/"}
{"version":1,"type":"invoke","request_id":"[REDACTED]","tool":"browser.read_page"}
{"version":1,"type":"invoke","request_id":"[REDACTED]","tool":"browser.fill","element_id":"opaque-id","value":"text"}
{"version":1,"type":"invoke","request_id":"[REDACTED]","tool":"browser.click","element_id":"opaque-id"}
```

URLs are 1–2,048 characters, opaque element IDs are 1–128, and fill values are
1–4,096. Browser results are closed semantic objects. `browser.list_tabs`
returns a `tabs` wrapper containing at most 6 closed tabs (`tab_id` 1–128,
`title` 0–256, `url` 1–2,048). `browser.read_page` returns those tab fields,
`text` (0–4,096), and at most 12 elements. Each element contains only an opaque
`element_id`, `role` (1–64), `name` (0–128), optional `value` (0–256),
`editable`, and `enabled`. Navigate, fill, and click return `tab_id`, optional
`element_id` (`null` for navigate), `url`, `title`, and `success`. These limits
keep worst-case UTF-8 serialized outputs below the 64 KiB result budget.

There is no Browser operation for arbitrary CDP, JavaScript, generic commands
or action/arguments dictionaries, headers, cookies, filesystem paths or access,
profile selection, device selection, or caller-controlled agent timeouts.

The Computer Use invoke messages are exactly:

```json
{"version":1,"type":"invoke","request_id":"[REDACTED]","tool":"computer.capture"}
{"version":1,"type":"invoke","request_id":"[REDACTED]","tool":"computer.click","element_id":"opaque-id"}
{"version":1,"type":"invoke","request_id":"[REDACTED]","tool":"computer.type","element_id":"opaque-id","text":"text"}
```

Element IDs are opaque, 1–128 characters, and scoped to the latest capture
generation. Type text is 1–4,096 characters and rejects every Unicode category
beginning `C` (control, format, surrogate, private-use, and unassigned code
points). Capture returns only `app` (1–128), `window_title` (0–256),
`generation` (1–128), and at most 12 elements. Each element contains only
`element_id`, `role` (1–64), `name` (0–128), required nullable `value` (0–256), and
`enabled`. Click and type return only `success`, `generation`, and the targeted
`element_id`. The bounded worst-case UTF-8 Computer capture remains below the
64 KiB result budget.

Computer Use has no coordinates, screenshots, key chords, clipboard, drag,
driver passthrough, process/window identifiers, delivery modes, or arbitrary
fields. Actions retain their semantic target; `computer.type` never types into
an implicit current selection. A click is dispatched at most once. A non-error
structured driver response is public success even when it contains
`verified:false`; only the independent E2E fixture oracle proves the side effect.

The agent finishes with exactly one correlated `result`:

```json
{"version":1,"type":"result","request_id":"[REDACTED]","result":{"pong":true}}
```

or an `error`, where `error.code` is 1–64 characters
`[a-z0-9_.-]+` and `error.message` is at most 512 characters:

```json
{"version":1,"type":"error","request_id":"[REDACTED]","error":{"code":"command_failed","message":"configured command failed"}}
```

`progress` is accepted during the single invocation: `progress` is 0–100 and
`message` is at most 512 characters. It is retained in memory only and not
returned by the current HTTP API. After a result, error, timeout, or
cancellation, a late response/progress frame closes the socket as a protocol
error.

The server may send:

```json
{"version":1,"type":"cancel","request_id":"[REDACTED]","reason":"control request cancelled or timed out"}
```

`reason` is 1–256 characters. Sending is best-effort and time-limited; the
agent cancels the corresponding active action and sends no late result.

## HTTP, authentication, and errors

`POST /v1/devices/{device_id}/invoke` requires exactly `Authorization:
Bearer [REDACTED]`, distinct from the agent token. The body is a closed union
discriminated by `tool`; each operation accepts only its fields above (or
`command_id` for `terminal.exec`) and the compatibility-only optional
`timeout_seconds`. MCP is authoritative. The server bounds the compatibility
timeout (0.1–30 s by default).

- `401`: missing or invalid Bearer token.
- `404`: unknown device; `503`: disconnected device.
- `409`: invocation in progress or undeclared capability.
- `504`: agent has not responded before timeout; the server attempts `cancel`.
- `502`: correlated error returned by the agent.

A successful response is `{ "request_id": "[REDACTED]", "result": {…} }`.
Never log the `register` token or Bearer header.
