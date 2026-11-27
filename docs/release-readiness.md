# Release readiness workflow

A reusable, **opt-in** GitHub Actions workflow that checks whether a repository
is ready to be released. It is **read-only**: it never publishes releases,
pushes tags, edits files, uses secrets or changes repository or organization
settings. Nothing is enrolled automatically. A repository uses it only after a
maintainer adds a caller workflow.

| Category | What is checked |
|---|---|
| Documents | Required documents (default `README`, `LICENSE`, `CHANGELOG`) exist and are not empty. Recommended ones (default `SECURITY`, `CONTRIBUTING`, `CODE_OF_CONDUCT`) only warn. Stub files and unfilled licence placeholders such as `[year]` also warn. |
| Version | The release version (from the tag, or the `version` input) is valid for the chosen scheme, and every version-bearing file declares that same version. |
| Changelog | The changelog has exactly one non-empty entry for the release version. By default a missing release date is a warning. When a tag or explicit version is checked, a non-empty `Unreleased` section is also a warning. |
| Checksums | Optional. Manifests such as `SHA256SUMS` are parsed and every listed file is hashed and compared. Unsafe paths, weak algorithms (MD5, SHA-1), duplicates, missing files and unlisted artifacts are reported. |

Each finding is **PASS**, **WARN**, **FAIL** or **SKIP**, shown in the job summary
as text with an icon, and failures and warnings also become annotations:

| State | Meaning | Blocks? |
|---|---|---|
| ✅ PASS | Requirement met | no |
| ⚠️ WARN | Should be fixed; release is still possible | only with `fail-on-warnings: true` |
| ❌ FAIL | Release is not ready | yes, unless `enforce: false` |
| ⏭️ SKIP | Not applicable or not configured (for example no checksum manifest in `auto` mode) | no |

The overall result is the worst state found.

## Quick start

Add `.github/workflows/release-readiness.yml` to the repository:

```yaml
name: Release readiness

on:
  pull_request:
    branches: ["release/**"]
  push:
    tags: ["v*"]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  release-readiness:
    permissions:
      contents: read
    uses: JSP-IT-Solutions-Group/.github/.github/workflows/release-readiness.yml@release-readiness-v1
    with:
      tooling-ref: release-readiness-v1
```

The same file is offered as an opt-in template under **Actions → New workflow**
in organization repositories (`workflow-templates/release-readiness.yml`).

> **Pin both refs together.** A reusable workflow cannot see which ref it was
> called with, so the checker script is checked out separately using
> `tooling-ref`. Always set `tooling-ref` to the same tag or commit SHA as the
> `uses:` line. For the strongest guarantee, pin both to a full commit SHA:
>
> ```yaml
> uses: JSP-IT-Solutions-Group/.github/.github/workflows/release-readiness.yml@<40-char-sha> # release-readiness-v1.0.0
> with:
>   tooling-ref: <40-char-sha>
> ```

More examples:

- [`caller-basic.yml`](../release-readiness/examples/caller-basic.yml): pull requests to release branches, tags and manual runs with an optional version.
- [`caller-binary-release.yml`](../release-readiness/examples/caller-binary-release.yml): verifies the checksums of binaries built in the same run and passed on as an artifact.
- [`caller-monorepo.yml`](../release-readiness/examples/caller-monorepo.yml): one check per package, each with its own tag prefix.
- Configuration files: [`python-library.json`](../release-readiness/examples/python-library.json), [`dotnet-binary.json`](../release-readiness/examples/dotnet-binary.json), [`node-package-in-monorepo.json`](../release-readiness/examples/node-package-in-monorepo.json).

## Where the release version comes from

1. The `version` input (or `version.expected` in the config). A leading tag prefix is stripped, so both `1.2.3` and `v1.2.3` work.
2. On tag builds, the tag name minus `tag-prefix` (default `v`). If the tag does not start with the prefix, that is a FAIL.
3. Otherwise, for example on a pull request, there is no expected version. The check then confirms that all version files agree and uses that version for the changelog check.

## Inputs

All inputs are optional. Empty string inputs fall back to the configuration file, then to the built-in defaults.

| Input | Default | Description |
|---|---|---|
| `working-directory` | `.` | Project directory, relative to the repository root. All other paths are relative to it. |
| `config-path` | `.github/release-readiness.json` if present | Configuration file, relative to `working-directory`. |
| `profile` | `auto` | Language profile, see [Profiles](#profiles). |
| `project-type` | `library` | `library`, `application` or `binary`, see [Project types](#project-types). |
| `version` | tag | Expected release version. |
| `tag-prefix` | `v` | Prefix stripped from tags. Use `none` for tags without a prefix. |
| `version-scheme` | profile default | `semver`, `pep440`, `calver` or `any`. |
| `required-documents` | `README,LICENSE,CHANGELOG` | Comma-separated logical names or relative paths, or `none`. |
| `recommended-documents` | `SECURITY,CONTRIBUTING,CODE_OF_CONDUCT` | Comma-separated logical names or relative paths, or `none`. |
| `changelog-path` | autodetect | Changelog file. Default search: `CHANGELOG.md`, `CHANGELOG`, `CHANGES.md`, `HISTORY.md`, `NEWS.md` (root, then `docs/`). |
| `checksums` | `auto` | `off`, `auto` (verify when a manifest exists) or `required`. |
| `checksum-manifest` | autodetect | Comma-separated manifest paths (globs allowed). |
| `artifacts-dir` | `dist` | Directory with release artifacts and manifests. |
| `artifact-name` | — | Downloads this artifact from the **same workflow run** and checks it instead of `artifacts-dir`. |
| `fail-on-warnings` | `false` | Treat warnings as failures. |
| `enforce` | `true` | Fail the job when the result is `fail`. Set `false` to only report (the result is still available as an output). |
| `tooling-ref` | `release-readiness-v1` | Ref of this repository to load the checker from. Keep it the same as the `uses:` ref. |
| `runs-on` | `ubuntu-24.04` | Runner label. Self-hosted runners need `bash` and Python 3.11 or newer. |

### Outputs

| Output | Description |
|---|---|
| `result` | `pass`, `warn`, `fail`, or `error` when the checker could not run. |
| `version` | The release version that was checked. Empty if it could not be determined or is not a safe single-line value. |
| `failures` / `warnings` | Number of failed checks and warnings. |

Example of gating a later job on the result:

```yaml
  notify:
    needs: release-readiness
    if: needs.release-readiness.outputs.result == 'warn'
    runs-on: ubuntu-24.04
    steps:
      - run: echo "Release is possible but has warnings"
```

## Configuration file

Optional. Place it at `.github/release-readiness.json` inside the working
directory (or point `config-path` at it). Unknown keys are errors, so typos do
not silently disable a check. Editors validate it via the
[JSON Schema](../release-readiness/config.schema.json).

```json
{
  "$schema": "https://raw.githubusercontent.com/JSP-IT-Solutions-Group/.github/release-readiness-v1/release-readiness/config.schema.json",
  "profile": "python",
  "project_type": "library",
  "fail_on_warnings": false,
  "documents": {
    "required": ["README", "LICENSE", "CHANGELOG"],
    "recommended": ["SECURITY", "CONTRIBUTING", "CODE_OF_CONDUCT"],
    "search_paths": [".", ".github", "docs"],
    "stub_bytes": 64
  },
  "version": {
    "tag_prefix": "v",
    "scheme": "pep440",
    "auto_detect": true,
    "sources": [
      { "type": "regex", "path": "src/pkg/__init__.py", "pattern": "^__version__ = \"(?P<version>[^\"]+)\"" }
    ]
  },
  "changelog": { "path": "CHANGELOG.md", "require_entry": true, "date": "warn", "warn_unreleased": true },
  "checksums": { "mode": "auto", "artifacts_dir": "dist", "manifests": [], "algorithm": "auto", "unlisted": "warn" }
}
```

Precedence, from lowest to highest: built-in defaults, project-type preset,
configuration file, workflow inputs.

**Documents.** Logical names are `README`, `LICENSE`, `CHANGELOG`, `SECURITY`,
`CONTRIBUTING`, `CODE_OF_CONDUCT`, `SUPPORT`, `NOTICE` and `AUTHORS`. Each one
matches common file names case-insensitively (for example `LICENSE`, `LICENSE.md`,
`COPYING`). `LICENSE`, `NOTICE` and `AUTHORS` are looked up at the project root
only. The others are searched in `search_paths`, the same places GitHub
recognises. Any other entry is treated as a relative file path, for example
`docs/INSTALL.md`.

**Version sources.** Configured sources are required (`"required": false` makes
one optional). With `auto_detect`, the profile's well-known files are added
when they exist. A file that exists but declares no static version is reported
as SKIP with the reason, for example a dynamic `pyproject.toml` version or a
`.csproj` without `<Version>`.

| `type` | Reads |
|---|---|
| `plain` | First non-comment line of a file such as `VERSION` |
| `json` / `toml` | Dotted `key` (default `version`) |
| `regex` | `pattern` with a named group `version` or a first capture group |
| `package-json`, `composer`, `vcpkg` | `version` (`vcpkg`: also `version-semver`, `version-string`, `version-date`) |
| `package-lock` | Top-level `version` and `packages[""].version` |
| `pyproject` | `project.version`, else `tool.poetry.version` |
| `setup-cfg` | `[metadata] version` (`attr:` and `file:` are skipped) |
| `cargo` | `package.version` or `workspace.package.version` |
| `msbuild` | `<Version>`, `<PackageVersion>`, `<VersionPrefix>`+`<VersionSuffix>` |
| `nuspec` | `<version>` (`$token$` values are skipped) |
| `cmake` | `project(... VERSION x.y.z)` |
| `meson` | `project(..., version: 'x.y.z')` |

### Profiles

| Profile | Detected by | Version files used when present | Default scheme |
|---|---|---|---|
| `python` | `pyproject.toml`, `setup.cfg`, `setup.py` | `pyproject.toml`, `setup.cfg`, `VERSION` | `pep440` |
| `node` | `package.json` | `package.json`, `package-lock.json`, `jsr.json`, `VERSION` | `semver` |
| `dotnet` | `*.sln`, `*.slnx`, `*.csproj`, `*.fsproj`, `Directory.Build.props` | `Directory.Build.props`, `*.csproj`, `*.fsproj`, `src/*/*.csproj`, `*.nuspec`, `VERSION` | `semver` |
| `rust` | `Cargo.toml` | `Cargo.toml` | `semver` |
| `php` | `composer.json` | `composer.json`, `VERSION` | `semver` |
| `cpp` | `CMakeLists.txt`, `meson.build`, `vcpkg.json`, `conanfile.*` | `CMakeLists.txt`, `meson.build`, `vcpkg.json`, `VERSION` | `semver` |
| `generic` | fallback | `VERSION`, `VERSION.txt`, `version.txt` | `semver` |

`auto` (the default) combines every detected profile. If more than one
ecosystem is detected, the scheme defaults to `semver`.

### Project types

| Type | Differences from the defaults |
|---|---|
| `library` | None (checksums are verified only when a manifest exists). |
| `application` | Also recommends `SUPPORT`. |
| `binary` | A checksum manifest is **required** and every file in the artifacts directory must be listed (`unlisted: fail`). Signature files (`.asc`, `.sig`, `.minisig`, `.sigstore`, `.pem`, ...) are exempt. |

### Checksum manifests

Discovered in the artifacts directory: `SHA256SUMS`, `SHA512SUMS`,
`sha256sums.txt`, `checksums.txt` and similar names, plus `*.sha256` /
`*.sha384` / `*.sha512` sidecar files. `SHA256SUMS` and `SHA512SUMS` are also
discovered at the project root. Supported line formats:

```text
<hex>  file.tar.gz            # GNU coreutils (sha256sum), text mode
<hex> *file.zip               # GNU coreutils, binary mode
SHA256 (file.zip) = <hex>     # BSD / shasum --tag
<hex>                         # sidecar file containing only the digest
```

Paths are resolved relative to the manifest. Absolute paths, `..` segments and
symlinks that point outside the manifest directory are rejected. The algorithm
is inferred from the digest length (SHA-256/384/512). MD5 and SHA-1 are
rejected.

## Security model

- **Permissions:** `contents: read` only, at workflow and job level. Callers cannot grant the workflow more than they have themselves.
- **No secrets:** the workflow declares no secrets and needs none. Do not pass `secrets: inherit`.
- **Pinned actions:** every third-party action is pinned to a full commit SHA with a version comment. Dependabot proposes updates as pull requests for human review.
- **No credential persistence:** both checkouts use `persist-credentials: false`.
- **No script injection:** inputs reach the shell only through environment variables, never through `${{ }}` inside `run:`. A policy test enforces this.
- **Untrusted content:** file contents shown in the summary are escaped. Step outputs only carry validated single-line values. Configured paths must stay inside the working directory.
- **Fork pull requests** run with a read-only token, which is all the workflow needs. Use `pull_request`, never `pull_request_target`.
- **No network access** by the checker itself. It uses the Python standard library only.

## Limitations

- Only **Markdown** changelogs are parsed (ATX `#` headings). reStructuredText or plain-text changelogs produce a warning.
- Version comparison is exact string equality after the tag prefix is stripped. It does not normalise across schemes (for example PEP 440 `1.0.0rc1` and SemVer `1.0.0-rc.1` are different).
- Versions computed at build time (setuptools-scm, MinVer, Nerdbank.GitVersioning, `$(Property)` values) cannot be read statically. They are skipped, and the tag becomes the source of truth.
- Java/Maven, Gradle, Go and other ecosystems have no dedicated profile yet. Use `plain` or `regex` sources.
- Checksums are verified for files in the working tree or in an artifact from the **same run**. Assets attached to an existing GitHub Release are not downloaded.
- Signatures (GPG, Sigstore) are not verified, only exempted from the unlisted-file check.
- Licence files are checked for presence and placeholders, not for SPDX validity or compatibility.
- GitHub can show organization-wide default community health files for repositories that lack their own. The check deliberately looks only at the project's own files, because the defaults are not part of the source archive.
- Requires a runner with `bash` and Python ≥ 3.11 (GitHub-hosted Ubuntu runners have it).

## Running locally

```sh
python3 release-readiness/release_readiness.py --root path/to/project --version 1.2.3
python3 release-readiness/release_readiness.py --help
```

Exit codes: `0` pass or warnings, `1` failures, `2` configuration error.
`--json-out report.json` writes a machine-readable report.

Tests (synthetic fixtures only):

```sh
python3 -m unittest discover -s tests/release-readiness -v
```

## Versioning and releases of this tooling

The workflow and checker are versioned together using
[Semantic Versioning](https://semver.org/). The version is recorded in
`release-readiness/VERSION`, `__version__` in the checker and
`release-readiness/CHANGELOG.md`. A unit test keeps the three consistent.

- **Immutable tags:** `release-readiness-vX.Y.Z`.
- **Moving major tag:** `release-readiness-vX`, which points at the latest `X.*` release and is the default `tooling-ref`.
- **Breaking changes** bump the major version. Examples: removing or renaming an input, making a WARN a FAIL by default, or changing an output.
- New checks start as WARN or opt-in in a minor release.

Release procedure (performed by a maintainer, by hand, after review):

1. Merge the reviewed pull request that updates `VERSION`, `__version__` and the changelog date.
2. Confirm the self-test workflow passed on `main`.
3. Create the tag `release-readiness-vX.Y.Z` on that commit and move `release-readiness-vX` to it.
4. If the major version changed, update the `tooling-ref` default and the template.

Until the first tag is created, callers must pin `uses:` and `tooling-ref` to a
commit SHA on `main`.

## Maintenance and ownership

- **Owner:** JSP-IT Solutions Group maintainers. Review ownership is set in [`.github/CODEOWNERS`](../.github/CODEOWNERS) (currently the `JSP-IT` organization account, until a maintainers team exists).
- **Changes** go through pull requests. The *Release readiness self-test* workflow must pass: actionlint (with shellcheck), JSON validation, unit and policy tests, and end-to-end runs of the reusable workflow against passing and deliberately failing fixtures.
- **Action updates:** Dependabot proposes monthly grouped updates for `.github/workflows`. Mirror any pin change in `release-readiness/examples/*.yml`. A policy test fails if pins drift. The actionlint version and checksum in the self-test are updated by hand from the upstream release's `checksums.txt`.
- **Reporting problems:** open an issue in this repository with the job summary and the caller workflow (remove anything sensitive first).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `result=error`, "python3 ... is required" | Use a GitHub-hosted runner, or install Python 3.11+ on the self-hosted runner. |
| Tooling checkout fails with "couldn't find remote ref release-readiness-v1" | The tag does not exist yet. Pin `uses:` and `tooling-ref` to a commit SHA. |
| "Tag ... does not start with the configured prefix" | Set `tag-prefix` to your tag scheme, or `none`. |
| "Version files disagree" on a pull request | Bump all version files together, or exclude stale ones via `version.sources` with `auto_detect: false`. |
| A `.csproj` or `pyproject.toml` version is SKIP | The version is computed at build time. Rely on the tag, or add a static `regex` source. |
| Unexpected "Unlisted artifacts" | Add the file to the manifest, move it out of the artifacts directory, or set `checksums.unlisted` to `ignore`. |
