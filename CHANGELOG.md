# Changelog

All notable changes to Agent Relay are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioned releases will follow [Semantic Versioning](https://semver.org/).
The project is currently pre-1.0 and has no formal release tag.

## [Unreleased]

### Added

- MIT license.
- Contributor guide and security-reporting policy.
- A single active project roadmap.

### Changed

- Browser, CUA, Terminal and System invocations now share the generic v2
  provider route and bounded descriptor/result validation.
- Browser exposes structured locators instead of Relay-generated element IDs;
  CUA publishes only explicitly selected driver descriptors.
- Documentation now distinguishes delivered, experimental and unsupported
  capabilities.
- Historical dated plans were removed; implementation history remains available
  through Git.
