#!/usr/bin/env python3
"""Read-only consistency check between GitHub profiles and the JSP-Product catalogues.

It reports, and never edits anything:

* catalogue counts (solutions and solution groups) for each language, taken from
  the structured JSON embedded in the catalogue page, cross-checked against the
  product cards and the visible counter;
* language parity (same counts, product ids and groups in every language);
* count statements in profile READMEs that disagree with the catalogue;
* broken links in the profile READMEs.

Outcomes are PASS / WARN / FAIL / SKIP. "Data unavailable" (network errors,
missing structured data) is reported separately from "discrepancy", so a site
outage is never mistaken for wrong profile numbers.

Exit codes: 0 consistent (warnings allowed), 1 discrepancy or broken link,
2 usage error, 3 catalogue data unavailable (and no discrepancy found).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html as html_lib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

__version__ = "1.4.0"

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
_SEVERITY = {SKIP: 0, PASS: 1, WARN: 2, FAIL: 3}
_LABEL = {PASS: "\u2705 PASS", WARN: "\u26a0\ufe0f WARN", FAIL: "\u274c FAIL", SKIP: "\u23ed\ufe0f SKIP"}
CATEGORIES = ("Catalogue", "Language parity", "Profile counts", "Profile list", "Profile links")
# Generated solution list written by sync_profile.py (excluded from count and link checks).
MARK_START = "<!-- jsp-catalogue:start -->"
MARK_END = "<!-- jsp-catalogue:end -->"

# Human-facing catalogue pages (also the base for generated group links).
OVERVIEW_PAGES = {
    "en": "https://jsp-it.com/products_jsp_overview",
    "fi": "https://jsp-it.fi/products_jsp_overview",
}
# Sources per language, tried in order ("|"-separated): the machine-readable
# /catalogue.json first, the HTML overview page as a fallback.
DEFAULT_CATALOGUES = {
    "en": "https://jsp-it.com/catalogue.json|https://jsp-it.com/products_jsp_overview",
    "fi": "https://jsp-it.fi/catalogue.json|https://jsp-it.fi/products_jsp_overview",
}
USER_AGENT = f"jsp-profile-consistency-check/{__version__} (+https://github.com/JSP-IT-Solutions-Group/.github)"
MAX_BYTES = 8 * 1024 * 1024

PRODUCTS_ISLAND = "jsp-solution-advisor-catalog"
CATEGORIES_ISLAND = "jsp-product-category-catalog"
COUNTER_PATTERNS = {
    "en": re.compile(r"\b(\d{1,4})\s+solutions\b", re.IGNORECASE),
    "fi": re.compile(r"\b(\d{1,4})\s+ratkaisua\b", re.IGNORECASE),
}
# Count statements in profile READMEs (English and Finnish wording).
PROFILE_SOLUTION_PATTERNS = [
    re.compile(r"\b(\d{1,4})\s+(?:[\w\u2122-]+\s+){0,3}?solutions\b(?!\s+groups)", re.IGNORECASE),
    re.compile(r"\b(\d{1,4})\s+(?:[\w\u2122-]+\s+){0,3}?ratkaisua\b", re.IGNORECASE),
]
PROFILE_GROUP_PATTERNS = [
    re.compile(r"\b(\d{1,4})\s+(?:solution|product)\s+groups\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,4})\s+ratkaisuryhm(?:ää|ässä|ään|än|ä)\b", re.IGNORECASE),
]


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


@dataclass
class Finding:
    category: str
    check: str
    status: str
    message: str
    details: list[str] = field(default_factory=list)


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    unavailable: bool = False
    catalogue_summary: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, category: str, check: str, status: str, message: str,
            details: list[str] | None = None) -> None:
        self.findings.append(Finding(category, check, status, message, list(details or [])))

    def count(self, status: str) -> int:
        return sum(1 for f in self.findings if f.status == status)

    @property
    def result(self) -> str:
        worst = max((_SEVERITY[f.status] for f in self.findings), default=_SEVERITY[PASS])
        return {3: FAIL, 2: WARN}.get(worst, PASS)

    @property
    def exit_code(self) -> int:
        if self.count(FAIL):
            return 1
        return 3 if self.unavailable else 0


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


@dataclass
class Fetched:
    url: str
    status: int | None
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None


def http_get(url: str, *, timeout: float, retries: int, method: str = "GET",
             read_body: bool = True) -> Fetched:
    """GET (or HEAD) with no-cache headers and retries on transient errors."""
    last: Fetched | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, method=method, headers={
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (http/https only)
                body = ""
                if read_body:
                    raw = response.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        return Fetched(url, response.status, error=f"response larger than {MAX_BYTES} bytes")
                    charset = response.headers.get_content_charset() or "utf-8"
                    body = raw.decode(charset, errors="replace")
                return Fetched(url, response.status, body, {k.lower(): v for k, v in response.headers.items()})
        except urllib.error.HTTPError as exc:
            last = Fetched(url, exc.code, error=f"HTTP {exc.code}")
            if exc.code < 500 and exc.code != 429:
                return last
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            last = Fetched(url, None, error=f"{type(exc).__name__}: {reason}")
        if attempt < retries:
            time.sleep(min(2 ** attempt, 8))
    assert last is not None
    return last


def load_source(spec: str, fetch: Callable[[str], Fetched]) -> Fetched:
    """Load a URL (http/https) or a local file path."""
    if re.match(r"^https?://", spec, re.IGNORECASE):
        return fetch(spec)
    path = Path(spec)
    if not path.is_file():
        return Fetched(spec, None, error="file not found")
    return Fetched(spec, 200, path.read_text(encoding="utf-8-sig", errors="replace"))


# --------------------------------------------------------------------------
# Catalogue extraction
# --------------------------------------------------------------------------


@dataclass
class Catalogue:
    language: str
    source: str
    solutions: int | None = None
    groups: int | None = None
    product_ids: set[str] = field(default_factory=set)
    group_keys: set[str] = field(default_factory=set)
    signals: dict[str, Any] = field(default_factory=dict)
    cache_note: str | None = None
    error: str | None = None


def _island(document: str, element_id: str) -> Any:
    match = re.search(
        rf"<script\b[^>]*\bid\s*=\s*[\"']?{re.escape(element_id)}[\"']?[^>]*>(.*?)</script>",
        document, re.IGNORECASE | re.DOTALL)
    if not match:
        raise LookupError(f"structured data element #{element_id} not found")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise LookupError(f"#{element_id} is not valid JSON: {exc}") from exc


def visible_text(document: str) -> str:
    text = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", document, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text))


JSON_SCHEMA_VERSION = 1


def describe_response(fetched: Fetched) -> str:
    """Short description of what the server sent (for 'data unavailable' messages)."""
    document = fetched.body or ""
    title = re.search(r"<title[^>]*>(.*?)</title>", document, re.IGNORECASE | re.DOTALL)
    return (f"server answered HTTP {fetched.status} with {len(document)} characters"
            + (f", title “{html_lib.unescape(title.group(1)).strip()[:80]}”" if title else "")
            + (f", content type {fetched.headers['content-type']}" if fetched.headers.get("content-type") else "")
            + (f", server {fetched.headers['server']}" if fetched.headers.get("server") else ""))


def parse_catalogue_json(body: str) -> dict[str, Any]:
    """Validate a /catalogue.json document (schema version 1) and return it.

    Raises ValueError when it cannot be used at all. Unknown fields are ignored.
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON document is not an object")
    if data.get("version") != JSON_SCHEMA_VERSION:
        raise ValueError(f"unsupported schema version {data.get('version')!r} (expected {JSON_SCHEMA_VERSION})")
    categories, products = data.get("categories"), data.get("products")
    if not isinstance(categories, list) or not isinstance(products, list):
        raise ValueError("'categories' and 'products' must be lists")
    if not all(isinstance(c, dict) and isinstance(c.get("key"), str) and c["key"] for c in categories):
        raise ValueError("every category needs a non-empty 'key'")
    if not all(isinstance(p, dict) and isinstance(p.get("id"), str) and p["id"] for p in products):
        raise ValueError("every product needs a non-empty 'id'")
    return data


def json_problems(data: dict[str, Any], language: str | None = None) -> list[str]:
    """Consistency problems in a valid catalogue JSON (reported as warnings)."""
    products, keys = data["products"], [c["key"] for c in data["categories"]]
    problems = []
    if data.get("count") != len(products):
        problems.append(f"count {data.get('count')!r} != {len(products)} products")
    unknown = sorted({str(p.get("category")) for p in products if p.get("category") not in keys})
    if unknown:
        problems.append("products in undefined groups: " + ", ".join(unknown))
    ids = [p["id"] for p in products]
    if len(ids) != len(set(ids)):
        problems.append("duplicate product ids")
    if language and data.get("language") not in (None, language):
        problems.append(f"language is {data.get('language')!r}, expected {language!r}")
    return problems


def _cache_note(fetched: Fetched) -> str | None:
    age = fetched.headers.get("age")
    x_cache = fetched.headers.get("x-cache") or fetched.headers.get("cf-cache-status")
    if (age and age.strip() not in ("", "0")) or (x_cache and "hit" in x_cache.lower()):
        return f"response may come from a cache (Age={age or '-'}, cache={x_cache or '-'})"
    return None


def is_json_source(fetched: Fetched) -> bool:
    path = urllib.parse.urlparse(fetched.url).path if "://" in fetched.url else fetched.url
    return path.lower().endswith(".json") or (fetched.body or "").lstrip().startswith("{")


def extract_catalogue_json(language: str, fetched: Fetched) -> Catalogue:
    cat = Catalogue(language, fetched.url)
    if fetched.error:
        cat.error = fetched.error
        return cat
    cat.cache_note = _cache_note(fetched)
    try:
        data = parse_catalogue_json(fetched.body)
    except ValueError as exc:
        cat.error = (f"catalogue JSON unusable: {exc} ({describe_response(fetched)}; "
                     "a bot-protection or error page is a likely cause)")
        return cat
    cat.product_ids = {p["id"] for p in data["products"]}
    cat.group_keys = {c["key"] for c in data["categories"]}
    cat.solutions = len(cat.product_ids)
    cat.groups = len(data["categories"])
    cat.signals["json products"] = len(data["products"])
    cat.signals["json groups"] = cat.groups
    problems = json_problems(data, language)
    if problems:
        cat.signals["json problems"] = "; ".join(problems)
    return cat


def extract_any_catalogue(language: str, fetched: Fetched) -> Catalogue:
    if not fetched.error and is_json_source(fetched):
        return extract_catalogue_json(language, fetched)
    return extract_catalogue(language, fetched)


def load_catalogue(language: str, spec: str, get: Callable[[str], Fetched]) -> Catalogue:
    """Try each "|"-separated source in order; the first usable one wins."""
    failures: list[str] = []
    cat: Catalogue | None = None
    for source in [s.strip() for s in spec.split("|") if s.strip()]:
        cat = extract_any_catalogue(language, load_source(source, get))
        if not cat.error:
            if failures:
                cat.signals["source fallback"] = "used after: " + " | ".join(failures)
            return cat
        failures.append(f"{source}: {cat.error}")
    assert cat is not None, "empty catalogue source list"
    if len(failures) > 1:
        cat.error = " | ".join(failures)
    return cat


def extract_catalogue(language: str, fetched: Fetched) -> Catalogue:
    """Read the HTML overview page (structured JSON islands, cards, counter)."""
    cat = Catalogue(language, fetched.url)
    if fetched.error:
        cat.error = fetched.error
        return cat
    cat.cache_note = _cache_note(fetched)
    document = fetched.body

    try:
        products = _island(document, PRODUCTS_ISLAND)
        if not isinstance(products, list):
            raise LookupError(f"#{PRODUCTS_ISLAND} is not a list")
        cat.product_ids = {str(p.get("id")) for p in products if isinstance(p, dict) and p.get("id")}
        cat.signals["structured products"] = len(cat.product_ids)
        if len(cat.product_ids) != len(products):
            cat.signals["structured entries (incl. duplicates/no id)"] = len(products)
        cat.solutions = len(cat.product_ids)
    except LookupError as exc:
        cat.signals["structured products"] = str(exc)
    try:
        categories = _island(document, CATEGORIES_ISLAND)
        if not isinstance(categories, dict):
            raise LookupError(f"#{CATEGORIES_ISLAND} is not an object")
        cat.group_keys = set(categories)
        cat.groups = len(cat.group_keys)
        cat.signals["structured groups"] = cat.groups
    except LookupError as exc:
        cat.signals["structured groups"] = str(exc)

    cards = re.findall(r"\bdata-product-category\s*=\s*[\"']?([\w-]+)", document)
    if cards:
        cat.signals["product cards"] = len(cards)
        cat.signals["card groups"] = len(set(cards))
    counter = COUNTER_PATTERNS.get(language)
    if counter:
        values = sorted({int(v) for v in counter.findall(visible_text(document))})
        if values:
            cat.signals["visible counter"] = values if len(values) > 1 else values[0]

    # Fall back to the cards only when the structured data is missing.
    if cat.solutions is None and cards:
        cat.solutions = len(cards)
        cat.signals["fallback"] = "solutions counted from product cards"
    if cat.groups is None and cards:
        cat.groups = len(set(cards))
        cat.signals["fallback"] = "counted from product cards"
    if cat.solutions is None or cat.groups is None:
        cat.error = "catalogue counts could not be determined: " + "; ".join(
            str(v) for k, v in cat.signals.items() if k.startswith("structured") and isinstance(v, str)) \
            + f" ({describe_response(fetched)}; a bot-protection or error page is a likely cause)"
    return cat


def check_catalogues(catalogues: list[Catalogue], report: Report) -> None:
    for cat in catalogues:
        name = f"{cat.language.upper()} catalogue"
        report.catalogue_summary[cat.language] = {
            "source": cat.source, "solutions": cat.solutions, "groups": cat.groups,
            "signals": cat.signals, "error": cat.error,
        }
        if cat.error:
            report.unavailable = True
            report.add("Catalogue", name, WARN, f"Data unavailable from `{cat.source}`: {cat.error}",
                       ["Not treated as a discrepancy. Re-run later or check the site."])
            continue
        details = [f"{k}: {v}" for k, v in cat.signals.items()]
        if cat.cache_note:
            details.append(cat.cache_note)
        disagreements = []
        if "product cards" in cat.signals and cat.signals["product cards"] != cat.solutions:
            disagreements.append(f"product cards {cat.signals['product cards']} != {cat.solutions}")
        if "card groups" in cat.signals and cat.signals["card groups"] > cat.groups:
            disagreements.append(f"cards use {cat.signals['card groups']} groups > {cat.groups} defined")
        counter = cat.signals.get("visible counter")
        if counter is not None and counter != cat.solutions:
            disagreements.append(f"visible counter {counter} != {cat.solutions}")
        if "json problems" in cat.signals:
            disagreements.append(str(cat.signals["json problems"]))
        if "source fallback" in cat.signals:
            report.add("Catalogue", name, WARN,
                       f"{cat.solutions} solutions, {cat.groups} groups from the fallback source "
                       f"`{cat.source}`; the primary source was unavailable"
                       + (": " + "; ".join(disagreements) if disagreements else ""), details)
        elif "fallback" in cat.signals:
            report.add("Catalogue", name, WARN,
                       f"{cat.solutions} solutions, {cat.groups} groups (structured data missing or "
                       "invalid; fell back to product cards)", details)
        elif disagreements:
            report.add("Catalogue", name, WARN,
                       f"{cat.solutions} solutions, {cat.groups} groups, but page signals disagree: "
                       + "; ".join(disagreements), details)
        else:
            report.add("Catalogue", name, PASS, f"{cat.solutions} solutions in {cat.groups} groups", details)

    available = [c for c in catalogues if not c.error]
    if len(available) < 2:
        report.add("Language parity", "Parity", SKIP,
                   "Needs at least two available catalogues" if len(catalogues) > 1 else
                   "Only one catalogue configured")
        return
    base = available[0]
    for other in available[1:]:
        label = f"{base.language.upper()} vs {other.language.upper()}"
        problems = []
        if base.solutions != other.solutions:
            problems.append(f"solutions {base.solutions} vs {other.solutions}")
        if base.groups != other.groups:
            problems.append(f"groups {base.groups} vs {other.groups}")
        details = []
        if base.product_ids and other.product_ids:
            only_base = sorted(base.product_ids - other.product_ids)
            only_other = sorted(other.product_ids - base.product_ids)
            if only_base:
                details.append(f"only in {base.language.upper()}: " + ", ".join(f"`{i}`" for i in only_base[:30]))
            if only_other:
                details.append(f"only in {other.language.upper()}: " + ", ".join(f"`{i}`" for i in only_other[:30]))
        if base.group_keys and other.group_keys and base.group_keys != other.group_keys:
            details.append("group keys differ: " + ", ".join(sorted(base.group_keys ^ other.group_keys)))
        if problems or details:
            report.add("Language parity", label, FAIL,
                       "Catalogues differ" + (": " + "; ".join(problems) if problems else ""), details)
        else:
            report.add("Language parity", label, PASS,
                       f"Same {base.solutions} solutions and {base.groups} groups"
                       + (" (product ids match)" if base.product_ids else ""))


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------


def strip_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[*_`]", "", text)
    return re.sub(r"[ \t]+", " ", text)


def find_counts(text: str, patterns: list[re.Pattern[str]]) -> list[tuple[int, str]]:
    plain = strip_markdown(text)
    found = []
    for pattern in patterns:
        for match in pattern.finditer(plain):
            start = max(0, match.start() - 30)
            found.append((int(match.group(1)), plain[start:match.end() + 10].strip().replace("\n", " ")))
    return found


_LINK_PATTERNS = [
    re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)"),  # [t](url) / ![a](url)
    re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", re.MULTILINE),  # reference definitions
    re.compile(r"<(https?://[^>\s]+)>"),  # autolinks
    re.compile(r"\b(?:href|src)\s*=\s*\"([^\"]+)\""),  # inline HTML
    re.compile(r"(?<![(<\"'=\]])\b(https?://[^\s)<>\"'\]]+)"),  # bare URLs
]


def extract_links(markdown: str) -> list[str]:
    text = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", " ", text)
    links: list[str] = []
    for pattern in _LINK_PATTERNS:
        for match in pattern.finditer(text):
            url = match.group(1).rstrip(".,;:")
            if url not in links:
                links.append(url)
    return links


def split_generated_list(text: str) -> tuple[str, str | None]:
    """Return (profile text without the generated list, the generated list or None)."""
    start, end = text.find(MARK_START), text.find(MARK_END)
    if start == -1 or end == -1 or end < start:
        return text, None
    return text[:start] + text[end + len(MARK_END):], text[start:end + len(MARK_END)]


def check_profile_list(name: str, block: str | None, reference: Catalogue | None, report: Report) -> None:
    if block is None:
        return
    listed = set(re.findall(r"\btype=(products_[a-z0-9_]+)", block))
    if reference is None or reference.error or not reference.product_ids:
        report.add("Profile list", name, WARN, f"Cannot compare the generated list ({len(listed)} solutions): "
                   "catalogue data unavailable")
        return
    missing = sorted(reference.product_ids - listed)
    extra = sorted(listed - reference.product_ids)
    if missing or extra:
        details = ([f"missing: {', '.join(f'`{m}`' for m in missing[:30])}"] if missing else [])
        if extra:
            details.append(f"no longer in the catalogue: {', '.join(f'`{e}`' for e in extra[:30])}")
        details.append("The weekly catalogue sync opens a pull request with the update.")
        report.add("Profile list", name, FAIL,
                   f"Generated list has {len(listed)} solutions; the catalogue has {len(reference.product_ids)}",
                   details)
    else:
        report.add("Profile list", name, PASS, f"Lists all {len(listed)} catalogue solutions")


def check_profile_counts(name: str, text: str, require: bool, reference: Catalogue | None,
                         report: Report) -> None:
    solutions = find_counts(text, PROFILE_SOLUTION_PATTERNS)
    groups = find_counts(text, PROFILE_GROUP_PATTERNS)
    if not solutions and not groups:
        report.add("Profile counts", name, FAIL if require else SKIP,
                   "No catalogue count statement found" + (" (counts are required for this profile)"
                                                           if require else ""))
        return
    if reference is None or reference.error:
        report.add("Profile counts", name, WARN,
                   "Cannot compare: catalogue data unavailable",
                   [f"profile says {n}: \u201c{ctx}\u201d" for n, ctx in solutions + groups])
        return
    for label, values, actual in (("solutions", solutions, reference.solutions),
                                  ("groups", groups, reference.groups)):
        if not values:
            if require:
                report.add("Profile counts", f"{name}: {label}", FAIL,
                           f"No {label} count found (catalogue has {actual})")
            continue
        wrong = [(n, ctx) for n, ctx in values if n != actual]
        if wrong:
            report.add("Profile counts", f"{name}: {label}", FAIL,
                       f"Profile states {', '.join(str(n) for n, _ in wrong)} {label}; the "
                       f"{reference.language.upper()} catalogue has {actual}",
                       [f"\u201c{ctx}\u201d" for _, ctx in wrong] +
                       ["Update the profile by hand; this check never edits it."])
        else:
            report.add("Profile counts", f"{name}: {label}", PASS,
                       f"States {actual} {label}, matching the catalogue")


def check_profile_links(name: str, base: str, text: str, report: Report,
                        fetch_status: Callable[[str], Fetched], cache: dict[str, Fetched]) -> None:
    broken, unverified, ok = [], [], 0
    for url in extract_links(text)[:200]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in ("mailto", "tel") or url.startswith("#"):
            continue
        if parsed.scheme in ("http", "https"):
            if url not in cache:
                cache[url] = fetch_status(url)
            result = cache[url]
            if result.status is not None and 200 <= result.status < 400:
                ok += 1
            elif result.status in (404, 410):
                broken.append(f"`{url}`: HTTP {result.status}")
            else:
                unverified.append(f"`{url}`: {result.error or result.status}")
        elif parsed.scheme:
            unverified.append(f"`{url}`: unsupported scheme")
        else:  # relative link: only checkable for local profiles
            if re.match(r"^https?://", base):
                unverified.append(f"`{url}`: relative link in a remote profile")
                continue
            target = (Path(base).parent / urllib.parse.unquote(parsed.path)).resolve()
            if target.exists():
                ok += 1
            else:
                broken.append(f"`{url}`: file not found")
    if broken:
        report.add("Profile links", name, FAIL, f"{len(broken)} broken link(s), {ok} OK", broken + unverified)
    elif unverified:
        report.add("Profile links", name, WARN,
                   f"{len(unverified)} link(s) could not be verified (not necessarily broken), {ok} OK",
                   unverified)
    else:
        report.add("Profile links", name, PASS, f"All {ok} link(s) reachable")


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def md_cell(text: str) -> str:
    text = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("|", "\\|").replace("\r", "").replace("\n", " ")


def render_markdown(report: Report, checked_at: str) -> str:
    title = {PASS: "\u2705 PASS \u2014 profiles match the catalogues",
             WARN: "\u26a0\ufe0f WARN \u2014 no discrepancy found, review the warnings",
             FAIL: "\u274c FAIL \u2014 discrepancies found"}[report.result]
    out = [f"## Profile consistency: {title}", ""]
    if report.unavailable:
        out += ["> **Catalogue data was unavailable** for at least one language. Counts that could not "
                "be compared are not reported as discrepancies.", ""]
    rows = [f"| {lang.upper()} | {md_cell(s['source'])} | {s['solutions'] if s['solutions'] is not None else '—'} | "
            f"{s['groups'] if s['groups'] is not None else '—'} |" for lang, s in report.catalogue_summary.items()]
    if rows:
        out += ["| Catalogue | Source | Solutions | Groups |", "|---|---|---:|---:|", *rows, ""]
    out.append(f"**{report.count(PASS)} passed · {report.count(WARN)} warnings · "
               f"{report.count(FAIL)} failed · {report.count(SKIP)} skipped** · checked {checked_at}")
    for category in CATEGORIES:
        items = [f for f in report.findings if f.category == category]
        if not items:
            continue
        out += ["", f"### {category}", "", "| Status | Check | Result |", "|---|---|---|"]
        for f in sorted(items, key=lambda f: -_SEVERITY[f.status]):
            detail = md_cell(f.message)
            if f.details:
                detail += "<br>" + "<br>".join("• " + md_cell(d) for d in f.details[:25])
            out.append(f"| {_LABEL[f.status]} | {md_cell(f.check)} | {detail} |")
    out += ["", f"<sub>profile-consistency {__version__} · read-only: reports only, never edits profiles "
            "or the website.</sub>", ""]
    return "\n".join(out)


def write_outputs(path: str, report: Report) -> None:
    en = report.catalogue_summary.get("en", {})
    values = {
        "result": report.result,
        "unavailable": str(report.unavailable).lower(),
        "failures": str(report.count(FAIL)),
        "warnings": str(report.count(WARN)),
        "solutions": str(en.get("solutions") or ""),
        "groups": str(en.get("groups") or ""),
    }
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _escape(text: str, prop: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def annotations(report: Report) -> list[str]:
    return [f"::{'error' if f.status == FAIL else 'warning'} "
            f"title={_escape('Profile consistency: ' + f.check, True)}::"
            f"{_escape((f.message + (' — ' + '; '.join(f.details[:5]) if f.details else '')).replace('`', ''))}"
            for f in report.findings if f.status in (FAIL, WARN)]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="check_profile_consistency.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--catalogue", action="append", default=[], metavar="LANG=SOURCE[|FALLBACK...]",
                   help="catalogue source(s) per language: /catalogue.json, the HTML overview page or a "
                        "local file; '|' separates fallbacks (default: en and fi catalogue.json, then the "
                        "overview pages)")
    p.add_argument("--profile", action="append", default=[], metavar="PATH_OR_URL",
                   help="profile README; counts are compared when the profile states them")
    p.add_argument("--counts-profile", action="append", default=[], metavar="PATH_OR_URL",
                   help="profile README that must state the catalogue counts")
    p.add_argument("--reference-language", default="en", help="catalogue that profiles are compared to")
    p.add_argument("--no-links", action="store_true", help="skip link checks")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--summary-file", default="")
    p.add_argument("--github-output", default="")
    p.add_argument("--json-out", default="")
    p.add_argument("--annotations", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--tool-version", action="version", version=f"profile-consistency {__version__}")
    return p.parse_args(argv)


def run(args: argparse.Namespace, fetch: Callable[..., Fetched] = http_get) -> Report:
    report = Report()
    get = lambda url: fetch(url, timeout=args.timeout, retries=args.retries)  # noqa: E731
    head = lambda url: _status(fetch, url, args)  # noqa: E731

    specs: dict[str, str] = {}
    for item in args.catalogue:
        lang, sep, source = item.partition("=")
        if not sep or not lang.strip() or not source.strip():
            raise SystemExit(f"--catalogue expects LANG=URL_OR_FILE, got {item!r}")
        specs[lang.strip().lower()] = source.strip()
    if not specs:
        specs = dict(DEFAULT_CATALOGUES)
    catalogues = [load_catalogue(lang, src, get) for lang, src in specs.items()]
    check_catalogues(catalogues, report)
    reference = next((c for c in catalogues if c.language == args.reference_language.lower()), None)

    link_cache: dict[str, Fetched] = {}
    profiles = [(p, False) for p in args.profile] + [(p, True) for p in args.counts_profile]
    for source, require in profiles:
        loaded = load_source(source, get)
        name = source if len(source) <= 100 else "…" + source[-60:]
        if loaded.error:
            status = FAIL if loaded.status in (404, 410) or loaded.error == "file not found" else WARN
            if status == WARN:
                report.unavailable = True
            report.add("Profile counts", name, status, f"Profile could not be loaded: {loaded.error}")
            continue
        body, generated = split_generated_list(loaded.body)
        check_profile_counts(name, body, require, reference, report)
        check_profile_list(name, generated, reference, report)
        if args.no_links:
            report.add("Profile links", name, SKIP, "Link checks disabled")
        else:
            check_profile_links(name, source, body, report, head, link_cache)
    return report


def _status(fetch: Callable[..., Fetched], url: str, args: argparse.Namespace) -> Fetched:
    result = fetch(url, timeout=args.timeout, retries=args.retries, method="HEAD", read_body=False)
    if result.status in (403, 405, 501) or result.status is None:  # some servers reject HEAD
        result = fetch(url, timeout=args.timeout, retries=args.retries, method="GET", read_body=False)
    return result


def main(argv: list[str] | None = None, fetch: Callable[..., Fetched] = http_get) -> int:
    args = parse_args(argv)
    report = run(args, fetch)
    checked_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    markdown = render_markdown(report, checked_at)
    if not args.quiet:
        print(markdown)
    if args.annotations:
        print("\n".join(annotations(report)))
    if args.summary_file:
        with open(args.summary_file, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    if args.github_output:
        write_outputs(args.github_output, report)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "tool_version": __version__, "checked_at": checked_at, "result": report.result,
            "unavailable": report.unavailable, "catalogues": report.catalogue_summary,
            "findings": [f.__dict__ for f in report.findings]}, indent=2, default=list) + "\n", encoding="utf-8")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
