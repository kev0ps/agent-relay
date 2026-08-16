# Agent Relay contributor contract

This file defines the repository-wide working agreement for every coding agent.

Direct user instructions override workflow preferences in this file. They do
not silently waive security boundaries or evidence requirements. External
actions require an explicit user request or approval. If an instruction
conflicts with a critical boundary, stop and surface the conflict.

## Project mission

Agent Relay lets an MCP-compatible hosted agent use an explicitly enabled set of
capabilities on a machine controlled by the operator. The controlled device
opens the outbound connection and must not become a general-purpose remote shell
or remote desktop.

The public request path is:

```text
MCP client -> /mcp -> Relay Server -> authenticated WebSocket
           -> Relay Agent -> enabled local capability -> closed result
```

`relay_device_status` is the exception: it is server-local and does not dispatch
a WebSocket invocation.

## Sources of truth

Read only the files relevant to the task. Use `README.md` and `CONTRIBUTING.md`
for project scope and workflow, then inspect the relevant code, tests, and
documentation for the behavior being changed.

When these sources disagree, surface the drift. When making a change, update the
smallest coherent set of code, tests, and documentation.

## Setup commands

Agent Relay requires Python 3.11 or newer and uses `uv` for dependency and
environment management.

Install the locked development environment with:

```sh
uv sync --locked --group dev
```

Do not hand-edit `uv.lock`. Regenerate it with `uv` only when dependency changes
are part of the task.

Browser and Computer Use checks require their optional dependencies and local
configuration. Follow the relevant setup guide before running those checks.

## Testing instructions

Run the narrowest relevant test first. Before reporting completion, run the
applicable repository checks:

```sh
uv run --frozen python -m pytest -q -m "not integration"
uv run --frozen python -m pytest -q -m integration
uv run --frozen ruff check .
uv lock --check
git diff --check
```

- Add or update focused tests when behavior or a public contract changes.
- For a defect, add a regression test that reproduces the failure when practical.
- Report only checks and runtime evidence that were actually obtained. A mock,
  preflight, driver response, or unit test is not product end-to-end proof.
- If a platform-specific check cannot run locally, state which check was not run
  and why.

## Code and change guidelines

- Inspect the current Git status before editing and preserve pre-existing or
  unrelated worktree changes.
- Keep changes focused. Avoid unrelated refactors, dependency upgrades,
  formatting churn, or speculative functionality.
- Follow the Python and Ruff settings in `pyproject.toml`.
- Update documentation in the same change when behavior, setup, limits, or
  public claims change.
- Distinguish local test results from CI or live runtime evidence.

## Security considerations

- Keep public capabilities explicit, bounded, and limited to what the operator
  has enabled.
- Do not turn a constrained capability into generic remote execution or an
  unrestricted provider passthrough.
- Preserve authentication, authorization boundaries, input validation, bounded
  outputs, timeouts, cancellation, and cleanup when changing those areas.
- If a change expands what a caller can invoke or access, update the relevant
  code, tests, and documentation and call out the boundary change.
- Do not expose or commit credentials, tokens, personal data, or unsanitized
  artifacts.
- Use synthetic fixtures, temporary workspaces, and isolated profiles or
  sessions for tests.

## Git and external actions

Do not create commits or tags, push changes, open or modify pull requests,
trigger remote workflows, publish artifacts, deploy infrastructure, or connect
external accounts without explicit user approval.
