# Agent Relay Roadmap

**Status:** experimental, pre-1.0
**Last updated:** 2026-08-03
**Source of truth:** this document is the only active roadmap for the repository.

## Direction

Agent Relay is an open-source local agent runtime with explicit permissions:

```text
hosted AI client -> Relay Server -> authenticated connection -> Relay Agent -> bounded local capability
```

The current priority is to make the open project coherent, reliable and usable
by others.

## Status at a glance

### Delivered

- Authenticated Relay Server and outbound Relay Agent lifecycle.
- Closed MCP tool surface with typed schemas and bounded results.
- Constrained Terminal capability without a shell or caller-supplied arguments.
- Native Linux Terminal, Browser and generic CUA-provider E2E gates with
  independent fixture oracles.
- Native Windows Terminal and headless Browser E2E gates.
- Docker AMD64/ARM64 image build, image-contract and CLI smoke validation.
- Shared MCP readiness, cancellation, timeout and cleanup contracts.
- Security model, Linux setup, Docker deployment and black-box capability
  documentation.
- MIT licensing, contributor guidance, vulnerability-reporting policy and
  changelog.

### Experimental or incomplete

- Windows CUA/UI Automation on the hosted Windows runner.
  The complete CI proof is not green yet. The remaining proof is:

  ```text
  descriptor inventory -> snapshot -> type -> click -> independent fixture event -> cleanup
  ```

- Release automation and a version-tagging process.
- Production deployment procedures, service management, credential rotation,
  observability and recovery runbooks.

### Explicitly out of scope for now

- A self-hosted runner as a requirement for the current CI or product path.
- Personal desktops, personal browser profiles, personal workspaces or real
  external websites in acceptance tests.
- Arbitrary shell execution, arbitrary paths, arbitrary browser control,
  screenshots/coordinates as a public CUA API, or unrestricted
  remote desktop behavior.
- A claim that Windows CUA is supported merely because a Windows job
  starts or a driver responds.

## Priority roadmap

### 1. Make the repository publishable

**Status:** active.

Make the public repository coherent before expanding the feature surface.

Scope:

- maintain the MIT license and package metadata;
- make the README state the experimental/pre-1.0 status and the exact current
  platform boundary;
- keep `docs/security.md` honest about missing isolation, RBAC and auditing;
- add contributor, security-reporting and release guidance where needed;
- remove stale claims and keep one active roadmap instead of parallel plans;
- verify that repository history, fixtures and artifacts contain no credentials,
  personal data or personal browser/desktop assumptions.

**Exit criteria:** a new contributor can understand what is supported, what is
experimental, how to run the safe local tests, and what must not be used in
production.

### 2. Close or classify the Windows CUA gate

**Status:** active technical investigation, not a product acceptance gate.

Use the existing full harness and `e2e-windows-cua` candidate. Do not replace
its end-to-end contract with weaker environment or direct-driver checks.

Scope:

- investigate the existing `tools/list` backend/Agent negotiation failure;
- verify the hosted runner's session prerequisites inside the complete gate;
- run the complete public MCP scenario if the hosted session is usable;
- require the independent fixture event and bounded cleanup;
- if hosted Windows cannot provide a repeatable interactive session, classify
  Windows CUA as unsupported/experimental instead of weakening the
  gate or introducing self-hosted infrastructure into the current path.

**Exit criteria:** either the complete sequence passes on the declared hosted
runner, or the limitation is documented clearly and Windows CUA is removed from
claims of current support. Neither result blocks the Linux/Windows core and
Browser product gates.

### 3. Harden the open-source core for third-party use

**Status:** after repository publication work.

Make the current local runtime reliable for operators other than the author.

Scope:

- stable version and compatibility policy for the protocol and MCP surface;
- explicit device enrollment and configuration lifecycle;
- deterministic reconnect, cancellation, timeout and process cleanup;
- documented token generation, storage, rotation and revocation;
- bounded diagnostics, sanitized artifacts and useful operational errors;
- smoke tests for a Linux Server with a Linux or Windows Agent without personal
  data;
- compatibility checks for supported Python and platform versions.

**Exit criteria:** an external operator can install, configure, run, diagnose,
stop and recover one Relay Server plus one Agent using only documented steps,
without copying secrets into source files or logs.

### 4. Validate the MVP

**Status:** pending after core stabilization.

Verify that the MVP presented in the README matches the code, documentation and
CI evidence.

Scope:

- run the documented Linux quick start;
- validate the supported MCP, Terminal, Browser and Linux CUA paths;
- confirm the Windows Terminal and Browser gates;
- keep Windows CUA experimental until its complete proof is green;
- verify security boundaries, tokens, cleanup and artifacts;
- remove the remaining stale or ambiguous claims.

**Exit criteria:** the MVP can be described in a few sentences, installed using
the published documentation, validated by the intended tests, and no
experimental capability is presented as supported.

## Working rules

- This file is the only active roadmap. Do not create another dated plan for
  routine implementation work.
- Implementation details belong in issues, pull requests and code comments;
  obsolete dated plans remain available through Git history and should not be
  restored as active documents.
- Every roadmap item must have an explicit status and exit criteria.
- A green unit test, a driver response or a preflight is not proof of a product
  capability by itself.
- Public documentation must distinguish **delivered**, **experimentally
  validated**, **unsupported** and **planned**.
- No commit, push, release, deployment or external CI action is implied by this
  roadmap; each action requires explicit authorization.
