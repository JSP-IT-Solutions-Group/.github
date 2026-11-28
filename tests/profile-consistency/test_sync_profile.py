"""Tests for profile-consistency/sync_profile.py (synthetic fixtures only).

    python -m unittest discover -s tests/profile-consistency -v
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixtures"
TOOLS = REPO / "profile-consistency"
SITE = "https://jsp-it.com/products_jsp_overview"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pc = sys.modules.get("check_profile_consistency") or _load("check_profile_consistency")
sync = _load("sync_profile")
CATALOGUE = (FIX / "catalogue-en-sync.html").read_text(encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.profile = self.tmp / "README.md"
        shutil.copy(FIX / "profile-sync.md", self.profile)
        self.catalogue = self.tmp / "catalogue.html"
        self.catalogue.write_text(CATALOGUE, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_sync(self, *extra: str, catalogue: Path | None = None) -> tuple[int, dict[str, str], str]:
        out = self.tmp / "out.txt"
        out.unlink(missing_ok=True)
        code = sync.main(["--profile", str(self.profile), "--catalogue", str(catalogue or self.catalogue),
                          "--site-url", SITE, "--github-output", str(out), *extra])
        outputs = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())
        return code, outputs, self.profile.read_text(encoding="utf-8")


class Parsing(unittest.TestCase):
    def test_groups_products_and_order(self) -> None:
        groups = sync.parse_catalogue(CATALOGUE)
        self.assertEqual([g.key for g in groups], ["automation", "data", "empty"])
        self.assertEqual([p.type for p in groups[0].products], ["products_alpha", "products_beta"])
        self.assertEqual(groups[0].products[0].name, "JSP-Alpha™")
        self.assertEqual(groups[0].products[0].summary, "First synthetic product | with a pipe")
        self.assertEqual(groups[2].products, [])

    def test_missing_structured_data_is_unavailable(self) -> None:
        broken = re.sub(r"<script id=jsp-solution-advisor-catalog.*?</script>", "", CATALOGUE, flags=re.S)
        with self.assertRaisesRegex(sync.Unavailable, "not found"):
            sync.parse_catalogue(broken)

    def test_cards_and_structured_data_must_agree(self) -> None:
        broken = CATALOGUE.replace('"products_gamma","name"', '"products_delta","name"')
        with self.assertRaisesRegex(sync.Unavailable, "disagree"):
            sync.parse_catalogue(broken)

    def test_undefined_group_is_unavailable(self) -> None:
        broken = CATALOGUE.replace("data-product-category=data>", "data-product-category=finance>")
        with self.assertRaisesRegex(sync.Unavailable, "undefined group"):
            sync.parse_catalogue(broken)


class Rendering(unittest.TestCase):
    def test_section_format(self) -> None:
        text = sync.render_section(sync.parse_catalogue(CATALOGUE), SITE)
        self.assertTrue(text.startswith(sync.MARK_START) and text.endswith(sync.MARK_END))
        self.assertIn("<summary><b>Automation and AI</b> · 2 solutions</summary>", text)
        self.assertIn("<summary><b>Data and reporting</b> · 1 solution</summary>", text)
        self.assertNotIn("Empty group", text)  # groups without products are omitted
        self.assertIn("- [JSP-Alpha™](https://jsp-it.com/products_show?type=products_alpha): "
                      "First synthetic product \\| with a pipe", text)
        self.assertIn("[JSP-Beta \\[Preview\\]™](https://jsp-it.com/products_show?type=products_beta)", text)
        self.assertIn("(https://jsp-it.com/products_jsp_overview?category=data)", text)
        self.assertEqual(text.count("<details>"), text.count("</details>"))


class Updating(Base):
    def test_inserts_section_and_updates_counts(self) -> None:
        code, outputs, text = self.run_sync("--write")
        self.assertEqual(code, 0)
        self.assertEqual(outputs["changed"], "true")
        self.assertEqual((outputs["solutions"], outputs["groups"]), ("3", "3"))  # empty group still counts
        self.assertIn("**3 software solutions** across **3 solution groups**", text)
        explore = text.index("Explore the complete")
        heading = text.index(sync.SECTION_HEADING)
        self.assertLess(explore, heading)
        self.assertLess(heading, text.index("## Other section"))
        self.assertIn("· 2 solutions</summary>", text)  # group headings are not rewritten as totals
        self.assertIn("This text stays as it is.", text)

    def test_second_run_changes_nothing(self) -> None:
        self.run_sync("--write")
        first = self.profile.read_text(encoding="utf-8")
        code, outputs, text = self.run_sync("--write")
        self.assertEqual((code, outputs["changed"]), (0, "false"))
        self.assertEqual(text, first)

    def test_dry_run_does_not_write(self) -> None:
        before = self.profile.read_text(encoding="utf-8")
        code, outputs, text = self.run_sync()
        self.assertEqual((code, outputs["changed"]), (0, "true"))
        self.assertEqual(text, before)

    def test_reports_added_and_removed_products(self) -> None:
        self.run_sync("--write")
        smaller = CATALOGUE.replace(
            '<article class="col-md-4 jsp-entity" data-product-category=data><h4>JSP-Gamma&trade;</h4>'
            '<p class=product-subtitle>Third synthetic product<p>Long description.</p>'
            '<a href="products_show?type=products_gamma">Read more</a></article>', "")
        smaller = smaller.replace(',{"id":"products_gamma","name":"JSP-Gamma","summary":"Third synthetic product"}', "")
        self.catalogue.write_text(smaller, encoding="utf-8")
        body = self.tmp / "body.md"
        code, outputs, text = self.run_sync("--write", "--pr-body", str(body))
        self.assertEqual(code, 0)
        self.assertNotIn("products_gamma", text)
        self.assertIn("**2 software solutions** across **3 solution groups**", text)
        pr = body.read_text(encoding="utf-8")
        self.assertIn("removed: products_gamma", pr)
        self.assertIn("solutions: 3 → 2", pr)
        self.assertIn("never merged automatically", pr)
        self.assertIn("catalogue.html", pr)  # names the source that was actually used

    def test_no_pr_body_when_unchanged(self) -> None:
        self.run_sync("--write")
        body = self.tmp / "body.md"
        self.run_sync("--write", "--pr-body", str(body))
        self.assertFalse(body.exists())

    def test_broken_markers_are_an_error(self) -> None:
        self.profile.write_text(self.profile.read_text(encoding="utf-8") + "\n" + sync.MARK_START + "\n",
                                encoding="utf-8")
        code, outputs, _ = self.run_sync("--write")
        self.assertEqual(code, 2)
        self.assertEqual(outputs["error"], "true")

    def test_no_markers_and_no_anchor_is_an_error(self) -> None:
        self.profile.write_text("# Profile\n\n**96 software solutions**\n", encoding="utf-8")
        code, outputs, text = self.run_sync("--write")
        self.assertEqual(code, 2)
        self.assertIn("96 software solutions", text)

    def test_unavailable_catalogue_writes_nothing(self) -> None:
        before = self.profile.read_text(encoding="utf-8")
        for catalogue in (self.tmp / "missing.html", FIX / "catalogue-en-no-data.html"):
            with self.subTest(catalogue=catalogue.name):
                code, outputs, text = self.run_sync("--write", catalogue=catalogue)
                self.assertEqual(code, 3)
                self.assertEqual(outputs["unavailable"], "true")
                self.assertEqual(text, before)

    def test_missing_profile(self) -> None:
        self.profile.unlink()
        out = self.tmp / "out.txt"
        code = sync.main(["--profile", str(self.profile), "--catalogue", str(self.catalogue),
                          "--github-output", str(out)])
        self.assertEqual(code, 2)


class CheckerUnderstandsGeneratedList(Base):
    def check(self, catalogue: Path) -> dict:
        out = self.tmp / "report.json"
        pc.main(["--catalogue", f"en={catalogue}", "--counts-profile", str(self.profile), "--no-links",
                 "--quiet", "--json-out", str(out)])
        return json.loads(out.read_text(encoding="utf-8"))

    def test_synced_profile_passes(self) -> None:
        self.run_sync("--write")
        report = self.check(self.catalogue)
        self.assertEqual(report["result"], "pass", report["findings"])
        self.assertTrue(any(f["category"] == "Profile list" and f["status"] == "pass" for f in report["findings"]))

    def test_outdated_list_fails(self) -> None:
        self.run_sync("--write")
        text = self.profile.read_text(encoding="utf-8")
        self.profile.write_text(re.sub(r"- \[JSP-Gamma.*\n", "", text), encoding="utf-8")
        report = self.check(self.catalogue)
        hits = [f for f in report["findings"] if f["category"] == "Profile list" and f["status"] == "fail"]
        self.assertTrue(hits)
        self.assertIn("products_gamma", " ".join(hits[0]["details"]))

    def test_group_headings_are_not_count_statements(self) -> None:
        self.run_sync("--write")
        body, block = pc.split_generated_list(self.profile.read_text(encoding="utf-8"))
        self.assertIsNotNone(block)
        self.assertNotIn("products_show", body)
        self.assertEqual([n for n, _ in pc.find_counts(body, pc.PROFILE_SOLUTION_PATTERNS)], [3])


if __name__ == "__main__":
    unittest.main()
