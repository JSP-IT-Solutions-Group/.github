#!/usr/bin/env python3
"""Update a profile README from the English JSP-Product catalogue.

Writes two things, and nothing else:

* the solution and group counts in count statements such as
  "102 software solutions across 15 solution groups";
* a generated list of all solutions, grouped by solution group, with direct
  links to the English product pages, between these markers:

      <!-- jsp-catalogue:start -->
      ...
      <!-- jsp-catalogue:end -->

When the markers are missing, the section (with a heading) is inserted after
the line linking to the complete catalogue.

It only edits the local file. Committing and opening a pull request is left to
the calling workflow, so every change is reviewed by a person before merging.
If the catalogue data is unavailable, nothing is written.

Exit codes: 0 done (changed or not), 2 usage/profile error, 3 catalogue data
unavailable. Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import html as html_lib
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_profile_consistency as pc  # noqa: E402

__version__ = pc.__version__

MARK_START = pc.MARK_START
MARK_END = pc.MARK_END
SECTION_HEADING = "### 📋 All JSP-Product™ solutions"
INSERT_AFTER = re.compile(r"^.*Explore the complete JSP-Product.*catalogue.*$", re.MULTILINE)
# Sources tried in order: /catalogue.json, then the HTML overview page.
DEFAULT_CATALOGUE = pc.DEFAULT_CATALOGUES["en"]
# Public overview page: base for group links and for product links that the
# source does not provide.
DEFAULT_SITE = pc.OVERVIEW_PAGES["en"]


class SyncError(Exception):
    """The profile cannot be updated safely (reported, exit code 2)."""


class Unavailable(Exception):
    """The catalogue data could not be read (exit code 3, nothing written)."""


@dataclass
class Product:
    type: str
    name: str
    summary: str
    url: str | None = None


@dataclass
class Group:
    key: str
    label: str
    products: list[Product] = field(default_factory=list)


# --------------------------------------------------------------------------
# Catalogue parsing
# --------------------------------------------------------------------------

_CARD = re.compile(r"<(article|div)\b[^>]*\bdata-product-category\s*=\s*[\"']?([\w-]+)[\"']?[^>]*>(.*?)</\1>",
                   re.IGNORECASE | re.DOTALL)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def parse_catalogue_json(body: str) -> list[Group]:
    """Groups and products from /catalogue.json (schema 1). Raises Unavailable."""
    try:
        data = pc.parse_catalogue_json(body)
    except ValueError as exc:
        raise Unavailable(f"catalogue JSON unusable: {exc}") from exc
    problems = pc.json_problems(data)
    if problems:
        raise Unavailable("catalogue JSON is inconsistent: " + "; ".join(problems))
    groups = {c["key"]: Group(c["key"], str(c.get("label") or c["key"])) for c in data["categories"]}
    for item in data["products"]:
        url = item.get("url")
        groups[item["category"]].products.append(Product(
            item["id"], str(item.get("name") or item["id"]), str(item.get("summary") or ""),
            url if isinstance(url, str) and url.startswith("https://") else None))
    if not data["products"]:
        raise Unavailable("catalogue JSON lists no products")
    return list(groups.values())


def parse_source(fetched: pc.Fetched) -> list[Group]:
    if fetched.error:
        raise Unavailable(fetched.error)
    if pc.is_json_source(fetched):
        return parse_catalogue_json(fetched.body)
    return parse_catalogue(fetched.body)


def parse_catalogue(document: str) -> list[Group]:
    """Groups in catalogue order, products in page order (HTML page). Raises Unavailable."""
    try:
        categories = pc._island(document, pc.CATEGORIES_ISLAND)
        advisor = pc._island(document, pc.PRODUCTS_ISLAND)
    except LookupError as exc:
        raise Unavailable(str(exc)) from exc
    if not isinstance(categories, dict) or not isinstance(advisor, list):
        raise Unavailable("structured catalogue data has an unexpected shape")
    summaries = {str(a.get("id")): str(a.get("summary") or "") for a in advisor if isinstance(a, dict)}

    groups = {key: Group(key, str(value.get("label") or key)) for key, value in categories.items()
              if isinstance(value, dict)}
    seen: set[str] = set()
    for _, category, body in _CARD.findall(document):
        link = re.search(r"\btype=(products_[a-z0-9_]+)", body)
        name = re.search(r"<h[2-5][^>]*>(.*?)</h[2-5]>", body, re.DOTALL)
        if not link or not name:
            continue
        ptype = link.group(1)
        if ptype in seen:
            continue
        seen.add(ptype)
        subtitle = re.search(r"class=[\"']?product-subtitle[\"']?[^>]*>(.*?)(?:</p>|<div|<p\b)", body, re.DOTALL)
        summary = summaries.get(ptype) or (_text(subtitle.group(1)) if subtitle else "")
        if category not in groups:
            raise Unavailable(f"product {ptype} uses undefined group {category!r}")
        groups[category].products.append(Product(ptype, _text(name.group(1)), summary))

    if seen != set(summaries):
        missing = sorted(set(summaries) - seen)
        extra = sorted(seen - set(summaries))
        raise Unavailable("product cards and structured data disagree"
                          + (f"; no card for {', '.join(missing[:10])}" if missing else "")
                          + (f"; not in structured data: {', '.join(extra[:10])}" if extra else ""))
    if not seen:
        raise Unavailable("no products found")
    return list(groups.values())


# --------------------------------------------------------------------------
# Rendering and README update
# --------------------------------------------------------------------------


def _md(text: str) -> str:
    text = text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return text.replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|")


def render_section(groups: list[Group], catalogue_url: str) -> str:
    base = catalogue_url.rsplit("/", 1)[0] + "/"
    overview = catalogue_url.split("?", 1)[0]
    lines = [MARK_START,
             f"<!-- Generated from {overview} by the weekly catalogue sync. Edits here are overwritten. -->", ""]
    for group in groups:
        if not group.products:
            continue
        count = len(group.products)
        lines += ["<details>",
                  f"<summary><b>{html_lib.escape(group.label)}</b> · {count} "
                  f"{'solution' if count == 1 else 'solutions'}</summary>", ""]
        for product in group.products:
            url = product.url or urllib.parse.urljoin(base, "products_show?type=" + urllib.parse.quote(product.type))
            item = f"- [{_md(product.name)}]({url})"
            if product.summary:
                item += f": {_md(product.summary)}"
            lines.append(item)
        group_url = f"{overview}?category={urllib.parse.quote(group.key)}"
        lines += ["", f"[View this group in the catalogue →]({group_url})", "", "</details>", ""]
    lines.append(MARK_END)
    return "\n".join(lines)


def split_generated(text: str) -> tuple[str, str | None, str]:
    start, end = text.find(MARK_START), text.find(MARK_END)
    if start == -1 and end == -1:
        return text, None, ""
    if start == -1 or end == -1 or end < start or text.count(MARK_START) > 1 or text.count(MARK_END) > 1:
        raise SyncError("catalogue markers are incomplete, duplicated or in the wrong order")
    return text[:start], text[start:end + len(MARK_END)], text[end + len(MARK_END):]


def _replace_counts(text: str, solutions: int, groups: int) -> tuple[str, list[str]]:
    changes: list[str] = []

    def apply(patterns: list[re.Pattern[str]], value: int, label: str, text: str) -> str:
        for pattern in patterns:
            def sub(match: re.Match[str]) -> str:
                old = int(match.group(1))
                if old != value:
                    changes.append(f"{label}: {old} → {value}")
                s, e = match.span(1)
                return match.group(0)[: s - match.start()] + str(value) + match.group(0)[e - match.start():]
            text = pattern.sub(sub, text)
        return text

    # Bold markers (**102 software solutions**) do not affect the patterns.
    text = apply(pc.PROFILE_SOLUTION_PATTERNS, solutions, "solutions", text)
    text = apply(pc.PROFILE_GROUP_PATTERNS, groups, "solution groups", text)
    return text, changes


def update_profile(text: str, groups: list[Group], catalogue_url: str) -> tuple[str, list[str]]:
    """Return the updated README text and a human-readable list of changes."""
    total = sum(len(g.products) for g in groups)
    # Same definition as the consistency check and the site's filters: every
    # group defined in the catalogue counts, even if it is currently empty.
    used_groups = len(groups)
    before, block, after = split_generated(text)
    section = render_section(groups, catalogue_url)

    # Counts are only updated outside the generated list (its group headings
    # also say "N solutions").
    before, changes = _replace_counts(before, total, used_groups)
    after, more = _replace_counts(after, total, used_groups)
    changes += more

    if block is None:
        match = INSERT_AFTER.search(before)
        if not match:
            raise SyncError("no catalogue markers and no line linking to the complete catalogue to insert after")
        before = before[: match.end()] + f"\n\n{SECTION_HEADING}\n\n{section}" + before[match.end():]
        changes.append(f"added the generated solution list ({total} solutions in {used_groups} groups)")
        return before + after, changes

    old_types = set(re.findall(r"\btype=(products_[a-z0-9_]+)", block))
    new_types = {p.type for g in groups for p in g.products}
    added, removed = sorted(new_types - old_types), sorted(old_types - new_types)
    names = {p.type: p.name for g in groups for p in g.products}
    if added:
        changes.append("added: " + ", ".join(names.get(t, t) for t in added))
    if removed:
        changes.append("removed: " + ", ".join(removed))
    if block != section and not added and not removed:
        changes.append("updated names, descriptions, grouping or order in the generated list")
    return before + section + after, changes


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sync_profile.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--profile", required=True, help="README to update")
    p.add_argument("--catalogue", default=DEFAULT_CATALOGUE,
                   help="English catalogue source(s): /catalogue.json, the overview page or a local file; "
                        "'|' separates fallbacks")
    p.add_argument("--site-url", default=DEFAULT_SITE,
                   help="public overview page: base for group links and missing product links")
    p.add_argument("--write", action="store_true", help="write changes (default: dry run)")
    p.add_argument("--pr-body", default="", help="write a pull request description here")
    p.add_argument("--github-output", default="")
    p.add_argument("--summary-file", default="")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--tool-version", action="version", version=f"profile-consistency {__version__}")
    return p.parse_args(argv)


def _outputs(path: str, **values: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def _summary(path: str, text: str) -> None:
    print(text)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def main(argv: list[str] | None = None, fetch=pc.http_get) -> int:
    args = parse_args(argv)
    profile = Path(args.profile)
    if not profile.is_file():
        _summary(args.summary_file, f"## Catalogue sync: ❌ profile `{args.profile}` not found")
        _outputs(args.github_output, changed="false", unavailable="false", error="true")
        return 2
    get = lambda url: fetch(url, timeout=args.timeout, retries=args.retries)  # noqa: E731
    groups: list[Group] | None = None
    used_source, failures = "", []
    for source in [s.strip() for s in args.catalogue.split("|") if s.strip()]:
        loaded = pc.load_source(source, get)
        try:
            groups = parse_source(loaded)
            used_source = source
            break
        except Unavailable as exc:
            detail = str(exc)
            if not loaded.error:
                detail += f" ({pc.describe_response(loaded)})"
            failures.append(f"`{source}`: {detail}")
    if groups is None:
        _summary(args.summary_file, "## Catalogue sync: ⚠️ catalogue data unavailable, nothing changed\n\n"
                 + "\n".join(f"- {f}" for f in failures))
        _outputs(args.github_output, changed="false", unavailable="true", error="false")
        return 3

    original = profile.read_text(encoding="utf-8")
    try:
        updated, changes = update_profile(original, groups, args.site_url)
    except SyncError as exc:
        _summary(args.summary_file, f"## Catalogue sync: ❌ cannot update `{args.profile}`\n\n{exc}")
        _outputs(args.github_output, changed="false", unavailable="false", error="true")
        return 2

    total = sum(len(g.products) for g in groups)
    used = len(groups)
    changed = updated != original
    if changed and args.write:
        profile.write_text(updated, encoding="utf-8", newline="\n")
    status = ("✏️ profile updated" if args.write else "✏️ changes needed (dry run)") if changed else "✅ up to date"
    lines = [f"## Catalogue sync: {status}", "",
             f"English catalogue: **{total} solutions in {used} groups** (`{used_source}`).", ""]
    if failures:
        lines += ["Fallback source used because:"] + [f"- {f}" for f in failures] + [""]
    lines += [f"- {c}" for c in changes] if changed else ["No changes needed."]
    _summary(args.summary_file, "\n".join(lines))

    if args.pr_body and changed:
        body = ["Automated weekly sync of the JSP-Product™ solution list and counts from the English catalogue "
                f"(`{used_source}`).", "",
                f"The catalogue currently has **{total} solutions in {used} solution groups**.", "", "### Changes", ""]
        body += [f"- {c}" for c in changes]
        body += ["", "### Review", "",
                 "- Generated from the catalogue's structured data. Only the counts and the section between the "
                 "`jsp-catalogue` markers are changed.",
                 "- Please check that the catalogue change is intended before merging. This pull request is never "
                 "merged automatically.",
                 f"- Generated by `sync_profile.py` {__version__} (JSP-IT-Solutions-Group/.github)."]
        Path(args.pr_body).write_text("\n".join(body) + "\n", encoding="utf-8")
    _outputs(args.github_output, changed=str(changed).lower(), unavailable="false", error="false",
             solutions=str(total), groups=str(used))
    return 0


if __name__ == "__main__":
    sys.exit(main())
