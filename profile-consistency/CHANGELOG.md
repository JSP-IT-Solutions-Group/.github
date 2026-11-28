# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). Tags: `profile-consistency-vX.Y.Z`.

## [Unreleased]

## [1.2.0] - 2026-11-28

### Added

- Retries when the catalogue data is unavailable (for example the site's bot
  protection page): new `attempts` (default 4) and `retry-delay-seconds`
  (default 90) inputs in both reusable workflows. Only the final attempt is
  reported; discrepancies and profile errors are never retried.

### Changed

- The consistency check job timeout is 30 minutes (was 15) to fit the retries.

## [1.1.0] - 2026-11-28

### Added

- `sync_profile.py`: updates the counts in a profile README and writes a
  generated list of all solutions, grouped by solution group, with direct links
  to the English product pages, between `jsp-catalogue` markers.
- Reusable workflow `profile-consistency-sync.yml`: opens or updates one pull
  request with the sync result, and closes it when it is no longer needed.
  Never pushes to the default branch and never merges.
- Consistency check: new "Profile list" check that the generated list matches
  the catalogue. Counts and links inside the generated list are not checked
  separately.

## [1.0.0] - 2026-11-27

### Added

- Read-only checker comparing profile READMEs with the EN and FI catalogues
  using the pages' structured JSON data, with card and counter cross-checks.
- Language parity (counts, product ids, groups), profile count and link checks.
- Distinct "data unavailable" outcome (exit code 3) that is never reported as a
  discrepancy.
- Reusable workflow, weekly scheduled check, synthetic fixtures and tests.

[Unreleased]: https://github.com/JSP-IT-Solutions-Group/.github/compare/profile-consistency-v1.2.0...main
[1.2.0]: https://github.com/JSP-IT-Solutions-Group/.github/compare/profile-consistency-v1.1.0...profile-consistency-v1.2.0
[1.1.0]: https://github.com/JSP-IT-Solutions-Group/.github/compare/profile-consistency-v1.0.0...profile-consistency-v1.1.0
[1.0.0]: https://github.com/JSP-IT-Solutions-Group/.github/tree/profile-consistency-v1.0.0
