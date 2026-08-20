# Changelog

All notable changes to Agent Relay are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioned releases will follow [Semantic Versioning](https://semver.org/).
The project is currently pre-1.0 and has no formal release tag.

## [Unreleased]

### Added

- MIT license.
- Contributor guide and security-reporting policy.
- YAML-first Server and Agent configuration with a private `.env` store and
  copyable tool profiles.
- Linux and Windows Terminal gates with a shared lifecycle and core scenario.
- Linux and Windows CUA gates with preinstalled Chrome, one shared browser
  fixture, scenario, and functional oracles.
- AMD64 and ARM64 Docker image contract and CLI smoke validation.

### Changed

- Python 3.14.4 is now the minimum and managed runtime for the package,
  installers, CI, and Docker image.
- Credentials now live in a private `.env` beside the selected YAML file and
  are read directly without dotenv environment injection. The former
  `secrets/` YAML layout and `*_TOKEN_FILE` runtime overrides are intentionally
  unsupported; existing installations must create `.env` manually before
  removing their old secret files.
- Windows uninstall retries `uv tool uninstall agent-relay` every 500 ms for
  up to 15 seconds and records each attempt in its status log.
- Runtime diagnostics use `[INFO]`, `[WARNING]`, and `[DEBUG]` prefixes, and
  validated tool calls emit only their internal tool name.
- CUA, Terminal and System invocations now share the generic v2 provider route
  and bounded descriptor/result validation. CUA publishes only explicitly
  selected driver descriptors, including its browser tools.
- The MCP facade now targets the Python MCP SDK 2.x with stateful Streamable
  HTTP and JSON responses; the Agent WebSocket protocol remains unchanged.
- `cua-driver` is optional for Server-only installs and remains selected by
  default for native Agent/local installs.
- Documentation now distinguishes delivered, experimental and unsupported
  capabilities.
- Documentation is organized around setup, tools, protocol, security, and one
  cross-platform E2E reference instead of historical protocol and roadmap files.
- Historical and provider-version-specific reference documents were removed;
  implementation history remains available through Git.
