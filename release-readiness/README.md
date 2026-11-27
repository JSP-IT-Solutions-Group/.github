# release-readiness

Read-only release-readiness checks for JSP-IT Solutions Group repositories:
project documents, version consistency, changelog entries and optional
checksum manifests.

- Documentation and integration examples: [`docs/release-readiness.md`](../docs/release-readiness.md)
- Reusable workflow: [`.github/workflows/release-readiness.yml`](../.github/workflows/release-readiness.yml)
- Configuration schema: [`config.schema.json`](config.schema.json)
- Changelog: [`CHANGELOG.md`](CHANGELOG.md)

Run locally (Python 3.11 or newer, no dependencies):

```sh
python3 release-readiness/release_readiness.py --root path/to/project --version 1.2.3
```
