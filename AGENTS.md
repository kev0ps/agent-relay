# Agent Relay contributor contract

This file defines the repository-wide working contract for coding agents and
human contributors. It applies equally to Hermes, Forge, Codex CLI, Codex
Desktop, and any other tool working directly in this repository. No Hermes
orchestration context is required to understand or follow it.

A direct user instruction overrides workflow preferences in this file, but it
does not silently waive security boundaries, evidence requirements, or the need
for explicit approval before external actions. If an instruction conflicts with
a safety invariant, stop and surface the conflict.

## Project mission

Agent Relay lets an MCP-compatible hosted agent use a deliberately small set of
capabilities on a machine controlled by the operator. The controlled device
opens the outbound connection; it does not become a general-purpose remote
shell or remote desktop.

The public request path is:

```text
MCP client -> /mcp -> Relay Server -> authenticated WebSocket
           -> Relay Agent -> enabled local capability -> closed result
```

`relay_device_status` is the exception: it is server-local and does not dispatch
a WebSocket invocation.

## Repository map and sources of truth

- `src/agent_relay/protocol.py`: strict wire models, limits, and tool contracts.
- `src/agent_relay/server.py`: authentication, connection registry, dispatch,
  timeout, cancellation, and HTTP/MCP-facing behavior.
- `src/agent_relay/agent.py`: outbound agent lifecycle and capability dispatch.
- `src/agent_relay/mcp_facade.py`: authoritative public MCP tool surface.
- `src/agent_relay/capabilities/`: local capability implementations.
- `tests/`: executable contracts. Prefer these over prose when they expose
  current behavior, then reconcile stale prose rather than weakening a test.
- `scripts/`: deterministic E2E harnesses and fixtures, not alternate product
  APIs.
- `docs/protocol-v2.md`: current generic invocation and direct-control contract.
- `docs/protocol-v1.md`: historical superseded protocol reference only.
- `docs/security.md`: threat model, deployment boundary, and honest limits.
- `docs/e2e-client-capabilities.md`: black-box E2E and independent-oracle
  contract.
- `.github/workflows/ci.yml`: remote gate definitions and pinned actions.
- `README.md`: product-facing current scope and supported quick start.
- `docs/ROADMAP.md`: the active product roadmap and current source of truth.
- `CONTRIBUTING.md`: contributor setup, checks, and pull-request expectations.
- `SECURITY.md`: private vulnerability-reporting policy.
- `LICENSE`: MIT licensing terms for the project.

When sources disagree, inspect code and executable tests, identify the drift,
and update the smallest correct set of code, tests, and documentation. Do not
quietly choose the interpretation that makes the task easiest.

## Non-negotiable product and security invariants

### Closed authority surface

- Never add a generic shell, arbitrary command/argument execution,
  caller-supplied filesystem path, unrestricted environment, driver
  passthrough, generic `execute()` method, or open action/arguments dictionary.
- Terminal execution remains a fixed command-ID allowlist and runs without a
  shell. An intentional allowlist change requires protocol, implementation,
  tests, MCP inventory, and documentation to change together.
- Public request and result models remain strict, typed, size-bounded,
  depth-bounded, collection-bounded, and closed to unknown fields.
- Do not relax a bound, schema, rejection, timeout, or concurrency rule merely
  to make a test, adapter, or CI job pass.

### Authentication and networking

- The canonical Relay Server MVP listens on `0.0.0.0:8000` so a trusted LAN
  Agent can connect; restrict host port `8000` with the host firewall to the
  intended LAN and never expose it directly to the Internet.
- `RELAY_ALLOW_INSECURE_WS=true` permits the Agent's `ws://` URL for a trusted
  LAN/test deployment; `false` rejects non-loopback `ws://` URLs and accepts
  `wss://` configuration. This URL policy is enforced by the Agent; Relay does
  not implement native TLS or reverse-proxy termination in this MVP.
- Agent and control credentials are distinct. Generate ephemeral credentials
  for tests and keep credential files private.
- Never print, log, commit, upload, or place in artifacts: tokens, Bearer
  headers, complete environments, URLs containing secrets, or personal data.
- Do not add credential configuration, rotation, remote secret storage, or
  external service access without explicit user approval.

### Browser and Computer Use boundaries

- Browser access is operator-enabled and origin-allowlisted. Use bounded
  structured locators; never expose Playwright handles or element IDs.
- Do not expose arbitrary CDP, JavaScript, headers, cookies, browser profile
  selection, downloads, filesystem access, or caller-controlled timeouts.
- CUA exposes only selected provider descriptors discovered from the configured
  driver. Do not expose screenshots, coordinates, key chords, clipboard
  contents, raw accessibility trees, process/window handles, or implicit-target
  typing.
- Preserve exact app/window identity checks, provider snapshot-token scoping,
  stale-element rejection, and at-most-once action dispatch.
- Tests use synthetic fixtures, temporary workspaces/profiles, and isolated
  desktop sessions. Never use a personal browser profile, desktop session,
  workspace, account, or external website as test material.

### Failure and lifecycle behavior

- Offline, busy, unsupported, malformed, stale, timeout, and cancellation paths
  fail closed.
- A request rejected before dispatch sends no Relay `invoke`.
- An accepted request has exactly one terminal result or error unless it is
  cancelled; cancellation does not accept or emit a late terminal result.
- Own every process and cleanup task explicitly. Timeout, cancellation, and
  teardown must terminate process trees within bounded time.
- Preserve the primary failure when cleanup also fails, but report both.
- A successful driver response is not proof of a side effect. Browser and
  Computer Use mutations require a structured public result plus exactly one
  correlated independent fixture event.
- An LLM, agent summary, process exit code, mocked adapter, or green unit test is
  never a substitute for the required runtime oracle.

### Containers, CI, and artifacts

- Do not introduce `--privileged`, Docker socket access, arbitrary client-side
  mounts, or weakened non-root/read-only/capability restrictions to obtain a
  green run.
- Keep diagnostics and artifacts synthetic, sanitized, regular-file-only,
  explicitly allowlisted, and size-bounded.
- Distinguish unit, loopback integration, native E2E, container image smoke,
  hardened container E2E, and manual platform acceptance. Report only the gate
  actually executed.
- A remote CI result is valid only for the exact reviewed commit SHA.

## Working agreement

### Before editing

1. Read this file and the source, tests, documentation, roadmap entry, and issue
   relevant to the requested behavior.
2. Run `git status --short --branch`. Preserve all pre-existing changes,
   including untracked files.
3. Never reset, clean, checkout over, rewrite, or stash another contributor's
   work. If isolation is needed, use a separate worktree from an agreed base.
4. Define a bounded scope, explicit non-goals, acceptance evidence, and any
   external gate that cannot be run locally.
5. Search downstream contract consumers before changing a tool, schema,
   inventory, CLI, workflow job, artifact set, or ordered list.

### Implementation

- Use test-driven development for behavior changes: write the smallest failing
  contract test, observe the expected failure, implement the minimum change,
  then refactor while green.
- For a defect, add a regression test that demonstrates the actual failure.
- Keep changes narrow. Do not bundle opportunistic refactors, dependency
  upgrades, formatting churn, or unrelated documentation edits.
- Preserve public compatibility unless the task explicitly changes it. If a
  contract changes intentionally, update all exact consumers rather than
  weakening fail-closed assertions.
- Keep dependencies locked. Do not hand-edit `uv.lock`; regenerate it with `uv`
  only when dependency changes are in scope.
- Keep Python compatible with the version declared in `pyproject.toml`; use
  Ruff's configured style and line length.
- Use isolated subprocess execution with fixed argument vectors and reduced
  environments. Do not use `shell=True` in product paths.
- Update documentation in the same change when behavior, limits, setup,
  security claims, supported platforms, or evidence status changes.
- In roadmap and status prose, distinguish clearly between planned, locally
  tested, remotely validated, and shipped behavior.

### Agent and review discipline

- One capable implementation context is the default. Do not create automatic
  chains of planners, implementers, and reviewers for routine work.
- Prefer deterministic evidence over another model opinion. Use a fresh,
  narrowly scoped independent review for authentication, public protocol,
  process ownership, timeout/cancellation/reconnect, native adapter,
  credential, or artifact-boundary changes.
- A coordinating agent remains responsible for checking the real diff and
  gates. Never accept a delegated agent's summary as completion evidence.
- An agent working directly in Codex Desktop or another repository-local tool
  follows the same contract and may complete bounded local work without first
  routing through Hermes or Forge.
- Parallel agents must use separate Git worktrees and must not race on one index
  or working tree.

## Verification

Run the narrowest relevant test first, then the repository gates appropriate to
the change. The normal local quality sequence is:

```sh
uv run --frozen pytest -q
uv run --frozen pytest -q -m integration
uv run --frozen ruff check .
uv lock --check
git diff --check
```

Additional rules:

- Use `uv sync --locked` when the locked environment needs installation or
  repair.
- For a focused change, run the directly affected test file or node before the
  full suite and report both results.
- Run real native, browser, desktop, or container harnesses when the changed
  boundary depends on them and the environment supports them.
- Do not call a mocked command-construction test an E2E pass.
- If Docker, a platform, credentials, or remote CI is unavailable, mark that
  gate as not run. Do not invent output or downgrade the requirement.
- Inspect `git diff --check`, `git diff --stat`, the full diff, and final
  `git status --short` before reporting completion.
- Tests must not leave generated tracked changes or unexpected untracked
  artifacts.

For documentation-only changes, at minimum run the affected documentation tests
when any exist, `git diff --check`, and a direct inspection of links, commands,
and statements against their source of truth.

## Approval boundary

Local reading, edits within the requested scope, tests, lint, diff inspection,
and non-destructive Git inspection are allowed as part of an authorized task.
The following require separate, explicit, task-specific user approval:

- creating a commit or tag;
- pushing or force-pushing;
- opening, updating, reviewing, or merging a pull request;
- triggering or rerunning remote CI or a remote workflow;
- publishing a release, package, image, or artifact;
- deploying or changing external infrastructure;
- posting to issues, chats, or other external systems;
- configuring credentials or connecting personal/external accounts.

Approval for implementation is not approval for any item above. Never infer
approval from a roadmap entry, an existing branch, prior approval on another
task, or the availability of credentials.

## Completion report

A completion report must state:

- files changed and the behavior or contract affected;
- exact verification commands run and their real outcomes;
- relevant gates not run and why;
- final Git status, including preserved pre-existing changes;
- whether any external action remains pending approval.

Do not claim a feature, platform, security property, E2E path, or CI gate is
complete unless the matching evidence was actually executed and inspected.
