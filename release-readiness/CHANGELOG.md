# Changelog

All notable changes to the release-readiness workflow and checker are
documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Release tags are `release-readiness-vMAJOR.MINOR.PATCH`; the moving tag
`release-readiness-v1` points at the latest 1.x release.

## [Unreleased]

## [1.0.0] - Unreleased

Replace `Unreleased` in the heading above with the release date when tagging.

### Added

- Reusable workflow `.github/workflows/release-readiness.yml` (`workflow_call`).
- Read-only checker `release_readiness.py` (Python standard library only) for:
  required/recommended documents, version consistency (Python, Node.js, .NET,
  Rust, PHP, C/C++, generic `VERSION` files and custom regex sources),
  changelog entries, and SHA-256/384/512 checksum manifests.
- Language profiles (`auto`, `generic`, `python`, `node`, `dotnet`, `rust`,
  `php`, `cpp`) and project types (`library`, `application`, `binary`).
- Job summary with explicit PASS / WARN / FAIL / SKIP states, workflow
  annotations and job outputs.
- JSON Schema for `.github/release-readiness.json`.
- Synthetic fixtures, unit tests and a self-test workflow with actionlint.
