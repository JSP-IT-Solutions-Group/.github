"""Tests for release-readiness/release_readiness.py.

Run from the repository root:

    python -m unittest discover -s tests/release-readiness -v

All inputs are synthetic fixtures under tests/release-readiness/fixtures or
temporary copies of them. Negative cases copy a passing fixture and break
exactly one thing, so each test proves a single check can fail.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = REPO / "release-readiness" / "release_readiness.py"

_spec = importlib.util.spec_from_file_location("release_readiness", SCRIPT)
rr = importlib.util.module_from_spec(_spec)
sys.modules["release_readiness"] = rr
_spec.loader.exec_module(rr)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def copy_fixture(self, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(FIXTURES / name, target)
        return target

    def run_checker(self, root: Path, *extra: str, ref_type: str = "branch",
                    ref_name: str = "main") -> tuple[int, dict, str, str]:
        out_json = self.tmp / "report.json"
        summary = self.tmp / "summary.md"
        outputs = self.tmp / "outputs.txt"
        for path in (out_json, summary, outputs):
            if path.exists():
                path.unlink()
        argv = ["--root", str(root), "--quiet", "--ref-type", ref_type, "--ref-name", ref_name,
                "--json-out", str(out_json), "--summary-file", str(summary),
                "--github-output", str(outputs), *extra]
        code = rr.main(argv)
        report = json.loads(out_json.read_text(encoding="utf-8"))
        return code, report, summary.read_text(encoding="utf-8"), outputs.read_text(encoding="utf-8")

    def findings(self, report: dict, category: str | None = None, status: str | None = None) -> list[dict]:
        return [f for f in report["findings"]
                if (category is None or f["category"] == category)
                and (status is None or f["status"] == status)]

    def assertFinding(self, report: dict, category: str, status: str, pattern: str) -> None:
        hits = [f for f in self.findings(report, category, status)
                if re.search(pattern, f["message"] + " " + " ".join(f["details"]) + " " + f["check"])]
        self.assertTrue(hits, f"no {status} finding in {category} matching {pattern!r}; got:\n"
                        + "\n".join(f"{f['status']} {f['category']} {f['check']}: {f['message']}"
                                    for f in report["findings"]))


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------


class PositiveFixtures(Base):
    def test_static_fixtures_pass(self) -> None:
        expected_versions = {"python-pass": "1.4.0", "node-pass": "2.0.0-rc.1",
                             "dotnet-pass": "3.1.0", "cpp-pass": "0.9.2"}
        for name, version in expected_versions.items():
            with self.subTest(fixture=name):
                code, report, summary, outputs = self.run_checker(FIXTURES / name)
                self.assertEqual(code, 0, summary)
                self.assertEqual(report["result"], "pass", summary)
                self.assertEqual(report["release_version"], version)
                self.assertEqual(report["counts"]["fail"], 0)
                self.assertIn("result=pass", outputs)
                self.assertIn(f"version={version}", outputs)

    def test_auto_profile_detection(self) -> None:
        detected = {name: self.run_checker(FIXTURES / name)[1]["profiles"]
                    for name in ("node-pass", "cpp-pass")}
        self.assertEqual(detected, {"node-pass": ["node"], "cpp-pass": ["cpp"]})

    def test_tag_matches_files(self) -> None:
        code, report, summary, _ = self.run_checker(FIXTURES / "python-pass",
                                                    ref_type="tag", ref_name="v1.4.0")
        self.assertEqual(code, 0, summary)
        self.assertEqual(report["version_origin"], "tag `v1.4.0`")

    def test_custom_tag_prefix(self) -> None:
        code, report, summary, _ = self.run_checker(FIXTURES / "node-pass", "--tag-prefix",
                                                    "synthetic-node-v", ref_type="tag",
                                                    ref_name="synthetic-node-v2.0.0-rc.1")
        self.assertEqual(code, 0, summary)
        self.assertEqual(report["release_version"], "2.0.0-rc.1")

    def test_explicit_version_input_strips_prefix(self) -> None:
        code, report, _, _ = self.run_checker(FIXTURES / "cpp-pass", "--version", "v0.9.2")
        self.assertEqual(code, 0)
        self.assertEqual(report["release_version"], "0.9.2")

    def test_dotnet_project_without_version_is_skipped_not_failed(self) -> None:
        _, report, _, _ = self.run_checker(FIXTURES / "dotnet-pass")
        self.assertFinding(report, "Version", "skip", r"no <Version> property")

    def test_signature_files_are_not_unlisted(self) -> None:
        _, report, _, _ = self.run_checker(FIXTURES / "dotnet-pass")
        self.assertFinding(report, "Checksums", "pass", r"covered by a manifest")

    def test_rust_workspace_and_php(self) -> None:
        rust = self.tmp / "rust"
        rust.mkdir()
        shutil.copy(FIXTURES / "cpp-pass" / "README.md", rust)
        shutil.copy(FIXTURES / "cpp-pass" / "LICENSE", rust)
        (rust / "CHANGELOG.md").write_text("# Changelog\n\n## v5.0.1 - 2026-01-02\n\n- fix\n",
                                           encoding="utf-8")
        (rust / "Cargo.toml").write_text('[workspace]\nmembers=["a"]\n\n[workspace.package]\n'
                                         'version = "5.0.1"\n', encoding="utf-8")
        code, report, summary, _ = self.run_checker(rust, "--recommended-documents", "none")
        self.assertEqual(code, 0, summary)
        self.assertEqual(report["profiles"], ["rust"])
        self.assertFinding(report, "Version", "pass", r"workspace\.package\.version")

        php = self.tmp / "php"
        shutil.copytree(rust, php)
        (php / "Cargo.toml").unlink()
        (php / "composer.json").write_text('{"name": "synthetic/php"}', encoding="utf-8")
        (php / "VERSION").write_text("5.0.1\n", encoding="utf-8")
        code, report, summary, _ = self.run_checker(php, "--recommended-documents", "none")
        self.assertEqual(code, 0, summary)
        self.assertFinding(report, "Version", "skip", r"no `version` field")
        self.assertFinding(report, "Version", "pass", r"VERSION")

    def test_pep440_scheme_for_python(self) -> None:
        root = self.copy_fixture("python-pass")
        for rel in ("pyproject.toml", "src/synthetic_pkg/__init__.py"):
            path = root / rel
            path.write_text(path.read_text(encoding="utf-8").replace("1.4.0", "1.4.0rc1"),
                            encoding="utf-8")
        changelog = root / "CHANGELOG.md"
        changelog.write_text(changelog.read_text(encoding="utf-8").replace("[1.4.0] -", "[1.4.0rc1] -"),
                             encoding="utf-8")
        code, report, summary, _ = self.run_checker(root, "--checksums", "off")
        self.assertEqual(code, 0, summary)
        self.assertFinding(report, "Version", "pass", r"valid pep440")

    def test_changelog_heading_styles(self) -> None:
        cases = ["## [1.2.3] - 2026-01-01", "## 1.2.3 (2026-01-01)", "## v1.2.3 - 2026-01-01",
                 "### Version 1.2.3 — 2026-01-01", "## [1.2.3](https://example.invalid) (2026-01-01)"]
        for heading in cases:
            with self.subTest(heading=heading):
                self.assertTrue(rr.heading_matches(heading.lstrip("# "), "1.2.3"))
        for heading in ("## 1.2.30", "## [1.2.3-rc.1]", "## 11.2.3", "## 1.2.3-rc.1 - 2026-01-01"):
            with self.subTest(heading=heading):
                self.assertFalse(rr.heading_matches(heading.lstrip("# "), "1.2.3"))

    def test_bsd_and_gnu_and_sidecar_manifests(self) -> None:
        root = self.tmp / "sums"
        (root / "dist").mkdir(parents=True)
        payload = b"synthetic payload\n"
        (root / "dist" / "a.bin").write_bytes(payload)
        (root / "dist" / "b.bin").write_bytes(payload + b"b")
        (root / "dist" / "c.bin").write_bytes(payload + b"c")
        (root / "dist" / "SHA256SUMS").write_text(
            f"# comment\n{sha256(payload)}  a.bin\nSHA256 (b.bin) = {sha256(payload + b'b')}\n",
            encoding="utf-8")
        (root / "dist" / "c.bin.sha256").write_text(f"{sha256(payload + b'c')} *c.bin\n", encoding="utf-8")
        code, report, summary, _ = self.run_checker(
            root, "--required-documents", "none", "--recommended-documents", "none",
            "--checksums", "required")
        self.assertFalse(self.findings(report, "Checksums", "fail"), summary)
        self.assertFinding(report, "Checksums", "pass", r"2 file\(s\) verified")
        self.assertFinding(report, "Checksums", "pass", r"c\.bin\.sha256")

    def test_artifacts_dir_absolute_override(self) -> None:
        root = self.copy_fixture("cpp-pass")
        moved = self.tmp / "downloaded-artifacts"
        shutil.move(str(root / "dist"), moved)
        code, report, summary, _ = self.run_checker(root, "--artifacts-dir", str(moved),
                                                    "--checksums", "required")
        self.assertEqual(code, 0, summary)

    def test_tooling_dogfood(self) -> None:
        """The tooling directory itself must be release-ready (warnings allowed)."""
        code, report, summary, _ = self.run_checker(REPO / "release-readiness",
                                                    "--config", "self-check.json")
        self.assertEqual(report["counts"]["fail"], 0, summary)
        self.assertEqual(report["release_version"], rr.__version__)
        self.assertEqual(len(self.findings(report, "Version", "pass")), 3)  # release + 2 sources

    def test_example_configs_and_fixture_configs_match_schema_keys(self) -> None:
        """Every shipped config must load without errors (unknown keys are fatal)."""
        configs = list(FIXTURES.glob("*/.github/release-readiness.json"))
        configs += list((REPO / "release-readiness" / "examples").glob("*.json"))
        configs.append(REPO / "release-readiness" / "self-check.json")
        self.assertGreaterEqual(len(configs), 4)
        schema = json.loads((REPO / "release-readiness" / "config.schema.json").read_text(encoding="utf-8"))
        top = set(schema["properties"])
        self.assertEqual(top, rr._ALLOWED_KEYS[None])
        for section in ("documents", "version", "changelog", "checksums"):
            self.assertEqual(set(schema["properties"][section]["properties"]), rr._ALLOWED_KEYS[section])
        for path in configs:
            with self.subTest(config=path.name):
                rr.validate_user_config(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


class NegativeDocuments(Base):
    def test_missing_required_document_fails(self) -> None:
        root = self.copy_fixture("python-pass")
        (root / "LICENSE").unlink()
        code, report, _, outputs = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Documents", "fail", r"LICENSE")
        self.assertIn("result=fail", outputs)

    def test_empty_required_document_fails(self) -> None:
        root = self.copy_fixture("python-pass")
        (root / "README.md").write_text("  \n\n", encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Documents", "fail", r"is empty")

    def test_stub_document_warns(self) -> None:
        root = self.copy_fixture("python-pass")
        (root / "SECURITY.md").write_text("# TODO\n", encoding="utf-8")
        code, report, _, outputs = self.run_checker(root)
        self.assertEqual(code, 0)
        self.assertEqual(report["result"], "warn")
        self.assertFinding(report, "Documents", "warn", r"stub")
        self.assertIn("result=warn", outputs)

    def test_license_placeholder_warns(self) -> None:
        root = self.copy_fixture("python-pass")
        lic = root / "LICENSE"
        lic.write_text(lic.read_text(encoding="utf-8").replace("2026 Synthetic Fixture Authors",
                                                               "[year] [fullname]"), encoding="utf-8")
        _, report, _, _ = self.run_checker(root)
        self.assertFinding(report, "Documents", "warn", r"placeholder")

    def test_missing_recommended_document_warns_and_mentions_org_default(self) -> None:
        root = self.copy_fixture("python-pass")
        (root / "CODE_OF_CONDUCT.md").unlink()
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 0)
        self.assertFinding(report, "Documents", "warn", r"organization-wide default")

    def test_custom_required_path(self) -> None:
        root = self.copy_fixture("python-pass")
        code, report, _, _ = self.run_checker(root, "--required-documents", "README,docs/INSTALL.md")
        self.assertEqual(code, 1)
        self.assertFinding(report, "Documents", "fail", r"docs/INSTALL\.md")

    def test_fail_on_warnings(self) -> None:
        root = self.copy_fixture("python-pass")
        (root / "CONTRIBUTING.md").unlink()
        code, report, summary, outputs = self.run_checker(root, "--fail-on-warnings")
        self.assertEqual(code, 1)
        self.assertEqual(report["result"], "fail")
        self.assertIn("treated as failures", summary)


class NegativeVersion(Base):
    def test_version_mismatch_between_files_fails(self) -> None:
        root = self.copy_fixture("python-pass")
        init = root / "src" / "synthetic_pkg" / "__init__.py"
        init.write_text(init.read_text(encoding="utf-8").replace("1.4.0", "1.4.1"), encoding="utf-8")
        code, report, _, outputs = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Version", "fail", r"disagree")
        self.assertIn("version=\n", outputs)

    def test_tag_mismatch_fails(self) -> None:
        code, report, _, _ = self.run_checker(FIXTURES / "python-pass", ref_type="tag",
                                              ref_name="v1.5.0")
        self.assertEqual(code, 1)
        self.assertFinding(report, "Version", "fail", r"Declares `1\.4\.0` but the release version is `1\.5\.0`")
        self.assertFinding(report, "Changelog", "fail", r"no entry for `1\.5\.0`")

    def test_tag_without_prefix_fails(self) -> None:
        code, report, _, _ = self.run_checker(FIXTURES / "python-pass", ref_type="tag",
                                              ref_name="release-1.4.0")
        self.assertEqual(code, 1)
        self.assertFinding(report, "Version", "fail", r"does not start with the configured prefix")

    def test_invalid_semver_fails(self) -> None:
        root = self.copy_fixture("node-pass")
        for name in ("package.json", "package-lock.json"):
            path = root / name
            path.write_text(path.read_text(encoding="utf-8").replace("2.0.0-rc.1", "2.0"), encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Version", "fail", r"not a valid semver")

    def test_required_source_missing_fails(self) -> None:
        root = self.copy_fixture("python-pass")
        (root / "src" / "synthetic_pkg" / "__init__.py").unlink()
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Version", "fail", r"Configured version source not found")

    def test_unparsable_source_fails(self) -> None:
        root = self.copy_fixture("node-pass")
        (root / "package.json").write_text("{ not json", encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Version", "fail", r"Cannot parse `package\.json`")

    def test_no_sources_warns(self) -> None:
        root = self.tmp / "empty"
        root.mkdir()
        _, report, _, _ = self.run_checker(root, "--required-documents", "none",
                                           "--recommended-documents", "none")
        self.assertFinding(report, "Version", "warn", r"No version-bearing files")
        self.assertFinding(report, "Changelog", "fail", r"No changelog found")

    def test_msbuild_computed_version_is_not_compared(self) -> None:
        root = self.copy_fixture("dotnet-pass")
        props = root / "Directory.Build.props"
        props.write_text(props.read_text(encoding="utf-8").replace(
            "<VersionPrefix>3.1.0</VersionPrefix>", "<Version>$(SyntheticVersion)</Version>"),
            encoding="utf-8")
        _, report, _, _ = self.run_checker(root, "--version", "3.1.0")
        self.assertFinding(report, "Version", "skip", r"computed by MSBuild")


class NegativeChangelog(Base):
    def test_missing_entry_fails(self) -> None:
        root = self.copy_fixture("cpp-pass")
        (root / "CHANGELOG.md").write_text("# Changelog\n\n## [0.9.1] - 2026-01-01\n\n- old\n",
                                           encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Changelog", "fail", r"no entry for `0\.9\.2`.*0\.9\.1")

    def test_empty_entry_fails(self) -> None:
        root = self.copy_fixture("cpp-pass")
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [0.9.2] - 2026-07-07\n\n### Added\n\n<!-- nothing yet -->\n\n"
            "## [0.9.1] - 2026-01-01\n\n- old\n", encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Changelog", "fail", r"is empty")

    def test_duplicate_entry_fails(self) -> None:
        root = self.copy_fixture("cpp-pass")
        (root / "CHANGELOG.md").write_text("## [0.9.2] - 2026-07-07\n\n- a\n\n## 0.9.2\n\n- b\n",
                                           encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Changelog", "fail", r"2 entries")

    def test_heading_in_code_fence_is_ignored(self) -> None:
        root = self.copy_fixture("cpp-pass")
        (root / "CHANGELOG.md").write_text("# Changelog\n\n```\n## [0.9.2] - 2026-07-07\n```\n",
                                           encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Changelog", "fail", r"no entry")

    def test_missing_date_warns_or_fails(self) -> None:
        root = self.copy_fixture("cpp-pass")
        (root / "CHANGELOG.md").write_text("# Changelog\n\n## [0.9.2]\n\n- change\n", encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 0)
        self.assertFinding(report, "Changelog", "warn", r"no release date")
        (root / ".github").mkdir()
        (root / ".github" / "release-readiness.json").write_text('{"changelog": {"date": "require"}}',
                                                                  encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Changelog", "fail", r"no release date")

    def test_invalid_and_future_dates(self) -> None:
        root = self.copy_fixture("cpp-pass")
        changelog = root / "CHANGELOG.md"
        changelog.write_text("## [0.9.2] - 2026-02-30\n\n- x\n", encoding="utf-8")
        self.assertFinding(self.run_checker(root)[1], "Changelog", "fail", r"not a valid calendar date")
        changelog.write_text("## [0.9.2] - 2999-01-01\n\n- x\n", encoding="utf-8")
        self.assertFinding(self.run_checker(root)[1], "Changelog", "warn", r"in the future")

    def test_unreleased_with_entries_warns_only_when_releasing(self) -> None:
        _, report, _, _ = self.run_checker(FIXTURES / "broken-release", ref_type="tag",
                                           ref_name="v1.2.0")
        self.assertFinding(report, "Changelog", "warn", r"Unreleased section still has entries")
        _, report, _, _ = self.run_checker(FIXTURES / "broken-release")  # ordinary branch build
        self.assertFalse([f for f in report["findings"] if f["check"] == "Unreleased section"])

    def test_tag_prefix_none_means_empty(self) -> None:
        code, report, summary, _ = self.run_checker(FIXTURES / "cpp-pass", "--tag-prefix", "none",
                                                    ref_type="tag", ref_name="0.9.2")
        self.assertEqual(code, 0, summary)

    def test_missing_changelog_fails(self) -> None:
        root = self.copy_fixture("cpp-pass")
        (root / "CHANGELOG.md").unlink()
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Changelog", "fail", r"No changelog found")


class NegativeChecksums(Base):
    def test_mismatch_fails(self) -> None:
        root = self.copy_fixture("python-pass")
        (root / "dist" / "synthetic_pkg-1.4.0.tar.gz").write_bytes(b"tampered")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Checksums", "fail", r"sha256 mismatch")

    def test_listed_file_missing_fails(self) -> None:
        root = self.copy_fixture("python-pass")
        (root / "dist" / "synthetic_pkg-1.4.0.tar.gz").unlink()
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Checksums", "fail", r"does not exist")

    def test_unlisted_file_policy(self) -> None:
        root = self.copy_fixture("python-pass")
        (root / "dist" / "extra.bin").write_bytes(b"extra")
        code, report, _, _ = self.run_checker(root)  # fixture config: unlisted = fail
        self.assertEqual(code, 1)
        self.assertFinding(report, "Checksums", "fail", r"extra\.bin")
        _, report, _, _ = self.run_checker(FIXTURES / "broken-release")  # default: warn
        self.assertFinding(report, "Checksums", "warn", r"broken-1\.2\.0-extra\.zip")

    def test_path_traversal_and_absolute_paths_rejected(self) -> None:
        root = self.copy_fixture("cpp-pass")
        secret = self.tmp / "outside.txt"
        secret.write_bytes(b"outside")
        digest = sha256(b"outside")
        (root / "dist" / "SHA256SUMS").write_text(
            f"{digest}  ../../outside.txt\n{digest}  {secret.as_posix()}\n{digest}  C:\\outside.txt\n",
            encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        finding = [f for f in self.findings(report, "Checksums", "fail") if "SHA256SUMS" in f["check"]][0]
        self.assertEqual(sum("not a safe relative path" in d for d in finding["details"]), 3)

    @unittest.skipIf(os.name == "nt", "creating symlinks needs extra privileges on Windows")
    def test_symlink_escape_rejected(self) -> None:
        root = self.copy_fixture("cpp-pass")
        secret = self.tmp / "outside.txt"
        secret.write_bytes(b"outside")
        (root / "dist" / "link.bin").symlink_to(secret)
        (root / "dist" / "SHA256SUMS").write_text(f"{sha256(b'outside')}  link.bin\n", encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Checksums", "fail", r"resolves outside")

    def test_weak_algorithms_rejected(self) -> None:
        root = self.copy_fixture("cpp-pass")
        data = (root / "dist" / "synthetic-0.9.2.zip").read_bytes()
        (root / "dist" / "synthetic-0.9.2.zip.sha256").unlink()
        (root / "dist" / "SHA256SUMS").write_text(
            f"{hashlib.md5(data).hexdigest()}  synthetic-0.9.2.zip\n", encoding="utf-8")
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 1)
        self.assertFinding(report, "Checksums", "fail", r"MD5, which is not accepted")

    def test_malformed_duplicate_and_empty_manifest(self) -> None:
        root = self.copy_fixture("cpp-pass")
        data = (root / "dist" / "synthetic-0.9.2.zip").read_bytes()
        (root / "dist" / "synthetic-0.9.2.zip.sha256").unlink()
        manifest = root / "dist" / "SHA256SUMS"
        manifest.write_text(f"{sha256(data)}  synthetic-0.9.2.zip\n{sha256(data)}  ./synthetic-0.9.2.zip\n"
                            "this is not a checksum line\n", encoding="utf-8")
        _, report, _, _ = self.run_checker(root)
        self.assertFinding(report, "Checksums", "fail", r"listed again")
        self.assertFinding(report, "Checksums", "fail", r"unrecognised format")
        manifest.write_text("# only a comment\n", encoding="utf-8")
        _, report, _, _ = self.run_checker(root)
        self.assertFinding(report, "Checksums", "fail", r"no entries")

    def test_required_mode_without_manifest_fails(self) -> None:
        root = self.copy_fixture("node-pass")
        code, report, _, _ = self.run_checker(root, "--checksums", "required")
        self.assertEqual(code, 1)
        self.assertFinding(report, "Checksums", "fail", r"No checksum manifest found")

    def test_binary_project_type_requires_manifest(self) -> None:
        root = self.copy_fixture("node-pass")
        code, report, _, _ = self.run_checker(root, "--project-type", "binary")
        self.assertEqual(code, 1)
        self.assertFinding(report, "Checksums", "fail", r"No checksum manifest found")

    def test_configured_manifest_missing_fails(self) -> None:
        root = self.copy_fixture("node-pass")
        code, report, _, _ = self.run_checker(root, "--checksum-manifest", "release/SHA256SUMS")
        self.assertEqual(code, 1)
        self.assertFinding(report, "Checksums", "fail", r"Configured checksum manifest not found")

    def test_off_mode_skips(self) -> None:
        root = self.copy_fixture("python-pass")
        (root / "dist" / "synthetic_pkg-1.4.0.tar.gz").write_bytes(b"tampered")
        code, report, _, _ = self.run_checker(root, "--checksums", "off")
        self.assertEqual(code, 0)
        self.assertFinding(report, "Checksums", "skip", r"turned off")


class NegativeConfiguration(Base):
    def write_config(self, root: Path, text: str) -> None:
        (root / ".github").mkdir(exist_ok=True)
        (root / ".github" / "release-readiness.json").write_text(text, encoding="utf-8")

    def test_invalid_json_is_a_config_error(self) -> None:
        root = self.copy_fixture("node-pass")
        self.write_config(root, "{ broken")
        code, report, summary, outputs = self.run_checker(root)
        self.assertEqual(code, 2)
        self.assertFinding(report, "Configuration", "fail", r"cannot parse")
        self.assertIn("FAIL", summary)
        self.assertIn("result=fail", outputs)

    def test_unknown_keys_are_rejected(self) -> None:
        root = self.copy_fixture("node-pass")
        for text, pattern in (('{"documnets": {}}', r"documnets"),
                              ('{"changelog": {"require_date": true}}', r"require_date"),
                              ('{"version": {"sources": [{"type": "plain", "path": "V", "file": "x"}]}}',
                               r"'file'")):
            with self.subTest(text=text):
                self.write_config(root, text)
                code, report, _, _ = self.run_checker(root)
                self.assertEqual(code, 2)
                self.assertFinding(report, "Configuration", "fail", pattern)

    def test_invalid_values_are_rejected(self) -> None:
        root = self.copy_fixture("node-pass")
        cases = [
            ("--profile", "cobol", r"unknown profile"),
            ("--project-type", "firmware", r"unknown project_type"),
            ("--checksums", "maybe", r"checksums\.mode"),
            ("--version-scheme", "roman", r"version\.scheme"),
            ("--changelog", "../outside.md", r"must not contain '\.\.'"),
            ("--required-documents", "/etc/passwd", r"relative path"),
            ("--config", "missing.json", r"not found"),
        ]
        for flag, value, pattern in cases:
            with self.subTest(flag=flag):
                code, report, _, _ = self.run_checker(root, flag, value)
                self.assertEqual(code, 2)
                self.assertFinding(report, "Configuration", "fail", pattern)

    def test_bad_regex_source(self) -> None:
        root = self.copy_fixture("node-pass")
        self.write_config(root, '{"version": {"sources": [{"type": "regex", "path": "x", "pattern": "("}]}}')
        code, report, _, _ = self.run_checker(root)
        self.assertEqual(code, 2)
        self.assertFinding(report, "Configuration", "fail", r"invalid regex")
        self.write_config(root, '{"version": {"sources": [{"type": "regex", "path": "x", "pattern": "v"}]}}')
        code, report, _, _ = self.run_checker(root)
        self.assertFinding(report, "Configuration", "fail", r"capture group")

    def test_missing_root(self) -> None:
        code, report, _, _ = self.run_checker(self.tmp / "does-not-exist")
        self.assertEqual(code, 2)
        self.assertFinding(report, "Configuration", "fail", r"not a directory")


class Output(Base):
    def test_summary_shows_explicit_states(self) -> None:
        _, _, summary, _ = self.run_checker(FIXTURES / "broken-release")
        self.assertIn("## Release readiness: \u274c FAIL", summary)
        for word in ("PASS", "WARN", "FAIL", "SKIP"):
            self.assertIn(word, summary)
        self.assertIn("| Category | Status |", summary)
        self.assertIn("publishes nothing", summary)

    def test_summary_escapes_untrusted_text(self) -> None:
        root = self.tmp / "inject"
        root.mkdir()
        (root / "VERSION").write_text("1.0.0|<img src=x onerror=alert(1)>\n", encoding="utf-8")
        _, _, summary, outputs = self.run_checker(root, "--version-scheme", "any")
        self.assertNotIn("<img", summary)
        self.assertIn("&lt;img", summary)
        self.assertIn("version=\n", outputs)  # unsafe value never reaches GITHUB_OUTPUT

    def test_annotation_escaping(self) -> None:
        report = rr.Report(root=".")
        report.add("Checksums", "a,b:c", "fail", "50% bad\nsecond line", path="dist/x,y")
        lines = rr.annotations(report, "packages/app")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("::error file=packages/app/dist/x%2Cy,title="))
        self.assertIn("a%2Cb%3Ac", lines[0])
        self.assertIn("50%25 bad%0Asecond line", lines[0])
        self.assertNotIn("\n", lines[0])

    def test_cli_entry_point(self) -> None:
        import subprocess
        proc = subprocess.run([sys.executable, "-I", str(SCRIPT), "--root",
                               str(FIXTURES / "broken-release"), "--annotations", "--ref-type", "branch"],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("::error ", proc.stdout)
        self.assertIn("::warning ", proc.stdout)


if __name__ == "__main__":
    unittest.main()
