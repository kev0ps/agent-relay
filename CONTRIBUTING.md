# Contributing to Agent Relay

Agent Relay is experimental, pre-1.0 software. Contributions should preserve
its deliberately narrow authority surface and keep claims aligned with runtime
evidence.

## Before you start

Read:

- [`README.md`](README.md) for the current product boundary;
- [`AGENTS.md`](AGENTS.md) for repository-wide engineering and security rules;
- [`docs/ROADMAP.md`](docs/ROADMAP.md) for active priorities;
- [`SECURITY.md`](SECURITY.md) before reporting a vulnerability.

For a non-trivial change, open or reference an issue that states the intended
behavior, non-goals and acceptance evidence. Do not create another dated plan;
implementation details belong in issues and pull requests.

## Development setup

Install [`uv`](https://docs.astral.sh/uv/), clone the repository, then run:

```sh
uv sync --locked --group dev
```

Use synthetic fixtures, temporary workspaces and temporary browser profiles.
Never use personal credentials, sessions, files, profiles or external websites
as test data.

## Local checks

Run the narrowest relevant tests first. Before requesting review, run:

```sh
uv run --frozen pytest -q -m "not integration"
uv run --frozen pytest -q -m integration
uv run --frozen ruff check .
uv lock --check
git diff --check
```

If a platform-specific E2E gate cannot run locally, state that clearly in the
pull request and identify the exact CI evidence still required.

## Change rules

- Keep the MCP, Terminal, Browser and Computer Use surfaces closed and typed.
- Do not add generic shell execution, arbitrary paths, unrestricted browser
  passthrough, screenshots or coordinates to the public Computer Use API.
- Preserve authentication, origin allowlists, stale-element rejection, bounded
  outputs, timeouts, cancellation and process cleanup.
- Add or update tests when behavior or a public contract changes.
- Keep documentation claims narrower than the evidence. A preflight, mock,
  driver response or unit test is not product E2E proof.
- Do not commit secrets, tokens, personal data or unsanitized artifacts.

## Pull requests

Keep each pull request focused. Include:

- a concise description of the problem and solution;
- affected security and compatibility boundaries;
- exact commands run and their results;
- unrun or externally blocked checks;
- migration or rollback notes when applicable.

By contributing, you agree that your contribution is licensed under the
repository's [MIT License](LICENSE).
