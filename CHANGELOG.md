# Changelog

All notable changes to Agent Relay are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioned releases will follow [Semantic Versioning](https://semver.org/).
The project is currently pre-1.0 and has no formal release tag.

## [Unreleased]

### Added

- MIT license.
- Contributor guide and security-reporting policy.
- YAML-first Server and Agent configuration with private secret files and
  copyable tool profiles.
- Native Linux and Windows Terminal and Browser E2E gates.
- Native Linux CUA E2E and an experimental Windows CUA candidate gate.
- AMD64 and ARM64 Docker image contract and CLI smoke validation.

### Changed

- Browser, CUA, Terminal and System invocations now share the generic v2
  provider route and bounded descriptor/result validation.
- Browser exposes structured locators instead of Relay-generated element IDs;
  CUA publishes only explicitly selected driver descriptors.
- Documentation now distinguishes delivered, experimental and unsupported
  capabilities.
- Documentation is organized around setup, tools, protocol, security, and one
  cross-platform E2E reference instead of historical protocol and roadmap files.
- Historical and provider-version-specific reference documents were removed;
  implementation history remains available through Git.
