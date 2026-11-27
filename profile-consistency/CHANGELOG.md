# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). Tags: `profile-consistency-vX.Y.Z`.

## [Unreleased]

## [1.0.0] - Unreleased

### Added

- Read-only checker comparing profile READMEs with the EN and FI catalogues
  using the pages' structured JSON data, with card and counter cross-checks.
- Language parity (counts, product ids, groups), profile count and link checks.
- Distinct "data unavailable" outcome (exit code 3) that is never reported as a
  discrepancy.
- Reusable workflow, weekly scheduled check, synthetic fixtures and tests.
