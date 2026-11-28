# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). Tags: `profile-consistency-vX.Y.Z`.

## [Unreleased]

## [1.4.0] - 2026-11-28

### Added

- `sync_projects.py`: generates the list of published open-source projects
  between `github-projects` markers from an account's public GitHub
  repositories that are not forks, archived or templates and have a licence
  and a published release. Names come from the repository name and
  descriptions from the About text; sorted by name; `list` and `table`
  formats. Private repositories are filtered out twice and never queried for
  releases.
- Sync workflow inputs `projects-owner`, `projects-format`,
  `projects-exclude` and `catalogue` (to sync only projects), and a `branch`
  output so callers can start required checks on the sync pull request.
- Example caller that starts a required check workflow on the sync branch.

### Changed

- The sync workflow combines catalogue and project changes into one pull
  request and is named "Profile sync". Callers keep the same permissions.

## [1.3.0] - 2026-11-28

### Added

- Read the website's machine-readable `/catalogue.json` (schema version 1)
  first, in both the check and the sync. Unknown fields are ignored;
  `count` vs products, undefined groups, duplicate ids and the language are
  verified.
- Catalogue sources are `|`-separated fallback lists. The defaults are
  `/catalogue.json`, then the HTML overview page; using a fallback is a
  warning that names the reason.

### Changed

- Default `catalogue-en-url`, `catalogue-fi-url` (check) and `catalogue-url`
  (sync) now list `/catalogue.json` first. The generated profile list is
  unchanged: product links come from the JSON, group links from the
  overview page (`--site-url`).

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

[Unreleased]: https://github.com/JSP-IT-Solutions-Group/.github/compare/profile-consistency-v1.4.0...main
[1.4.0]: https://github.com/JSP-IT-Solutions-Group/.github/compare/profile-consistency-v1.3.0...profile-consistency-v1.4.0
[1.3.0]: https://github.com/JSP-IT-Solutions-Group/.github/compare/profile-consistency-v1.2.0...profile-consistency-v1.3.0
[1.2.0]: https://github.com/JSP-IT-Solutions-Group/.github/compare/profile-consistency-v1.1.0...profile-consistency-v1.2.0
[1.1.0]: https://github.com/JSP-IT-Solutions-Group/.github/compare/profile-consistency-v1.0.0...profile-consistency-v1.1.0
[1.0.0]: https://github.com/JSP-IT-Solutions-Group/.github/tree/profile-consistency-v1.0.0
