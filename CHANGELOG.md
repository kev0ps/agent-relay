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
- Linux and Windows Terminal and CUA E2E gates.
- Linux CUA E2E and an experimental Windows CUA candidate gate.
- AMD64 and ARM64 Docker image contract and CLI smoke validation.

### Changed

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
- Documentation now distinguishes delivered, experimental and unsupported
  capabilities.
- Documentation is organized around setup, tools, protocol, security, and one
  cross-platform E2E reference instead of historical protocol and roadmap files.
- Historical and provider-version-specific reference documents were removed;
  implementation history remains available through Git.
