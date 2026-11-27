"""Static policy checks for the release-readiness workflows, examples and template.

These complement actionlint (run in CI) with organisation-specific rules and
need no third-party packages: they read the YAML files as text.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted(
    list((REPO / ".github" / "workflows").glob("release-readiness*.yml"))
    + list((REPO / "release-readiness" / "examples").glob("*.yml"))
    + list((REPO / "workflow-templates").glob("release-readiness*.yml"))
)
REUSABLE = REPO / ".github" / "workflows" / "release-readiness.yml"
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)\s*(#.*)?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OWN_REUSABLE = "JSP-IT-Solutions-Group/.github/.github/workflows/release-readiness.yml@"


def run_blocks(text: str) -> list[str]:
    """Return the bodies of `run: |` blocks and single-line `run:` values."""
    blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = re.match(r"^(\s*)(?:-\s*)?run:\s*(.*)$", lines[i])
        if not match:
            i += 1
            continue
        indent, rest = len(match.group(1)), match.group(2)
        if rest.strip() in ("|", ">", "|-", ">-"):
            body = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
                body.append(lines[i])
                i += 1
            blocks.append("\n".join(body))
        else:
            blocks.append(rest)
            i += 1
    return blocks


class WorkflowPolicy(unittest.TestCase):
    def test_files_found(self) -> None:
        names = {p.name for p in WORKFLOWS}
        self.assertIn("release-readiness.yml", names)
        self.assertIn("release-readiness-selftest.yml", names)
        self.assertGreaterEqual(len(WORKFLOWS), 5)

    def test_actions_pinned_to_full_sha_with_version_comment(self) -> None:
        for path in WORKFLOWS:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                match = USES_RE.match(line)
                if not match:
                    continue
                ref = match.group(1)
                with self.subTest(file=path.name, line=number):
                    if ref.startswith("./") or ref.startswith(OWN_REUSABLE):
                        continue  # local, or our own reusable workflow (documented tag/SHA)
                    self.assertIn("@", ref)
                    self.assertRegex(ref.split("@", 1)[1], SHA_RE, f"{ref} is not pinned to a SHA")
                    self.assertTrue(match.group(2) and re.search(r"v\d", match.group(2)),
                                    f"{ref} needs a '# vX.Y.Z' comment")

    def test_same_action_same_pin_everywhere(self) -> None:
        pins: dict[str, set[str]] = {}
        for path in WORKFLOWS:
            for line in path.read_text(encoding="utf-8").splitlines():
                match = USES_RE.match(line)
                if match and "@" in match.group(1) and not match.group(1).startswith((".", OWN_REUSABLE)):
                    action, ref = match.group(1).split("@", 1)
                    pins.setdefault(action, set()).add(ref)
        for action, refs in pins.items():
            with self.subTest(action=action):
                self.assertEqual(len(refs), 1, f"{action} is pinned to different SHAs: {refs}")

    def test_no_expressions_inside_run_scripts(self) -> None:
        """Inputs reach scripts through env vars only (prevents script injection)."""
        for path in WORKFLOWS:
            for block in run_blocks(path.read_text(encoding="utf-8")):
                with self.subTest(file=path.name, block=block[:60]):
                    self.assertNotIn("${{", block)

    def test_least_privilege(self) -> None:
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertRegex(text, r"(?m)^permissions:\s*\n\s+contents:\s*read\s*$",
                                 "top-level permissions must be contents: read")
                for perm in re.findall(r"(?m)^\s+([a-z-]+):\s*write\s*$", text):
                    self.fail(f"write permission granted: {perm}")
                self.assertNotIn("secrets: inherit", text)
                self.assertNotRegex(text, r"\$\{\{\s*secrets\.")
                self.assertNotIn("pull_request_target", text)
                self.assertNotRegex(text, r"(?m)^\s*-?\s*run:.*\bgh release\b")

    def test_no_publishing_actions(self) -> None:
        forbidden = re.compile(r"(softprops/action-gh-release|actions/create-release|"
                               r"ncipollo/release-action|pypa/gh-action-pypi-publish|"
                               r"\bnpm publish\b|\bdotnet nuget push\b|\bcargo publish\b|\btwine upload\b)")
        for path in WORKFLOWS:
            with self.subTest(file=path.name):
                self.assertIsNone(forbidden.search(path.read_text(encoding="utf-8")))

    def test_checkouts_do_not_persist_credentials(self) -> None:
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            checkouts = len(re.findall(r"uses:\s*actions/checkout@", text))
            with self.subTest(file=path.name):
                self.assertEqual(len(re.findall(r"persist-credentials:\s*false", text)), checkouts)

    def test_reusable_workflow_contract(self) -> None:
        text = REUSABLE.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^on:\s*\n\s+workflow_call:")
        self.assertNotRegex(text, r"(?m)^\s+(push|pull_request|schedule|workflow_dispatch):")
        inputs_block = text.split("    inputs:\n", 1)[1].split("    outputs:\n", 1)[0]
        inputs = re.findall(r"(?m)^      ([a-z-]+):\n        description:", inputs_block)
        expected = {"working-directory", "config-path", "profile", "project-type", "version",
                    "tag-prefix", "version-scheme", "required-documents", "recommended-documents",
                    "changelog-path", "checksums", "checksum-manifest", "artifacts-dir",
                    "artifact-name", "fail-on-warnings", "enforce", "tooling-ref", "runs-on"}
        self.assertEqual(set(inputs), expected)
        docs = (REPO / "docs" / "release-readiness.md").read_text(encoding="utf-8")
        for name in expected:
            with self.subTest(input=name):
                self.assertIn(f"`{name}`", docs, f"input {name} is not documented")

    def test_default_tooling_ref_matches_major_version(self) -> None:
        version = (REPO / "release-readiness" / "VERSION").read_text(encoding="utf-8").strip()
        major = version.split(".")[0]
        text = REUSABLE.read_text(encoding="utf-8")
        self.assertRegex(text, rf"default: release-readiness-v{major}\n")


if __name__ == "__main__":
    unittest.main()
