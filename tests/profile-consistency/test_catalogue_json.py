"""Tests for reading /catalogue.json (schema 1) with HTML fallback (synthetic fixtures only).

    python -m unittest discover -s tests/profile-consistency -v
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixtures"
TOOLS = REPO / "profile-consistency"


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pc = _load("check_profile_consistency")
sync = _load("sync_profile")
EN = json.loads((FIX / "catalogue-en.json").read_text(encoding="utf-8"))


def fetched(body: str, url: str = "https://jsp-it.com/catalogue.json", status: int = 200,
            error: str | None = None, headers: dict | None = None):
    return pc.Fetched(url, status, body, headers or {}, error)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_json(self, name: str, data) -> Path:
        path = self.tmp / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def check(self, *argv: str) -> tuple[int, dict]:
        out = self.tmp / "report.json"
        code = pc.main([*argv, "--no-links", "--quiet", "--retries", "0", "--json-out", str(out)])
        return code, json.loads(out.read_text(encoding="utf-8"))


class Schema(unittest.TestCase):
    def test_valid_document_and_unknown_fields(self) -> None:
        data = pc.parse_catalogue_json(json.dumps(EN))
        self.assertEqual(len(data["products"]), 3)
        self.assertEqual(pc.json_problems(data, "en"), [])

    def test_unusable_documents(self) -> None:
        cases = {
            "<html><title>One moment, please...</title></html>": "not valid JSON",
            "[]": "not an object",
            json.dumps({**EN, "version": 2}): "unsupported schema version 2",
            json.dumps({**EN, "products": {}}): "must be lists",
            json.dumps({**EN, "products": [{"name": "no id"}]}): "non-empty 'id'",
            json.dumps({**EN, "categories": [{"label": "no key"}]}): "non-empty 'key'",
        }
        for body, message in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    pc.parse_catalogue_json(body)

    def test_consistency_problems(self) -> None:
        bad = json.loads(json.dumps(EN))
        bad["count"] = 4
        bad["products"][2]["category"] = "finance"
        bad["products"][1]["id"] = "products_alpha"
        problems = " ".join(pc.json_problems(bad, "fi"))
        for text in ("count 4 != 3", "undefined groups: finance", "duplicate product ids", "language is 'en'"):
            self.assertIn(text, problems)


class Checker(Base):
    def test_json_sources_pass_and_match_html(self) -> None:
        code, report = self.check("--catalogue", f"en={FIX / 'catalogue-en.json'}",
                                  "--catalogue", f"fi={FIX / 'catalogue-fi.json'}",
                                  "--counts-profile", str(FIX / "profile-current.md"))
        self.assertEqual(code, 0, report["findings"])
        self.assertEqual(report["catalogues"]["en"]["signals"], {"json products": 3, "json groups": 2})
        self.assertTrue(any(f["category"] == "Language parity" and f["status"] == "pass" for f in report["findings"]))
        html = pc.extract_catalogue("en", fetched((FIX / "catalogue-en.html").read_text(encoding="utf-8"),
                                                   url="https://jsp-it.com/products_jsp_overview"))
        json_cat = pc.extract_any_catalogue("en", fetched((FIX / "catalogue-en.json").read_text(encoding="utf-8")))
        self.assertEqual((json_cat.solutions, json_cat.groups), (html.solutions, html.groups))

    def test_inconsistent_json_warns(self) -> None:
        bad = json.loads(json.dumps(EN))
        bad["count"] = 99
        path = self.write_json("bad.json", bad)
        code, report = self.check("--catalogue", f"en={path}")
        self.assertEqual(code, 0)
        hits = [f for f in report["findings"] if f["category"] == "Catalogue" and f["status"] == "warn"]
        self.assertIn("count 99 != 3", hits[0]["message"])

    def test_falls_back_to_html_when_json_unusable(self) -> None:
        blocked = self.write_json("blocked.json", "<html><title>One moment, please...</title></html>")
        code, report = self.check("--catalogue", f"en={blocked}|{FIX / 'catalogue-en.html'}")
        self.assertEqual(code, 0)
        cat = report["catalogues"]["en"]
        self.assertEqual(cat["solutions"], 3)
        self.assertIn("catalogue-en.html", cat["source"])
        self.assertIn("One moment, please", cat["signals"]["source fallback"])
        warn = [f for f in report["findings"] if f["category"] == "Catalogue"][0]
        self.assertEqual(warn["status"], "warn")
        self.assertIn("fallback source", warn["message"])

    def test_all_sources_unavailable(self) -> None:
        blocked = self.write_json("blocked.json", "<html><title>One moment, please...</title></html>")
        code, report = self.check("--catalogue", f"en={blocked}|{self.tmp / 'missing.html'}",
                                  "--counts-profile", str(FIX / "profile-stale.md"))
        self.assertEqual(code, 3)
        error = report["catalogues"]["en"]["error"]
        self.assertIn("blocked.json", error)
        self.assertIn("missing.html", error)
        self.assertFalse([f for f in report["findings"] if f["status"] == "fail"])

    def test_wrong_schema_version_falls_back(self) -> None:
        v2 = self.write_json("v2.json", {**EN, "version": 2})
        code, report = self.check("--catalogue", f"en={v2}|{FIX / 'catalogue-en.html'}")
        self.assertIn("unsupported schema version 2", report["catalogues"]["en"]["signals"]["source fallback"])

    def test_defaults_point_to_production_json_first(self) -> None:
        for lang, host in (("en", "jsp-it.com"), ("fi", "jsp-it.fi")):
            sources = pc.DEFAULT_CATALOGUES[lang].split("|")
            self.assertEqual(sources[0], f"https://{host}/catalogue.json")
            self.assertEqual(sources[1], f"https://{host}/products_jsp_overview")


class Sync(Base):
    def setUp(self) -> None:
        super().setUp()
        self.profile = self.tmp / "README.md"
        shutil.copy(FIX / "profile-sync.md", self.profile)

    def run_sync(self, catalogue: str, *extra: str) -> tuple[int, str, str]:
        summary = self.tmp / "summary.md"
        summary.unlink(missing_ok=True)
        code = sync.main(["--profile", str(self.profile), "--catalogue", catalogue, "--write",
                          "--retries", "0", "--summary-file", str(summary), *extra])
        return code, self.profile.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8")

    def test_json_and_html_produce_the_same_profile(self) -> None:
        code, from_json, _ = self.run_sync(str(FIX / "catalogue-en.json"))
        self.assertEqual(code, 0)
        shutil.copy(FIX / "profile-sync.md", self.profile)
        html = (FIX / "catalogue-en-sync.html").read_text(encoding="utf-8")
        html = html.replace(',"empty":{"label":"Empty group","intro":"no products"}', "")
        html_path = self.tmp / "catalogue.html"
        html_path.write_text(html, encoding="utf-8")
        code, from_html, _ = self.run_sync(str(html_path))
        self.assertEqual(code, 0)
        self.assertEqual(from_json, from_html)
        self.assertIn("(https://jsp-it.com/products_show?type=products_alpha): First synthetic product \\| with a pipe",
                      from_json)
        self.assertIn("(https://jsp-it.com/products_jsp_overview?category=data)", from_json)

    def test_json_fallback_and_reporting(self) -> None:
        blocked = self.write_json("blocked.json", "<html><title>One moment, please...</title></html>")
        code, text, summary = self.run_sync(f"{blocked}|{FIX / 'catalogue-en.json'}")
        self.assertEqual(code, 0)
        self.assertIn("products_gamma", text)
        self.assertIn("Fallback source used because", summary)
        self.assertIn("One moment, please", summary)

    def test_inconsistent_json_is_not_written(self) -> None:
        bad = json.loads(json.dumps(EN))
        bad["products"][0]["category"] = "finance"
        path = self.write_json("bad.json", bad)
        before = self.profile.read_text(encoding="utf-8")
        code, text, summary = self.run_sync(str(path))
        self.assertEqual(code, 3)
        self.assertEqual(text, before)
        self.assertIn("inconsistent", summary)

    def test_non_https_product_urls_are_rebuilt(self) -> None:
        data = json.loads(json.dumps(EN))
        data["products"][0]["url"] = "javascript:alert(1)"
        code, text, _ = self.run_sync(str(self.write_json("odd.json", data)))
        self.assertEqual(code, 0)
        self.assertNotIn("javascript:", text)
        self.assertIn("(https://jsp-it.com/products_show?type=products_alpha)", text)

    def test_default_source_is_production_json_first(self) -> None:
        self.assertTrue(sync.DEFAULT_CATALOGUE.startswith("https://jsp-it.com/catalogue.json|"))
        self.assertEqual(sync.DEFAULT_SITE, "https://jsp-it.com/products_jsp_overview")


if __name__ == "__main__":
    unittest.main()
