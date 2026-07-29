"""Portable end-to-end kernel for Agent Relay.

This package isolates the deterministic MCP scenarios, the independent
fixture-oracle checks, and the Streamable HTTP MCP client used by both
the native platform harnesses.

Design contract (locked by tests/test_e2e_kernel.py):

* No Docker imports or container-inspection primitives.
* No Windows-only process APIs.
* No caller-supplied filesystem paths, shell text, or arbitrary argv.
* All public functions accept only typed runtime configurations and
  bound fixtures.
"""