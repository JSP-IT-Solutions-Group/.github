"""Tests for profile-consistency/check_profile_consistency.py (synthetic fixtures only).

    python -m unittest discover -s tests/profile-consistency -v
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import re
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixtures"
SCRIPT = REPO / "profile-consistency" / "check_profile_consistency.py"

_spec = importlib.util.spec_from_file_location("check_profile_consistency", SCRIPT)
pc = importlib.util.module_from_spec(_spec)
sys.modules["check_profile_consistency"] = pc
_spec.loader.exec_module(pc)


def fake_fetch(responses: dict[str, tuple[int | None, str]]):
    """Return a fetch() replacement: URL -> (status, body); unknown URLs time out."""
    calls: list[tuple[str, str]] = []

    def fetch(url, *, timeout, retries, method="GET", read_body=True):
        calls.append((method, url))
        status, body = responses.get(url, (None, ""))
        if status is None:
            return pc.Fetched(url, None, error="TimeoutError: timed out")
        if status >= 400:
            return pc.Fetched(url, status, error=f"HTTP {status}")
        return pc.Fetched(url, status, body if read_body else "", {})

    fetch.calls = calls
    return fetch


OK_LINKS = {"https://example.test/": (200, ""), "https://example.test/catalogue": (200, "")}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_check(self, *argv: str, responses: dict | None = None) -> tuple[int, dict, str, str]:
        out, summary, outputs = self.tmp / "r.json", self.tmp / "s.md", self.tmp / "o.txt"
        for p in (out, summary, outputs):
            p.unlink(missing_ok=True)
        code = pc.main([*argv, "--quiet", "--retries", "0", "--json-out", str(out),
                        "--summary-file", str(summary), "--github-output", str(outputs)],
                       fetch=fake_fetch({**OK_LINKS, **(responses or {})}))
        return (code, json.loads(out.read_text(encoding="utf-8")),
                summary.read_text(encoding="utf-8"), outputs.read_text(encoding="utf-8"))

    @staticmethod
    def cats(en: str = "catalogue-en.html", fi: str = "catalogue-fi.html") -> list[str]:
        return ["--catalogue", f"en={FIX / en}", "--catalogue", f"fi={FIX / fi}"]

    def assertFinding(self, report: dict, category: str, status: str, pattern: str) -> None:
        hits = [f for f in report["findings"] if f["category"] == category and f["status"] == status
                and re.search(pattern, f["check"] + " " + f["message"] + " " + " ".join(f["details"]))]
        self.assertTrue(hits, f"no {status} {category} finding matching {pattern!r} in:\n" + "\n".join(
            f"{f['status']} {f['category']} {f['check']}: {f['message']} {f['details']}" for f in report["findings"]))


class CountExtraction(Base):
    def test_structured_counts_and_parity(self) -> None:
        code, report, summary, outputs = self.run_check(*self.cats(), "--counts-profile",
                                                        str(FIX / "profile-current.md"))
        self.assertEqual(code, 0, summary)
        self.assertEqual(report["result"], "pass")
        self.assertEqual(report["catalogues"]["en"]["solutions"], 3)
        self.assertEqual(report["catalogues"]["fi"]["groups"], 2)
        self.assertFinding(report, "Language parity", "pass", r"product ids match")
        self.assertFinding(report, "Profile counts", "pass", r"States 3 solutions")
        self.assertIn("solutions=3", outputs)
        self.assertIn("unavailable=false", outputs)

    def test_script_text_is_not_counted(self) -> None:
        cat = pc.extract_catalogue("en", pc.load_source(str(FIX / "catalogue-en.html"), None))
        self.assertEqual(cat.signals["visible counter"], 3)  # "999 solutions" lives in a <script>

    def test_finnish_profile_wording(self) -> None:
        code, report, _, _ = self.run_check(*self.cats(), "--counts-profile", str(FIX / "profile-finnish.md"),
                                            "--reference-language", "fi", "--no-links")
        self.assertEqual(code, 0)
        self.assertFinding(report, "Profile counts", "pass", r"States 2 groups")

    def test_count_patterns(self) -> None:
        cases = {
            "includes **102 software solutions** across **15 solution groups**": ([102], [15]),
            "102 JSP-Product™ solutions in 15 product groups": ([102], [15]),
            "Valikoimassa on 102 ratkaisua 15 ratkaisuryhmässä": ([102], [15]),
            "15 solution groups only": ([], [15]),
            "No numbers here, just solutions.": ([], []),
        }
        for text, (solutions, groups) in cases.items():
            with self.subTest(text=text):
                self.assertEqual([n for n, _ in pc.find_counts(text, pc.PROFILE_SOLUTION_PATTERNS)], solutions)
                self.assertEqual([n for n, _ in pc.find_counts(text, pc.PROFILE_GROUP_PATTERNS)], groups)

    def test_counts_in_code_blocks_are_ignored(self) -> None:
        text = (FIX / "profile-nocounts.md").read_text(encoding="utf-8")
        self.assertEqual(pc.find_counts(text, pc.PROFILE_SOLUTION_PATTERNS), [])

    def test_cards_fallback_warns(self) -> None:
        code, report, _, _ = self.run_check(*self.cats(en="catalogue-en-cards-only.html"), "--no-links")
        self.assertEqual(code, 0)
        self.assertFinding(report, "Catalogue", "warn", r"fell back to product cards")
        self.assertEqual(report["catalogues"]["en"]["solutions"], 3)

    def test_internal_disagreement_warns(self) -> None:
        _, report, _, _ = self.run_check(*self.cats(en="catalogue-en-counter-mismatch.html"), "--no-links")
        self.assertFinding(report, "Catalogue", "warn", r"visible counter 4 != 3")


class Discrepancies(Base):
    def test_stale_profile_fails(self) -> None:
        code, report, summary, outputs = self.run_check(*self.cats(), "--counts-profile",
                                                        str(FIX / "profile-stale.md"), "--no-links")
        self.assertEqual(code, 1)
        self.assertFinding(report, "Profile counts", "fail", r"Profile states 96 solutions; the EN catalogue has 3")
        self.assertFinding(report, "Profile counts", "fail", r"15 groups.*has 2")
        self.assertFinding(report, "Profile counts", "fail", r"never edits")
        self.assertIn("result=fail", outputs)
        self.assertIn("❌ FAIL", summary)

    def test_profile_without_counts(self) -> None:
        _, report, _, _ = self.run_check(*self.cats(), "--profile", str(FIX / "profile-nocounts.md"), "--no-links")
        self.assertFinding(report, "Profile counts", "skip", r"No catalogue count statement")
        code, report, _, _ = self.run_check(*self.cats(), "--counts-profile", str(FIX / "profile-nocounts.md"),
                                            "--no-links")
        self.assertEqual(code, 1)
        self.assertFinding(report, "Profile counts", "fail", r"counts are required")

    def test_language_parity_failure(self) -> None:
        code, report, _, _ = self.run_check(*self.cats(fi="catalogue-fi-missing-product.html"), "--no-links")
        self.assertEqual(code, 1)
        self.assertFinding(report, "Language parity", "fail", r"solutions 3 vs 2")
        self.assertFinding(report, "Language parity", "fail", r"only in EN: `products_gamma`")

    def test_group_key_parity(self) -> None:
        fi = self.tmp / "fi.html"
        fi.write_text((FIX / "catalogue-fi.html").read_text(encoding="utf-8").replace('"data":', '"finance":'),
                      encoding="utf-8")
        code, report, _, _ = self.run_check("--catalogue", f"en={FIX / 'catalogue-en.html'}",
                                            "--catalogue", f"fi={fi}", "--no-links")
        self.assertEqual(code, 1)
        self.assertFinding(report, "Language parity", "fail", r"group keys differ: data, finance")


class Unavailable(Base):
    def test_network_failure_is_not_a_discrepancy(self) -> None:
        code, report, summary, outputs = self.run_check(
            "--catalogue", "en=https://catalogue.test/en", "--catalogue", "fi=https://catalogue.test/fi",
            "--counts-profile", str(FIX / "profile-stale.md"), "--no-links")
        self.assertEqual(code, 3)
        self.assertTrue(report["unavailable"])
        self.assertFinding(report, "Catalogue", "warn", r"Data unavailable.*TimeoutError")
        self.assertFinding(report, "Profile counts", "warn", r"Cannot compare")
        self.assertFinding(report, "Language parity", "skip", r"at least two")
        self.assertEqual([f for f in report["findings"] if f["status"] == "fail"], [])
        self.assertIn("unavailable=true", outputs)
        self.assertIn("Catalogue data was unavailable", summary)

    def test_http_error_page(self) -> None:
        code, report, _, _ = self.run_check("--catalogue", "en=https://catalogue.test/en", "--no-links",
                                            responses={"https://catalogue.test/en": (503, "")})
        self.assertEqual(code, 3)
        self.assertFinding(report, "Catalogue", "warn", r"HTTP 503")

    def test_missing_structured_data_and_no_cards_is_unavailable(self) -> None:
        code, report, _, _ = self.run_check("--catalogue", f"en={FIX / 'catalogue-en-no-data.html'}", "--no-links")
        self.assertEqual(code, 3, report)
        self.assertFinding(report, "Catalogue", "warn", r"#jsp-solution-advisor-catalog not found")
        self.assertFinding(report, "Catalogue", "warn", r"HTTP 200 with \d+ characters, title “Synthetic catalogue")

    def test_invalid_structured_json_falls_back_with_warning(self) -> None:
        code, report, _, _ = self.run_check("--catalogue", f"en={FIX / 'catalogue-en-invalid-json.html'}",
                                            "--no-links")
        self.assertEqual(code, 0, report)
        self.assertFinding(report, "Catalogue", "warn", r"missing or invalid.*not valid JSON")

    def test_one_language_unavailable_still_compares_the_other(self) -> None:
        code, report, _, _ = self.run_check("--catalogue", f"en={FIX / 'catalogue-en.html'}",
                                            "--catalogue", "fi=https://catalogue.test/fi",
                                            "--counts-profile", str(FIX / "profile-stale.md"), "--no-links")
        self.assertEqual(code, 1)  # the discrepancy against EN is still real
        self.assertTrue(report["unavailable"])

    def test_remote_profile_unavailable(self) -> None:
        code, report, _, _ = self.run_check(*self.cats(), "--counts-profile", "https://profile.test/README.md")
        self.assertEqual(code, 3)
        self.assertFinding(report, "Profile counts", "warn", r"could not be loaded")
        code, report, _, _ = self.run_check(*self.cats(), "--counts-profile", "https://profile.test/README.md",
                                            responses={"https://profile.test/README.md": (404, "")})
        self.assertEqual(code, 1)
        self.assertFinding(report, "Profile counts", "fail", r"HTTP 404")

    def test_cache_indicators_are_reported(self) -> None:
        fetched = pc.Fetched("https://catalogue.test/en", 200,
                             (FIX / "catalogue-en.html").read_text(encoding="utf-8"), {"age": "3600"})
        self.assertIn("Age=3600", pc.extract_catalogue("en", fetched).cache_note)


class Links(Base):
    def test_link_outcomes(self) -> None:
        responses = {
            "https://example.test/ok": (200, ""), "https://example.test/gone": (404, ""),
            "https://example.test/limited": (429, ""), "https://example.test/badge.svg": (200, ""),
            "https://example.test/html-link": (200, ""), "https://example.test/bare": (200, ""),
            "https://example.test/reference": (200, ""),
        }
        code, report, _, _ = self.run_check(*self.cats(), "--profile", str(FIX / "profile-links.md"),
                                            responses=responses)
        self.assertEqual(code, 1)
        finding = [f for f in report["findings"] if f["category"] == "Profile links"][0]
        details = " ".join(finding["details"])
        self.assertIn("https://example.test/gone`: HTTP 404", details)
        self.assertIn("does-not-exist.md`: file not found", details)
        self.assertIn("limited`: HTTP 429", details)  # reported as unverified, not broken
        self.assertIn("2 broken link(s), 5 OK", finding["message"])
        self.assertNotIn("mailto", details)

    def test_unverifiable_only_warns(self) -> None:
        code, report, _, _ = self.run_check(*self.cats(), "--profile", str(FIX / "profile-nocounts.md"),
                                            responses={"https://example.test/catalogue": (503, "")})
        self.assertEqual(code, 0)
        self.assertFinding(report, "Profile links", "warn", r"could not be verified \(not necessarily broken\)")

    def test_relative_links_resolved_for_local_profiles(self) -> None:
        _, report, _, _ = self.run_check(*self.cats(), "--profile", str(FIX / "profile-current.md"))
        self.assertFinding(report, "Profile links", "pass", r"All 3 link")

    def test_link_extraction(self) -> None:
        links = pc.extract_links((FIX / "profile-links.md").read_text(encoding="utf-8"))
        self.assertIn("https://example.test/bare", links)  # trailing '.' stripped
        self.assertIn("https://example.test/reference", links)
        self.assertEqual(len(links), len(set(links)))


class RealHttp(unittest.TestCase):
    """Exercise http_get against a local server (no external network)."""

    @classmethod
    def setUpClass(cls) -> None:
        state = {"flaky": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                pass

            def do_HEAD(self):
                if self.path == "/no-head":
                    self.send_response(405)
                    self.end_headers()
                    return
                self.do_GET(head=True)

            def do_GET(self, head=False):
                if self.path == "/flaky" and state["flaky"] == 0:
                    state["flaky"] += 1
                    self.send_response(503)
                    self.end_headers()
                    return
                code = {"/missing": 404}.get(self.path, 200)
                body = (FIX / "catalogue-en.html").read_bytes()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head and code == 200:
                    self.wfile.write(body)

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_get_and_extract(self) -> None:
        fetched = pc.http_get(self.base + "/catalogue", timeout=5, retries=0)
        self.assertEqual(fetched.status, 200)
        self.assertEqual(pc.extract_catalogue("en", fetched).solutions, 3)

    def test_404_is_not_retried(self) -> None:
        self.assertEqual(pc.http_get(self.base + "/missing", timeout=5, retries=2).status, 404)

    def test_transient_error_is_retried(self) -> None:
        self.assertEqual(pc.http_get(self.base + "/flaky", timeout=5, retries=1).status, 200)

    def test_head_rejected_falls_back_to_get(self) -> None:
        args = pc.parse_args(["--timeout", "5", "--retries", "0"])
        self.assertEqual(pc._status(pc.http_get, self.base + "/no-head", args).status, 200)

    def test_connection_refused(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        fetched = pc.http_get(f"http://127.0.0.1:{port}/", timeout=2, retries=0)
        self.assertIsNone(fetched.status)
        self.assertTrue(fetched.error)


class Output(Base):
    def test_annotations_escape(self) -> None:
        report = pc.Report()
        report.add("Profile counts", "a,b:c", "fail", "50% off\nnext")
        line = pc.annotations(report)[0]
        self.assertTrue(line.startswith("::error title=Profile consistency%3A a%2Cb%3Ac::"))
        self.assertIn("50%25 off%0Anext", line)

    def test_summary_escapes_html(self) -> None:
        report = pc.Report()
        report.add("Profile links", "<b>x</b>", "warn", "<img src=x>")
        text = pc.render_markdown(report, "now")
        self.assertNotIn("<img", text)
        self.assertIn("&lt;img", text)

    def test_bad_catalogue_argument(self) -> None:
        with self.assertRaises(SystemExit):
            pc.main(["--catalogue", "no-equals-sign", "--quiet"], fetch=fake_fetch({}))


class Metadata(unittest.TestCase):
    def test_version_files_agree(self) -> None:
        version = (REPO / "profile-consistency" / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, pc.__version__)
        changelog = (REPO / "profile-consistency" / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}]", changelog)
        workflow = (REPO / ".github" / "workflows" / "profile-consistency.yml").read_text(encoding="utf-8")
        self.assertIn(f"default: profile-consistency-v{version.split('.')[0]}\n", workflow)

    def test_all_inputs_documented(self) -> None:
        workflow = (REPO / ".github" / "workflows" / "profile-consistency.yml").read_text(encoding="utf-8")
        block = workflow.split("    inputs:\n", 1)[1].split("    outputs:\n", 1)[0]
        inputs = re.findall(r"(?m)^      ([a-z-]+):\n", block)
        docs = (REPO / "docs" / "profile-consistency.md").read_text(encoding="utf-8")
        self.assertEqual(len(inputs), 11)
        for name in inputs:
            self.assertIn(f"`{name}`", docs)


class Retries(unittest.TestCase):
    def test_both_workflows_retry_only_unavailable_data(self) -> None:
        docs = (REPO / "docs" / "profile-consistency.md").read_text(encoding="utf-8")
        for name in ("profile-consistency.yml", "profile-consistency-sync.yml"):
            text = (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
            with self.subTest(workflow=name):
                self.assertRegex(text, r"(?m)^      attempts:\n(?:.*\n){1,2}\s+type: number\n\s+default: 4$")
                self.assertRegex(text, r"(?m)^      retry-delay-seconds:\n(?:.*\n){1,2}\s+type: number\n\s+default: 90$")
                # The loop only continues on exit code 3 (data unavailable).
                self.assertIn('if [ "$status" -ne 3 ] || [ "$attempt" -eq "$attempts" ]; then break; fi', text)
        self.assertEqual(docs.count("| `attempts` | `4` |"), 2)
        self.assertEqual(docs.count("| `retry-delay-seconds` | `90` |"), 2)


class WorkflowPolicy(unittest.TestCase):
    FILES = sorted((REPO / ".github" / "workflows").glob("profile-consistency*.yml")) + \
        sorted((REPO / "profile-consistency" / "examples").glob("*.yml"))

    def test_files_exist(self) -> None:
        self.assertGreaterEqual(len(self.FILES), 3)

    def test_policies(self) -> None:
        for path in self.FILES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertRegex(text, r"(?m)^permissions:\s*\n\s+contents:\s*read\s*$")
                writes = set(re.findall(r"(?m)^\s+([a-z-]+):\s*write\s*$", text))
                # Only the sync workflow (and callers of it) may write, and only to open pull requests.
                allowed = {"contents", "pull-requests"} if "sync" in path.name else set()
                self.assertLessEqual(writes, allowed, f"unexpected write permissions: {writes - allowed}")
                if writes:
                    self.assertNotRegex(text, r"git push[^\n]*(main|master|default_branch)")
                    self.assertNotRegex(text, r"gh pr merge")
                self.assertNotRegex(text, r"\$\{\{\s*secrets\.")
                self.assertNotIn("pull_request_target", text)
                for match in re.finditer(r"(?m)^\s*(?:-\s*)?uses:\s*([^\s#]+)\s*(#.*)?$", text):
                    ref = match.group(1)
                    if ref.startswith("./") or "JSP-IT-Solutions-Group/.github/" in ref:
                        continue
                    self.assertRegex(ref, r"@[0-9a-f]{40}$")
                    self.assertTrue(match.group(2), f"{ref} needs a version comment")
                for block in re.findall(r"run: \|\n((?:\s{10,}.*\n?)+)", text):
                    self.assertNotIn("${{", block)
                self.assertEqual(len(re.findall(r"persist-credentials:\s*false", text)),
                                 len(re.findall(r"uses:\s*actions/checkout@", text)))


if __name__ == "__main__":
    unittest.main()
