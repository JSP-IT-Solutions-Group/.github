#!/usr/bin/env python3
"""Update a profile README's list of published open-source projects from GitHub.

The list is generated between these markers, which must already exist in the
README (add them once by hand around the list or table):

    <!-- github-projects:start -->
    ...
    <!-- github-projects:end -->

A repository is listed when it is **public**, not a fork, not archived and not
a template, has a licence that GitHub recognises and has at least one published
release. The account's profile repository (named like the account) and
`.github` are never listed. Private repositories are never read into the list:
they are filtered out twice (by `private` and by `visibility`).

Names come from the repository name ("sbom-release-diff" → "SBOM Release Diff"),
descriptions from the repository's "About" text. Projects are sorted by name.

Formats:
  list   "- **[Name](url)** — Description.<br>" + "Language · Licence · release badge"
  table  | Project | Description | Language | Release | Stars |

It only edits the local file; committing and opening a pull request is left to
the calling workflow. If the GitHub API is unavailable, nothing is written.

Exit codes: 0 done (changed or not), 2 usage/profile error, 3 data unavailable.
Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_profile_consistency as pc  # noqa: E402

__version__ = pc.__version__

MARK_START = "<!-- github-projects:start -->"
MARK_END = "<!-- github-projects:end -->"
API_ROOT = "https://api.github.com"
ACRONYMS = {"ai", "api", "cd", "ci", "cli", "csv", "http", "iot", "json", "llm", "mcp", "oss", "pdf",
            "sbom", "sdk", "sql", "ui", "url", "ux", "wp", "yaml"}
SPECIAL_WORDS = {"github": "GitHub", "javascript": "JavaScript", "typescript": "TypeScript",
                 "dotnet": ".NET", "gitlab": "GitLab", "openapi": "OpenAPI", "wordpress": "WordPress"}

ApiGet = Callable[[str], Any]


class SyncError(Exception):
    """The profile cannot be updated safely (exit code 2)."""


class Unavailable(Exception):
    """GitHub data could not be read (exit code 3, nothing written)."""


@dataclass
class Project:
    repo: str
    name: str
    url: str
    description: str
    language: str
    license: str
    release: str


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------


def make_api_get(token: str | None, timeout: float, retries: int) -> ApiGet:
    """GET a GitHub REST API path. Returns parsed JSON, or None for 404."""

    def get(path: str) -> Any:
        url = path if path.startswith("https://") else API_ROOT + path
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": f"jsp-profile-projects-sync/{__version__}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        last_error = "unknown error"
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                last_error = f"HTTP {exc.code} for {path}"
                if exc.code < 500 and exc.code not in (403, 429):
                    break
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__} for {path}: {getattr(exc, 'reason', exc)}"
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
        raise Unavailable(last_error)

    return get


def list_repositories(owner: str, api: ApiGet) -> list[dict[str, Any]]:
    account = api(f"/users/{urllib.parse.quote(owner)}")
    if not isinstance(account, dict):
        raise SyncError(f"GitHub account {owner!r} not found")
    base = (f"/orgs/{urllib.parse.quote(owner)}/repos?type=public"
            if account.get("type") == "Organization" else f"/users/{urllib.parse.quote(owner)}/repos?type=owner")
    repos: list[dict[str, Any]] = []
    for page in range(1, 51):
        batch = api(f"{base}&per_page=100&page={page}")
        if not isinstance(batch, list):
            raise Unavailable(f"unexpected repository list for {owner}")
        repos.extend(r for r in batch if isinstance(r, dict))
        if len(batch) < 100:
            break
    return repos


def display_name(repo: str) -> str:
    words = []
    for part in [p for p in re.split(r"[-_.]+", repo) if p]:
        low = part.lower()
        words.append(SPECIAL_WORDS.get(low) or (part.upper() if low in ACRONYMS else part[:1].upper() + part[1:]))
    return " ".join(words) or repo


def is_candidate(repo: dict[str, Any], owner: str, exclude: set[str]) -> bool:
    name = str(repo.get("name") or "")
    return (repo.get("private") is False
            and repo.get("visibility", "public") == "public"
            and not repo.get("fork") and not repo.get("archived") and not repo.get("is_template")
            and str((repo.get("owner") or {}).get("login", owner)).lower() == owner.lower()
            and name.lower() not in {owner.lower(), ".github"} | {e.lower() for e in exclude})


def collect_projects(owner: str, api: ApiGet, exclude: set[str]) -> list[Project]:
    projects = []
    for repo in list_repositories(owner, api):
        if not is_candidate(repo, owner, exclude):
            continue
        licence = (repo.get("license") or {}).get("spdx_id")
        if not licence or licence == "NOASSERTION":
            continue
        release = api(f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo['name'])}/releases/latest")
        if not isinstance(release, dict) or not release.get("tag_name") or release.get("draft"):
            continue
        projects.append(Project(
            repo=repo["name"], name=display_name(repo["name"]), url=str(repo.get("html_url") or
                                                                         f"https://github.com/{owner}/{repo['name']}"),
            description=" ".join(str(repo.get("description") or "").split()),
            language=str(repo.get("language") or ""), license=licence, release=str(release["tag_name"])))
    return sorted(projects, key=lambda p: p.name.lower())


# --------------------------------------------------------------------------
# Rendering and README update
# --------------------------------------------------------------------------


def _md(text: str) -> str:
    text = text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return text.replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|")


def render(projects: list[Project], owner: str, fmt: str) -> str:
    lines = [MARK_START, f"<!-- Generated from the public GitHub repositories of {owner} by the weekly profile "
             "sync. Edit a description in the repository's About text; edits here are overwritten. -->", ""]
    if fmt == "list":
        for p in projects:
            desc = _md(p.description)
            if desc and desc[-1] not in ".!?":
                desc += "."
            meta = " · ".join(x for x in (p.language, p.license) if x)
            badge = (f"[![Latest release](https://img.shields.io/github/v/release/{owner}/{p.repo}?label=release)]"
                     f"({p.url}/releases/latest)")
            lines.append(f"- **[{_md(p.name)}]({p.url})**" + (f" — {desc}" if desc else "") + "<br>")
            lines.append(f"  {meta} · {badge}" if meta else f"  {badge}")
    else:
        lines += ["| Project | Description | Language | Release | Stars |",
                  "|---------|-------------|----------|---------|-------|"]
        for p in projects:
            lines.append(f"| [{_md(p.name)}]({p.url}) | {_md(p.description)} | {_md(p.language) or '—'} | "
                         f"![Release](https://img.shields.io/github/v/release/{owner}/{p.repo}) | "
                         f"![Stars](https://img.shields.io/github/stars/{owner}/{p.repo}) |")
    lines += ["", MARK_END]
    return "\n".join(lines)


def update_profile(text: str, section: str, owner: str) -> tuple[str, list[str]]:
    start, end = text.find(MARK_START), text.find(MARK_END)
    if start == -1 or end == -1:
        raise SyncError(f"the README has no {MARK_START} … {MARK_END} markers; add them once around the "
                        "project list")
    if end < start or text.count(MARK_START) > 1 or text.count(MARK_END) > 1:
        raise SyncError("project markers are duplicated or in the wrong order")
    block = text[start:end + len(MARK_END)]
    pattern = re.compile(rf"github\.com/{re.escape(owner)}/([A-Za-z0-9_.-]+?)(?=[)/?#\s]|$)", re.IGNORECASE)
    old = {m.lower() for m in pattern.findall(block)}
    new = {m.lower() for m in pattern.findall(section)}
    changes = []
    if new - old:
        changes.append("added: " + ", ".join(sorted(new - old)))
    if old - new:
        changes.append("removed: " + ", ".join(sorted(old - new)))
    if block != section and not changes:
        changes.append("updated names, descriptions, languages, licences or order")
    return text[:start] + section + text[end + len(MARK_END):], changes


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sync_projects.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--profile", required=True, help="README to update")
    p.add_argument("--owner", required=True, help="GitHub account whose public repositories are listed")
    p.add_argument("--format", choices=("list", "table"), default="list")
    p.add_argument("--exclude", default="", help="comma-separated repository names to leave out")
    p.add_argument("--write", action="store_true", help="write changes (default: dry run)")
    p.add_argument("--pr-body", default="", help="write a pull request description here")
    p.add_argument("--github-output", default="")
    p.add_argument("--summary-file", default="")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--tool-version", action="version", version=f"profile-consistency {__version__}")
    return p.parse_args(argv)


def _emit(path: str, text: str) -> None:
    print(text)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def _outputs(path: str, **values: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None, api: ApiGet | None = None) -> int:
    args = parse_args(argv)
    api = api or make_api_get(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"),
                              args.timeout, args.retries)
    profile = Path(args.profile)
    if not profile.is_file():
        _emit(args.summary_file, f"## Projects sync: ❌ profile `{args.profile}` not found")
        _outputs(args.github_output, changed="false", unavailable="false", error="true")
        return 2
    exclude = {e.strip() for e in args.exclude.split(",") if e.strip()}
    try:
        projects = collect_projects(args.owner, api, exclude)
    except Unavailable as exc:
        _emit(args.summary_file, f"## Projects sync: ⚠️ GitHub data unavailable, nothing changed\n\n{exc}")
        _outputs(args.github_output, changed="false", unavailable="true", error="false")
        return 3
    except SyncError as exc:
        _emit(args.summary_file, f"## Projects sync: ❌ {exc}")
        _outputs(args.github_output, changed="false", unavailable="false", error="true")
        return 2

    original = profile.read_text(encoding="utf-8")
    try:
        updated, changes = update_profile(original, render(projects, args.owner, args.format), args.owner)
    except SyncError as exc:
        _emit(args.summary_file, f"## Projects sync: ❌ cannot update `{args.profile}`\n\n{exc}")
        _outputs(args.github_output, changed="false", unavailable="false", error="true")
        return 2
    changed = updated != original
    if changed and args.write:
        profile.write_text(updated, encoding="utf-8", newline="\n")
    status = ("✏️ profile updated" if args.write else "✏️ changes needed (dry run)") if changed else "✅ up to date"
    lines = [f"## Projects sync: {status}", "",
             f"**{len(projects)} published open-source projects** in the public repositories of `{args.owner}`: "
             + (", ".join(p.repo for p in projects) or "none"), ""]
    lines += [f"- {c}" for c in changes] if changed else ["No changes needed."]
    _emit(args.summary_file, "\n".join(lines))
    if args.pr_body and changed:
        body = [f"### Published open-source projects ({args.owner})", "",
                f"The list now has **{len(projects)} projects**, generated from the public repositories of "
                f"`{args.owner}` that are not forks or archived and have a licence and a published release.", ""]
        body += [f"- {c}" for c in changes]
        body += ["", "Names come from the repository name and descriptions from each repository's About text. "
                     "Private repositories are never listed."]
        Path(args.pr_body).write_text("\n".join(body) + "\n", encoding="utf-8")
    _outputs(args.github_output, changed=str(changed).lower(), unavailable="false", error="false",
             projects=str(len(projects)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
