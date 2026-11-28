"""Tests for profile-consistency/sync_projects.py (fake GitHub API, no network).

    python -m unittest discover -s tests/profile-consistency -v
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "profile-consistency"


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sp = _load("sync_projects")
OWNER = "example"


def repo(name: str, **overrides) -> dict:
    data = {"name": name, "private": False, "visibility": "public", "fork": False, "archived": False,
            "is_template": False, "owner": {"login": OWNER}, "license": {"spdx_id": "MIT"},
            "language": "Go", "description": f"Description of {name}",
            "html_url": f"https://github.com/{OWNER}/{name}"}
    data.update(overrides)
    return data


REPOS = [
    repo("sbom-release-diff", language="Rust", license={"spdx_id": "Apache-2.0"}),
    repo("ai-repo-rules-generator", language="Python", description="Uses | pipes and [brackets]"),
    repo("secret-internal-tool", private=True, visibility="private"),
    repo("sneaky", private=False, visibility="private"),
    repo("forked-thing", fork=True),
    repo("old-thing", archived=True),
    repo("template-thing", is_template=True),
    repo("no-licence", license=None),
    repo("unknown-licence", license={"spdx_id": "NOASSERTION"}),
    repo("no-release"),
    repo("draft-only"),
    repo(OWNER),          # profile repository
    repo(".github"),
    repo("excluded-one"),
]
RELEASES = {"sbom-release-diff": {"tag_name": "v0.1.0"}, "ai-repo-rules-generator": {"tag_name": "v0.3.0"},
            "secret-internal-tool": {"tag_name": "v1"}, "sneaky": {"tag_name": "v1"},
            "forked-thing": {"tag_name": "v1"}, "old-thing": {"tag_name": "v1"},
            "template-thing": {"tag_name": "v1"}, "no-licence": {"tag_name": "v1"},
            "unknown-licence": {"tag_name": "v1"}, "draft-only": {"tag_name": "v1", "draft": True},
            OWNER: {"tag_name": "v1"}, ".github": {"tag_name": "v1"}, "excluded-one": {"tag_name": "v1"}}


def fake_api(repos=None, releases=None, account_type="User", fail_on: str | None = None):
    repos = REPOS if repos is None else repos
    releases = RELEASES if releases is None else releases
    calls: list[str] = []

    def api(path: str):
        calls.append(path)
        if fail_on and fail_on in path:
            raise sp.Unavailable(f"HTTP 503 for {path}")
        if path == f"/users/{OWNER}":
            return {"login": OWNER, "type": account_type}
        if "/repos?" in path:
            page = int(path.rsplit("page=", 1)[1])
            return repos[(page - 1) * 100: page * 100]
        if path.endswith("/releases/latest"):
            return releases.get(path.split("/")[3])
        return None

    api.calls = calls
    return api


PROFILE_LIST = f"""# Profile

### Projects

Intro.

{sp.MARK_START}
- old hand-written entry
{sp.MARK_END}

### Next section
"""


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.profile = self.tmp / "README.md"
        self.profile.write_text(PROFILE_LIST, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_sync(self, *extra: str, api=None, fmt: str = "list") -> tuple[int, dict, str]:
        out = self.tmp / "out.txt"
        out.unlink(missing_ok=True)
        code = sp.main(["--profile", str(self.profile), "--owner", OWNER, "--format", fmt,
                        "--github-output", str(out), "--exclude", "excluded-one", *extra],
                       api=api or fake_api())
        outputs = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())
        return code, outputs, self.profile.read_text(encoding="utf-8")


class Selection(unittest.TestCase):
    def test_only_published_public_projects(self) -> None:
        projects = sp.collect_projects(OWNER, fake_api(), {"excluded-one"})
        self.assertEqual([p.repo for p in projects], ["ai-repo-rules-generator", "sbom-release-diff"])

    def test_private_repositories_never_listed(self) -> None:
        projects = sp.collect_projects(OWNER, fake_api(), set())
        names = {p.repo for p in projects}
        self.assertNotIn("secret-internal-tool", names)
        self.assertNotIn("sneaky", names)  # visibility wins even if "private" is false

    def test_private_repositories_are_never_queried_for_releases(self) -> None:
        api = fake_api()
        sp.collect_projects(OWNER, api, set())
        self.assertFalse([c for c in api.calls if "secret-internal-tool" in c or "sneaky" in c])

    def test_organisation_endpoint_and_pagination(self) -> None:
        many = [repo(f"tool-{i:03d}") for i in range(150)]
        api = fake_api(repos=many, releases={r["name"]: {"tag_name": "v1"} for r in many},
                       account_type="Organization")
        projects = sp.collect_projects(OWNER, api, set())
        self.assertEqual(len(projects), 150)
        self.assertTrue(any(c.startswith(f"/orgs/{OWNER}/repos?type=public") and c.endswith("page=2")
                            for c in api.calls))

    def test_display_names(self) -> None:
        cases = {"sbom-release-diff": "SBOM Release Diff", "wp-security-doctor": "WP Security Doctor",
                 "github-contribution-doctor": "GitHub Contribution Doctor",
                 "ai-repo-rules-generator": "AI Repo Rules Generator",
                 "mcp-capability-registry": "MCP Capability Registry", "Handlet": "Handlet",
                 "configuration-baseline": "Configuration Baseline"}
        for repo_name, expected in cases.items():
            with self.subTest(repo=repo_name):
                self.assertEqual(sp.display_name(repo_name), expected)


class Rendering(unittest.TestCase):
    def setUp(self) -> None:
        self.projects = sp.collect_projects(OWNER, fake_api(), {"excluded-one"})

    def test_list_format(self) -> None:
        text = sp.render(self.projects, OWNER, "list")
        self.assertIn("- **[SBOM Release Diff](https://github.com/example/sbom-release-diff)** — "
                      "Description of sbom-release-diff.<br>", text)
        self.assertIn("  Rust · Apache-2.0 · [![Latest release](https://img.shields.io/github/v/release/example/"
                      "sbom-release-diff?label=release)](https://github.com/example/sbom-release-diff/releases/latest)",
                      text)
        self.assertLess(text.index("AI Repo Rules Generator"), text.index("SBOM Release Diff"))

    def test_table_format_escapes_cells(self) -> None:
        text = sp.render(self.projects, OWNER, "table")
        self.assertIn("| Project | Description | Language | Release | Stars |", text)
        self.assertIn("| [AI Repo Rules Generator](https://github.com/example/ai-repo-rules-generator) | "
                      "Uses \\| pipes and \\[brackets\\] | Python | "
                      "![Release](https://img.shields.io/github/v/release/example/ai-repo-rules-generator) | "
                      "![Stars](https://img.shields.io/github/stars/example/ai-repo-rules-generator) |", text)
        rows = [line for line in text.splitlines() if line.startswith("| [")]
        self.assertTrue(all(line.count(" | ") == 4 for line in rows))


class Updating(Base):
    def test_replaces_block_and_reports(self) -> None:
        body = self.tmp / "body.md"
        code, outputs, text = self.run_sync("--write", "--pr-body", str(body))
        self.assertEqual(code, 0)
        self.assertEqual((outputs["changed"], outputs["projects"]), ("true", "2"))
        self.assertNotIn("old hand-written entry", text)
        self.assertIn("### Next section", text)
        self.assertIn("Intro.", text)
        pr = body.read_text(encoding="utf-8")
        self.assertIn("added: ai-repo-rules-generator, sbom-release-diff", pr)
        self.assertIn("Private repositories are never listed", pr)

    def test_second_run_changes_nothing(self) -> None:
        self.run_sync("--write")
        first = self.profile.read_text(encoding="utf-8")
        code, outputs, text = self.run_sync("--write")
        self.assertEqual((code, outputs["changed"], text), (0, "false", first))

    def test_removed_project_is_reported(self) -> None:
        self.run_sync("--write")
        body = self.tmp / "body.md"
        releases = dict(RELEASES)
        releases.pop("sbom-release-diff")
        code, outputs, text = self.run_sync("--write", "--pr-body", str(body), api=fake_api(releases=releases))
        self.assertEqual(outputs["projects"], "1")
        self.assertIn("removed: sbom-release-diff", body.read_text(encoding="utf-8"))

    def test_dry_run_does_not_write(self) -> None:
        code, outputs, text = self.run_sync()
        self.assertEqual((code, outputs["changed"], text), (0, "true", PROFILE_LIST))

    def test_missing_or_broken_markers(self) -> None:
        for content in ("# Profile without markers\n", f"{sp.MARK_END}\n{sp.MARK_START}\n",
                        f"{sp.MARK_START}\n{sp.MARK_START}\n{sp.MARK_END}\n"):
            with self.subTest(content=content[:30]):
                self.profile.write_text(content, encoding="utf-8")
                code, outputs, text = self.run_sync("--write")
                self.assertEqual((code, outputs["error"]), (2, "true"))
                self.assertEqual(text, content)

    def test_api_unavailable_writes_nothing(self) -> None:
        for fail_on in ("/repos?", "/releases/latest"):
            with self.subTest(fail_on=fail_on):
                code, outputs, text = self.run_sync("--write", api=fake_api(fail_on=fail_on))
                self.assertEqual((code, outputs["unavailable"]), (3, "true"))
                self.assertEqual(text, PROFILE_LIST)

    def test_unknown_account(self) -> None:
        code, outputs, _ = self.run_sync("--write", api=lambda path: None)
        self.assertEqual((code, outputs["error"]), (2, "true"))


if __name__ == "__main__":
    unittest.main()
