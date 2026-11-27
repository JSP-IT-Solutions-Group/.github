#!/usr/bin/env python3
"""Release-readiness checker for JSP-IT Solutions Group open-source repositories.

The checker is strictly read-only. It inspects files below a project root and
reports PASS / WARN / FAIL / SKIP findings for:

* required and recommended project documents,
* version consistency across version-bearing files and the release tag,
* a changelog entry for the release version,
* optional binary checksum manifests (SHA256SUMS and similar).

It never modifies files, creates tags, publishes releases or talks to the
network. It depends on the Python standard library only (Python >= 3.11).

Exit codes: 0 = pass or warnings only, 1 = at least one failure,
2 = usage or configuration error (also reported as a failure).
"""

from __future__ import annotations

import sys

MIN_PYTHON = (3, 11)
if sys.version_info < MIN_PYTHON:  # pragma: no cover - guard for old runners
    sys.stderr.write(
        "release-readiness requires Python %d.%d or newer (found %s)\n"
        % (MIN_PYTHON[0], MIN_PYTHON[1], sys.version.split()[0])
    )
    sys.exit(2)

import argparse
import configparser
import copy
import datetime as dt
import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

__version__ = "1.0.0"

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
_SEVERITY = {SKIP: 0, PASS: 1, WARN: 2, FAIL: 3}
_LABEL = {
    PASS: "\u2705 PASS",
    WARN: "\u26a0\ufe0f WARN",
    FAIL: "\u274c FAIL",
    SKIP: "\u23ed\ufe0f SKIP",
}
CATEGORIES = ("Configuration", "Documents", "Version", "Changelog", "Checksums")


class ConfigError(Exception):
    """Raised for invalid configuration; reported as a FAIL finding."""


# --------------------------------------------------------------------------
# Findings and report
# --------------------------------------------------------------------------


@dataclass
class Finding:
    category: str
    check: str
    status: str
    message: str
    details: list[str] = field(default_factory=list)
    path: str | None = None


@dataclass
class Report:
    root: str
    findings: list[Finding] = field(default_factory=list)
    release_version: str | None = None
    version_origin: str | None = None
    profiles: list[str] = field(default_factory=list)
    project_type: str = "library"
    fail_on_warnings: bool = False

    def add(self, category: str, check: str, status: str, message: str,
            details: Iterable[str] | None = None, path: str | None = None) -> None:
        self.findings.append(
            Finding(category, check, status, message, list(details or []), path)
        )

    def count(self, status: str) -> int:
        return sum(1 for f in self.findings if f.status == status)

    @property
    def result(self) -> str:
        worst = max((_SEVERITY[f.status] for f in self.findings), default=_SEVERITY[PASS])
        if worst == _SEVERITY[FAIL]:
            return FAIL
        if worst == _SEVERITY[WARN]:
            return FAIL if self.fail_on_warnings else WARN
        return PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": "release-readiness",
            "tool_version": __version__,
            "result": self.result,
            "root": self.root,
            "release_version": self.release_version,
            "version_origin": self.version_origin,
            "profiles": self.profiles,
            "project_type": self.project_type,
            "fail_on_warnings": self.fail_on_warnings,
            "counts": {s: self.count(s) for s in (PASS, WARN, FAIL, SKIP)},
            "findings": [f.__dict__ for f in self.findings],
        }


# --------------------------------------------------------------------------
# Defaults, profiles and project types
# --------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "profile": "auto",
    "project_type": "library",
    "fail_on_warnings": False,
    "documents": {
        "required": ["README", "LICENSE", "CHANGELOG"],
        "recommended": ["SECURITY", "CONTRIBUTING", "CODE_OF_CONDUCT"],
        "search_paths": [".", ".github", "docs"],
        "stub_bytes": 64,
    },
    "version": {
        "expected": None,
        "tag_prefix": "v",
        "scheme": None,  # resolved from the profile when not set
        "auto_detect": True,
        "sources": [],
    },
    "changelog": {
        "path": None,
        "require_entry": True,
        "date": "warn",  # require | warn | ignore
        "warn_unreleased": True,
    },
    "checksums": {
        "mode": "auto",  # off | auto | required
        "artifacts_dir": "dist",
        "manifests": [],
        "algorithm": "auto",  # auto | sha256 | sha384 | sha512
        "unlisted": "warn",  # ignore | warn | fail
    },
}

# Each profile lists version sources that are used when present (auto-detected
# sources are optional) and marker files that identify the ecosystem.
PROFILES: dict[str, dict[str, Any]] = {
    "generic": {
        "markers": [],
        "scheme": "semver",
        "sources": [
            {"type": "plain", "path": "VERSION"},
            {"type": "plain", "path": "VERSION.txt"},
            {"type": "plain", "path": "version.txt"},
        ],
    },
    "python": {
        "markers": ["pyproject.toml", "setup.cfg", "setup.py"],
        "scheme": "pep440",
        "sources": [
            {"type": "pyproject", "path": "pyproject.toml"},
            {"type": "setup-cfg", "path": "setup.cfg"},
            {"type": "plain", "path": "VERSION"},
        ],
    },
    "node": {
        "markers": ["package.json"],
        "scheme": "semver",
        "sources": [
            {"type": "package-json", "path": "package.json"},
            {"type": "package-lock", "path": "package-lock.json"},
            {"type": "json", "path": "jsr.json"},
            {"type": "plain", "path": "VERSION"},
        ],
    },
    "dotnet": {
        "markers": ["*.sln", "*.slnx", "*.csproj", "*.fsproj", "Directory.Build.props",
                    "src/*/*.csproj"],
        "scheme": "semver",
        "sources": [
            {"type": "msbuild", "path": "Directory.Build.props"},
            {"type": "msbuild", "path": "*.csproj"},
            {"type": "msbuild", "path": "*.fsproj"},
            {"type": "msbuild", "path": "src/*/*.csproj"},
            {"type": "nuspec", "path": "*.nuspec"},
            {"type": "plain", "path": "VERSION"},
        ],
    },
    "rust": {
        "markers": ["Cargo.toml"],
        "scheme": "semver",
        "sources": [{"type": "cargo", "path": "Cargo.toml"}],
    },
    "php": {
        "markers": ["composer.json"],
        "scheme": "semver",
        "sources": [
            {"type": "composer", "path": "composer.json"},
            {"type": "plain", "path": "VERSION"},
        ],
    },
    "cpp": {
        "markers": ["CMakeLists.txt", "meson.build", "vcpkg.json", "conanfile.txt",
                    "conanfile.py"],
        "scheme": "semver",
        "sources": [
            {"type": "cmake", "path": "CMakeLists.txt"},
            {"type": "meson", "path": "meson.build"},
            {"type": "vcpkg", "path": "vcpkg.json"},
            {"type": "plain", "path": "VERSION"},
        ],
    },
}

PROJECT_TYPES: dict[str, dict[str, Any]] = {
    # Source-only libraries and packages: checksums are verified when present.
    "library": {},
    # Deployable applications and services: a SUPPORT document is recommended.
    "application": {
        "documents": {
            "recommended": ["SECURITY", "CONTRIBUTING", "CODE_OF_CONDUCT", "SUPPORT"],
        },
    },
    # Projects that ship binaries: a checksum manifest is mandatory and every
    # artifact in the artifacts directory must be listed in it.
    "binary": {
        "checksums": {"mode": "required", "unlisted": "fail"},
    },
}

DOCUMENT_ALIASES: dict[str, tuple[list[str], list[str] | None]] = {
    "README": (["README.md", "README", "README.markdown", "README.rst", "README.txt",
                "README.adoc"], None),
    "LICENSE": (["LICENSE", "LICENSE.md", "LICENSE.txt", "LICENSE.rst", "LICENCE",
                 "LICENCE.md", "LICENCE.txt", "COPYING", "COPYING.md", "COPYING.txt",
                 "UNLICENSE"], ["."]),
    "CHANGELOG": (["CHANGELOG.md", "CHANGELOG", "CHANGELOG.markdown", "CHANGELOG.txt",
                   "CHANGELOG.rst", "CHANGES.md", "HISTORY.md", "NEWS.md"], [".", "docs"]),
    "SECURITY": (["SECURITY.md", "SECURITY.rst", "SECURITY.txt", "SECURITY"], None),
    "CONTRIBUTING": (["CONTRIBUTING.md", "CONTRIBUTING.rst", "CONTRIBUTING.txt",
                      "CONTRIBUTING"], None),
    "CODE_OF_CONDUCT": (["CODE_OF_CONDUCT.md", "CODE_OF_CONDUCT.rst",
                         "CODE_OF_CONDUCT.txt", "CODE_OF_CONDUCT"], None),
    "SUPPORT": (["SUPPORT.md", "SUPPORT.rst", "SUPPORT.txt", "SUPPORT"], None),
    "NOTICE": (["NOTICE", "NOTICE.md", "NOTICE.txt"], ["."]),
    "AUTHORS": (["AUTHORS", "AUTHORS.md", "AUTHORS.txt"], ["."]),
}
# Community health files that GitHub can fall back to from the organization's
# .github repository. The fallback does not end up in source archives.
ORG_DEFAULT_DOCUMENTS = {"SECURITY", "CONTRIBUTING", "CODE_OF_CONDUCT", "SUPPORT"}

LICENSE_PLACEHOLDERS = re.compile(
    r"\[(?:year|yyyy|fullname|name of copyright owner|copyright holders?)\]"
    r"|<(?:year|copyright holders?|name of author|owner)>"
    r"|\{\{\s*(?:year|author|owner)\s*\}\}",
    re.IGNORECASE,
)

VERSION_SCHEMES: dict[str, re.Pattern[str]] = {
    "semver": re.compile(
        r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
        r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
        r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
    ),
    # Canonical public PEP 440 versions (what may be uploaded to PyPI).
    "pep440": re.compile(
        r"^([1-9][0-9]*!)?(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*"
        r"((a|b|rc)(0|[1-9][0-9]*))?(\.post(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?$"
    ),
    "calver": re.compile(
        r"^(\d{4}|\d{2})\.(0?[1-9]|1[0-2])(\.(0|[1-9]\d*))?(\.(0|[1-9]\d*))?"
        r"([.-][0-9A-Za-z][0-9A-Za-z.-]*)?$"
    ),
    "any": re.compile(r"^[^\s]{1,128}$"),
}

CHANGELOG_DATE_POLICIES = ("require", "warn", "ignore")
CHECKSUM_MODES = ("off", "auto", "required")
UNLISTED_POLICIES = ("ignore", "warn", "fail")
HASH_BY_LENGTH = {32: "md5", 40: "sha1", 56: "sha224", 64: "sha256", 96: "sha384",
                  128: "sha512"}
WEAK_HASHES = {"md5", "sha1"}
ACCEPTED_ALGORITHMS = ("auto", "sha256", "sha384", "sha512")
MANIFEST_NAMES = ["SHA256SUMS", "SHA256SUMS.txt", "SHA512SUMS", "SHA512SUMS.txt",
                  "sha256sums.txt", "sha512sums.txt", "checksums.txt", "CHECKSUMS",
                  "CHECKSUMS.txt", "checksums.sha256"]
ROOT_MANIFEST_NAMES = ["SHA256SUMS", "SHA256SUMS.txt", "SHA512SUMS", "SHA512SUMS.txt"]
SIDECAR_SUFFIXES = (".sha256", ".sha384", ".sha512")
SIGNATURE_SUFFIXES = (".asc", ".sig", ".minisig", ".sigstore", ".sigstore.json", ".pem",
                      ".crt", ".intoto.jsonl")

SOURCE_TYPES = ("plain", "json", "toml", "regex", "package-json", "package-lock",
                "composer", "vcpkg", "pyproject", "setup-cfg", "cargo", "msbuild",
                "nuspec", "cmake", "meson")


# --------------------------------------------------------------------------
# Configuration loading and validation
# --------------------------------------------------------------------------

_ALLOWED_KEYS: dict[str | None, set[str]] = {
    None: {"$schema", "profile", "project_type", "fail_on_warnings", "documents",
           "version", "changelog", "checksums"},
    "documents": {"required", "recommended", "search_paths", "stub_bytes"},
    "version": {"expected", "tag_prefix", "scheme", "auto_detect", "sources"},
    "changelog": {"path", "require_entry", "date", "warn_unreleased"},
    "checksums": {"mode", "artifacts_dir", "manifests", "algorithm", "unlisted"},
}
_SOURCE_KEYS = {"type", "path", "key", "pattern", "required"}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _expect(cond: bool, message: str) -> None:
    if not cond:
        raise ConfigError(message)


def _str_list(value: Any, name: str) -> list[str]:
    _expect(isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value),
            f"{name} must be a list of non-empty strings")
    return [v.strip() for v in value]


def safe_relative(path_text: str, what: str) -> str:
    """Validate that a configured path stays inside the project root."""
    _expect(isinstance(path_text, str) and path_text.strip() != "", f"{what} must be a non-empty string")
    text = path_text.strip().replace("\\", "/")
    _expect(not PurePosixPath(text).is_absolute() and not PureWindowsPath(path_text).is_absolute()
            and not PureWindowsPath(path_text).drive,
            f"{what} must be a relative path inside the project: {path_text!r}")
    _expect(".." not in PurePosixPath(text).parts,
            f"{what} must not contain '..': {path_text!r}")
    return text


def validate_user_config(cfg: dict[str, Any]) -> None:
    """Strictly validate a user-supplied configuration file (typos are errors)."""
    _expect(isinstance(cfg, dict), "configuration must be a JSON object")
    for key in cfg:
        _expect(key in _ALLOWED_KEYS[None], f"unknown configuration key: {key!r}")
    for section in ("documents", "version", "changelog", "checksums"):
        if section in cfg:
            _expect(isinstance(cfg[section], dict), f"{section} must be an object")
            for key in cfg[section]:
                _expect(key in _ALLOWED_KEYS[section],
                        f"unknown configuration key: {section}.{key!r}")
    for src in cfg.get("version", {}).get("sources", []) or []:
        _expect(isinstance(src, dict), "version.sources entries must be objects")
        for key in src:
            _expect(key in _SOURCE_KEYS, f"unknown key in version.sources entry: {key!r}")


def validate_effective_config(cfg: dict[str, Any]) -> None:
    _expect(cfg["profile"] in PROFILES or cfg["profile"] == "auto",
            f"unknown profile {cfg['profile']!r}; expected auto or one of "
            + ", ".join(sorted(PROFILES)))
    _expect(cfg["project_type"] in PROJECT_TYPES,
            f"unknown project_type {cfg['project_type']!r}; expected one of "
            + ", ".join(PROJECT_TYPES))
    _expect(isinstance(cfg["fail_on_warnings"], bool), "fail_on_warnings must be a boolean")

    docs = cfg["documents"]
    docs["required"] = _str_list(docs["required"], "documents.required")
    docs["recommended"] = _str_list(docs["recommended"], "documents.recommended")
    docs["search_paths"] = [safe_relative(p, "documents.search_paths entry")
                            for p in _str_list(docs["search_paths"], "documents.search_paths")]
    _expect(isinstance(docs["stub_bytes"], int) and not isinstance(docs["stub_bytes"], bool)
            and docs["stub_bytes"] >= 0, "documents.stub_bytes must be a non-negative integer")
    for name in docs["required"] + docs["recommended"]:
        if name.upper() not in DOCUMENT_ALIASES:
            safe_relative(name, "document path")

    ver = cfg["version"]
    _expect(ver["expected"] is None or (isinstance(ver["expected"], str) and ver["expected"].strip()),
            "version.expected must be a non-empty string or null")
    _expect(isinstance(ver["tag_prefix"], str), "version.tag_prefix must be a string")
    _expect(ver["scheme"] is None or ver["scheme"] in VERSION_SCHEMES,
            "version.scheme must be one of " + ", ".join(VERSION_SCHEMES))
    _expect(isinstance(ver["auto_detect"], bool), "version.auto_detect must be a boolean")
    _expect(isinstance(ver["sources"], list), "version.sources must be a list")
    for src in ver["sources"]:
        _expect(isinstance(src, dict), "version.sources entries must be objects")
        _expect(src.get("type") in SOURCE_TYPES,
                f"version.sources type must be one of {', '.join(SOURCE_TYPES)}: {src.get('type')!r}")
        src["path"] = safe_relative(src.get("path", ""), "version.sources path")
        if src["type"] == "regex":
            pattern = src.get("pattern")
            _expect(isinstance(pattern, str) and pattern, "regex sources need a 'pattern'")
            try:
                compiled = re.compile(pattern, re.MULTILINE)
            except re.error as exc:
                raise ConfigError(f"invalid regex pattern {pattern!r}: {exc}") from exc
            _expect(compiled.groups >= 1,
                    f"regex pattern needs a capture group (named 'version' or the first group): {pattern!r}")
        if "key" in src:
            _expect(isinstance(src["key"], str) and src["key"], "source 'key' must be a non-empty string")
        if "required" in src:
            _expect(isinstance(src["required"], bool), "source 'required' must be a boolean")

    chg = cfg["changelog"]
    if chg["path"] is not None:
        chg["path"] = safe_relative(chg["path"], "changelog.path")
    _expect(isinstance(chg["require_entry"], bool), "changelog.require_entry must be a boolean")
    _expect(chg["date"] in CHANGELOG_DATE_POLICIES,
            "changelog.date must be one of " + ", ".join(CHANGELOG_DATE_POLICIES))
    _expect(isinstance(chg["warn_unreleased"], bool), "changelog.warn_unreleased must be a boolean")

    chk = cfg["checksums"]
    _expect(chk["mode"] in CHECKSUM_MODES, "checksums.mode must be one of " + ", ".join(CHECKSUM_MODES))
    _expect(chk["algorithm"] in ACCEPTED_ALGORITHMS,
            "checksums.algorithm must be one of " + ", ".join(ACCEPTED_ALGORITHMS))
    _expect(chk["unlisted"] in UNLISTED_POLICIES,
            "checksums.unlisted must be one of " + ", ".join(UNLISTED_POLICIES))
    _expect(isinstance(chk["artifacts_dir"], str) and chk["artifacts_dir"].strip(),
            "checksums.artifacts_dir must be a non-empty string")
    chk["manifests"] = _str_list(chk["manifests"], "checksums.manifests")


def split_list(text: str) -> list[str]:
    """Parse a comma/newline separated list. 'none' yields an empty list."""
    if text.strip().lower() in ("none", "-"):
        return []
    return [part.strip() for part in re.split(r"[,\n]", text) if part.strip()]


def build_config(root: Path, args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Merge defaults < profile/project type presets < config file < CLI inputs."""
    file_cfg: dict[str, Any] = {}
    config_label: str | None = None
    if args.config:
        rel = safe_relative(args.config, "config path")
        config_path = root / rel
        _expect(config_path.is_file(), f"configuration file not found: {rel}")
    else:
        rel = ".github/release-readiness.json"
        config_path = root / rel
    if config_path.is_file():
        config_label = rel
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot parse {rel}: {exc}") from exc
        validate_user_config(file_cfg)
        file_cfg.pop("$schema", None)

    cli: dict[str, Any] = {"documents": {}, "version": {}, "changelog": {}, "checksums": {}}
    if args.profile:
        cli["profile"] = args.profile.strip()
    if args.project_type:
        cli["project_type"] = args.project_type.strip()
    if args.fail_on_warnings:
        cli["fail_on_warnings"] = True
    if args.required_documents:
        cli["documents"]["required"] = split_list(args.required_documents)
    if args.recommended_documents:
        cli["documents"]["recommended"] = split_list(args.recommended_documents)
    if args.version:
        cli["version"]["expected"] = args.version.strip()
    if args.tag_prefix is not None:
        cli["version"]["tag_prefix"] = "" if args.tag_prefix.strip().lower() == "none" else args.tag_prefix
    if args.version_scheme:
        cli["version"]["scheme"] = args.version_scheme.strip()
    if args.changelog:
        cli["changelog"]["path"] = args.changelog.strip()
    if args.checksums:
        cli["checksums"]["mode"] = args.checksums.strip()
    if args.checksum_manifest:
        cli["checksums"]["manifests"] = split_list(args.checksum_manifest)
    cli = {k: v for k, v in cli.items() if v != {}}

    profile = cli.get("profile", file_cfg.get("profile", DEFAULT_CONFIG["profile"]))
    project_type = cli.get("project_type", file_cfg.get("project_type", DEFAULT_CONFIG["project_type"]))
    _expect(isinstance(project_type, str) and project_type in PROJECT_TYPES,
            f"unknown project_type {project_type!r}; expected one of " + ", ".join(PROJECT_TYPES))

    cfg = deep_merge(DEFAULT_CONFIG, PROJECT_TYPES[project_type])
    cfg = deep_merge(cfg, file_cfg)
    cfg = deep_merge(cfg, cli)
    cfg["profile"] = profile
    cfg["project_type"] = project_type
    validate_effective_config(cfg)
    return cfg, config_label


# --------------------------------------------------------------------------
# Filesystem helpers
# --------------------------------------------------------------------------


def find_case_insensitive(directory: Path, name: str) -> Path | None:
    if not directory.is_dir():
        return None
    exact = directory / name
    if exact.is_file():
        return exact
    lowered = name.lower()
    try:
        for entry in sorted(directory.iterdir()):
            if entry.name.lower() == lowered and entry.is_file():
                return entry
    except OSError:
        return None
    return None


def rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def is_within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def expand_glob(root: Path, pattern: str) -> list[Path]:
    if any(ch in pattern for ch in "*?["):
        return sorted(p for p in root.glob(pattern) if p.is_file() and is_within(p, root))
    candidate = root / pattern
    return [candidate] if candidate.is_file() and is_within(candidate, root) else []


def human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num} B"  # pragma: no cover


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


def locate_document(root: Path, name: str, search_paths: list[str]) -> tuple[Path | None, list[str]]:
    key = name.upper()
    if key in DOCUMENT_ALIASES:
        candidates, locations = DOCUMENT_ALIASES[key]
        locations = locations or search_paths
        tried = []
        for loc in locations:
            directory = root if loc in (".", "") else root / loc
            for candidate in candidates:
                tried.append(candidate if loc in (".", "") else f"{loc}/{candidate}")
                found = find_case_insensitive(directory, candidate)
                if found is not None and is_within(found, root):
                    return found, tried
        return None, tried
    path = root / name
    return (path if path.is_file() and is_within(path, root) else None), [name]


def check_documents(root: Path, cfg: dict[str, Any], report: Report) -> None:
    docs = cfg["documents"]
    seen: set[str] = set()
    for level, names in (("required", docs["required"]), ("recommended", docs["recommended"])):
        missing_status = FAIL if level == "required" else WARN
        for name in names:
            if name.upper() in seen:
                continue
            seen.add(name.upper())
            label = name.upper() if name.upper() in DOCUMENT_ALIASES else name
            path, tried = locate_document(root, name, docs["search_paths"])
            if path is None:
                details = ["Looked for: " + ", ".join(tried[:12]) + (" ..." if len(tried) > 12 else "")]
                if name.upper() in ORG_DEFAULT_DOCUMENTS:
                    details.append("GitHub may show an organization-wide default from the "
                                   ".github repository, but that file is not part of this "
                                   "project's source archive or packages.")
                report.add("Documents", label, missing_status,
                           f"{level.capitalize()} document is missing", details)
                continue
            shown = rel(root, path)
            content = read_text(path)
            meaningful = content.strip()
            if not meaningful:
                report.add("Documents", label, FAIL if level == "required" else WARN,
                           f"`{shown}` is empty", path=shown)
                continue
            if len(meaningful.encode("utf-8")) < docs["stub_bytes"]:
                report.add("Documents", label, WARN,
                           f"`{shown}` looks like a stub ({len(meaningful)} characters)", path=shown)
                continue
            if label == "LICENSE":
                placeholder = LICENSE_PLACEHOLDERS.search(content)
                if placeholder:
                    report.add("Documents", label, WARN,
                               f"`{shown}` still contains the template placeholder "
                               f"`{placeholder.group(0)}`", path=shown)
                    continue
            report.add("Documents", label, PASS,
                       f"Found `{shown}` ({human_size(path.stat().st_size)})", path=shown)


# --------------------------------------------------------------------------
# Version sources
# --------------------------------------------------------------------------


@dataclass
class SourceValue:
    label: str
    value: str | None
    note: str | None = None
    path: str | None = None


def _dig(data: Any, dotted: str) -> Any:
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _as_version(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _load_json(path: Path) -> Any:
    return json.loads(read_text(path))


def _load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(read_text(path))


_MSBUILD_RE = {
    tag: re.compile(rf"<{tag}(?:\s[^>]*)?>\s*([^<]+?)\s*</{tag}>", re.IGNORECASE)
    for tag in ("Version", "VersionPrefix", "VersionSuffix", "PackageVersion")
}


def extract_source(path: Path, spec: dict[str, Any], shown: str) -> list[SourceValue]:
    stype = spec["type"]
    if stype == "plain":
        for line in read_text(path).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return [SourceValue(shown, line)]
        return [SourceValue(shown, None, "file contains no version")]
    if stype in ("json", "package-json", "composer"):
        key = spec.get("key", "version")
        value = _dig(_load_json(path), key)
        if value is None:
            return [SourceValue(f"{shown} ({key})", None, f"no `{key}` field")]
        return [SourceValue(f"{shown} ({key})", _as_version(value) or str(value))]
    if stype == "package-lock":
        data = _load_json(path)
        values = [SourceValue(f"{shown} (version)", _as_version(data.get("version")),
                              None if data.get("version") else "no `version` field")]
        root_pkg = _dig(data, "packages")
        if isinstance(root_pkg, dict) and isinstance(root_pkg.get(""), dict) and "version" in root_pkg[""]:
            values.append(SourceValue(f'{shown} (packages[""].version)',
                                      _as_version(root_pkg[""]["version"])))
        return values
    if stype == "vcpkg":
        data = _load_json(path)
        for key in ("version", "version-semver", "version-string", "version-date"):
            if key in data:
                return [SourceValue(f"{shown} ({key})", _as_version(data[key]))]
        return [SourceValue(shown, None, "no version field")]
    if stype == "toml":
        key = spec.get("key", "version")
        value = _dig(_load_toml(path), key)
        if value is None:
            return [SourceValue(f"{shown} ({key})", None, f"no `{key}` key")]
        return [SourceValue(f"{shown} ({key})", _as_version(value) or str(value))]
    if stype == "pyproject":
        data = _load_toml(path)
        project = data.get("project", {})
        if "version" in project:
            return [SourceValue(f"{shown} (project.version)", _as_version(project["version"]))]
        if "version" in project.get("dynamic", []):
            return [SourceValue(f"{shown} (project.version)", None,
                                "version is dynamic (computed at build time)")]
        poetry = _dig(data, "tool.poetry.version")
        if poetry is not None:
            return [SourceValue(f"{shown} (tool.poetry.version)", _as_version(poetry))]
        return [SourceValue(shown, None, "no static version declared")]
    if stype == "setup-cfg":
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(read_text(path))
        value = parser.get("metadata", "version", fallback=None)
        if value is None:
            return [SourceValue(f"{shown} (metadata.version)", None, "no version declared")]
        if value.strip().startswith(("attr:", "file:")):
            return [SourceValue(f"{shown} (metadata.version)", None,
                                f"version is indirect (`{value.strip()}`)")]
        return [SourceValue(f"{shown} (metadata.version)", value.strip())]
    if stype == "cargo":
        data = _load_toml(path)
        version = _dig(data, "package.version")
        if isinstance(version, dict) and version.get("workspace") is True:
            ws = _dig(data, "workspace.package.version")
            if ws is not None:
                return [SourceValue(f"{shown} (workspace.package.version)", _as_version(ws))]
            return [SourceValue(f"{shown} (package.version)", None, "inherited from the workspace")]
        if version is not None:
            return [SourceValue(f"{shown} (package.version)", _as_version(version))]
        ws = _dig(data, "workspace.package.version")
        if ws is not None:
            return [SourceValue(f"{shown} (workspace.package.version)", _as_version(ws))]
        return [SourceValue(shown, None, "no version declared")]
    if stype == "msbuild":
        text = read_text(path)
        found = {tag: rx.search(text) for tag, rx in _MSBUILD_RE.items()}
        values: list[SourceValue] = []
        for tag in ("Version", "PackageVersion"):
            if found[tag]:
                values.append(SourceValue(f"{shown} (<{tag}>)", found[tag].group(1)))
        if not found["Version"] and found["VersionPrefix"]:
            value = found["VersionPrefix"].group(1)
            if found["VersionSuffix"]:
                value = f"{value}-{found['VersionSuffix'].group(1)}"
            values.append(SourceValue(f"{shown} (<VersionPrefix>)", value))
        for sv in values:
            if sv.value and "$(" in sv.value:
                sv.note, sv.value = f"computed by MSBuild (`{sv.value}`)", None
        return values or [SourceValue(shown, None, "no <Version> property")]
    if stype == "nuspec":
        match = re.search(r"<version>\s*([^<]+?)\s*</version>", read_text(path), re.IGNORECASE)
        if not match:
            return [SourceValue(shown, None, "no <version> element")]
        if "$" in match.group(1):
            return [SourceValue(shown, None, f"token `{match.group(1)}` is replaced at pack time")]
        return [SourceValue(f"{shown} (<version>)", match.group(1))]
    if stype == "cmake":
        match = re.search(r"\bproject\s*\(([^)]*)\)", read_text(path), re.IGNORECASE | re.DOTALL)
        if not match:
            return [SourceValue(shown, None, "no project() call")]
        ver = re.search(r"\bVERSION\s+\"?([^\s\")]+)", match.group(1))
        if not ver:
            return [SourceValue(shown, None, "project() declares no VERSION")]
        if "${" in ver.group(1):
            return [SourceValue(shown, None, f"VERSION is a variable (`{ver.group(1)}`)")]
        return [SourceValue(f"{shown} (project VERSION)", ver.group(1))]
    if stype == "meson":
        match = re.search(r"\bproject\s*\((.*?)\)\s*$", read_text(path), re.DOTALL | re.MULTILINE)
        ver = re.search(r"\bversion\s*:\s*'([^']+)'", match.group(1)) if match else None
        if not ver:
            return [SourceValue(shown, None, "project() declares no version")]
        return [SourceValue(f"{shown} (project version)", ver.group(1))]
    if stype == "regex":
        compiled = re.compile(spec["pattern"], re.MULTILINE)
        match = compiled.search(read_text(path))
        if not match:
            return [SourceValue(shown, None, "pattern did not match")]
        value = match.groupdict().get("version") or match.group(1)
        return [SourceValue(f"{shown} (pattern)", _as_version(value))]
    raise ConfigError(f"unsupported source type {stype!r}")  # pragma: no cover


def detect_profiles(root: Path, configured: str) -> list[str]:
    if configured != "auto":
        return [configured]
    detected = [name for name, prof in PROFILES.items()
                if any(expand_glob(root, marker) for marker in prof["markers"])]
    return detected or ["generic"]


def collect_sources(root: Path, cfg: dict[str, Any], profiles: list[str]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for src in cfg["version"]["sources"]:
        specs.append({**src, "required": src.get("required", True)})
    if cfg["version"]["auto_detect"]:
        names = profiles if "generic" in profiles else profiles + ["generic"]
        for name in names:
            for src in PROFILES[name]["sources"]:
                specs.append({**src, "required": False})
    # De-duplicate on (type, path, key, pattern) keeping the first (explicit wins).
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for spec in specs:
        key = (spec["type"], spec["path"], spec.get("key"), spec.get("pattern"))
        unique.setdefault(key, spec)
    return list(unique.values())


def resolve_expected_version(cfg: dict[str, Any], args: argparse.Namespace,
                             report: Report) -> tuple[str | None, str | None]:
    prefix = cfg["version"]["tag_prefix"]
    explicit = cfg["version"]["expected"]
    if explicit:
        value = explicit[len(prefix):] if prefix and explicit.startswith(prefix) else explicit
        return value, f"input `{explicit}`"
    if (args.ref_type or "").strip() == "tag" and args.ref_name:
        tag = args.ref_name.strip()
        if prefix and not tag.startswith(prefix):
            report.add("Version", "Release tag", FAIL,
                       f"Tag `{tag}` does not start with the configured prefix `{prefix}`",
                       ["Set `tag-prefix` (or version.tag_prefix) to match your tagging scheme."])
            return None, None
        return tag[len(prefix):], f"tag `{tag}`"
    return None, None


def check_version(root: Path, cfg: dict[str, Any], args: argparse.Namespace,
                  report: Report) -> str | None:
    profiles = detect_profiles(root, cfg["profile"])
    report.profiles = profiles
    scheme = cfg["version"]["scheme"]
    if scheme is None:
        scheme = PROFILES[profiles[0]]["scheme"] if len(profiles) == 1 else "semver"
    pattern = VERSION_SCHEMES[scheme]

    expected, origin = resolve_expected_version(cfg, args, report)

    values: list[SourceValue] = []
    for spec in collect_sources(root, cfg, profiles):
        matches = expand_glob(root, spec["path"])
        if not matches:
            if spec["required"]:
                report.add("Version", spec["path"], FAIL, "Configured version source not found",
                           path=spec["path"])
            continue
        for path in matches:
            shown = rel(root, path)
            try:
                extracted = extract_source(path, spec, shown)
            except (json.JSONDecodeError, tomllib.TOMLDecodeError, configparser.Error,
                    UnicodeDecodeError, AttributeError, TypeError) as exc:
                report.add("Version", shown, FAIL, f"Cannot parse `{shown}`: {exc}", path=shown)
                continue
            for sv in extracted:
                sv.path = shown
                if sv.value is None:
                    if spec["required"]:
                        report.add("Version", sv.label, FAIL,
                                   f"No version found ({sv.note})", path=shown)
                    else:
                        report.add("Version", sv.label, SKIP,
                                   f"Ignored: {sv.note}", path=shown)
                    continue
                values.append(sv)

    distinct = sorted({sv.value for sv in values if sv.value})
    release = expected
    if expected is None and len(distinct) == 1:
        release, origin = distinct[0], "version files"

    if release is not None:
        report.release_version, report.version_origin = release, origin
        if pattern.match(release):
            report.add("Version", "Release version", PASS,
                       f"`{release}` from {origin} is a valid {scheme} version")
        else:
            report.add("Version", "Release version", FAIL,
                       f"`{release}` from {origin} is not a valid {scheme} version",
                       ["Change version.scheme / version-scheme if the project uses another scheme."])
    elif not any(f.check == "Release tag" for f in report.findings if f.category == "Version"):
        report.add("Version", "Release version", SKIP,
                   "No release tag or explicit version given; checking that version files agree")

    if not values:
        report.add("Version", "Version sources", WARN,
                   "No version-bearing files found for profile(s): " + ", ".join(profiles),
                   ["Add a VERSION file or configure version.sources if the version lives elsewhere."])
        return release

    if expected is None and len(distinct) > 1:
        report.add("Version", "Consistency", FAIL,
                   "Version files disagree: " + ", ".join(f"`{v}`" for v in distinct),
                   [f"{sv.label}: `{sv.value}`" for sv in values])
        return None

    for sv in values:
        if release is not None and sv.value != release:
            report.add("Version", sv.label, FAIL,
                       f"Declares `{sv.value}` but the release version is `{release}`", path=sv.path)
        else:
            report.add("Version", sv.label, PASS, f"Declares `{sv.value}`", path=sv.path)
    return release


# --------------------------------------------------------------------------
# Changelog
# --------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_LINK_REF_RE = re.compile(r"^\s*\[[^\]]+\]:\s*\S+")
_UNRELEASED_RE = re.compile(r"^\[?unreleased\]?(?=$|[\s:(\]-])", re.IGNORECASE)


@dataclass
class Section:
    level: int
    title: str
    line: int
    body: list[str]


def parse_sections(text: str) -> list[Section]:
    """Return every Markdown heading with its body.

    A heading's body runs until the next heading of the same or a higher level,
    so "## [1.2.0]" includes its "### Added" subsections but not "## [1.1.0]".
    HTML comments are removed first (line numbers are kept stable) and headings
    inside fenced code blocks are ignored.
    """
    cleaned = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)
    lines = cleaned.splitlines()
    headings: list[tuple[int, int, str]] = []  # (index, level, title)
    in_fence = False
    for index, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else _HEADING_RE.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    sections: list[Section] = []
    for pos, (index, level, title) in enumerate(headings):
        end = len(lines)
        for later_index, later_level, _ in headings[pos + 1:]:
            if later_level <= level:
                end = later_index
                break
        sections.append(Section(level, title, index + 1, lines[index + 1:end]))
    return sections


def heading_matches(title: str, version: str) -> bool:
    text = re.sub(r"^(?:version|release)\s+", "", title.strip(), flags=re.IGNORECASE)
    esc = re.escape(version)
    return re.match(rf"^(?:\[v?{esc}\]|v?{esc}(?=$|[\s:,(]))", text, re.IGNORECASE) is not None


def meaningful_lines(body: list[str]) -> list[str]:
    return [ln for ln in body
            if ln.strip() and not _HEADING_RE.match(ln) and not _LINK_REF_RE.match(ln)
            and not re.fullmatch(r"\s*([-*_])\s*(\1\s*){2,}", ln)]


def check_changelog(root: Path, cfg: dict[str, Any], version: str | None, report: Report) -> None:
    chg = cfg["changelog"]
    if chg["path"]:
        path = root / chg["path"]
        path = path if path.is_file() else None
        shown_missing = chg["path"]
    else:
        path, _ = locate_document(root, "CHANGELOG", cfg["documents"]["search_paths"])
        shown_missing = "CHANGELOG.md"
    if path is None:
        status = FAIL if chg["require_entry"] else SKIP
        report.add("Changelog", "Changelog file", status,
                   f"No changelog found (expected `{shown_missing}`)")
        return
    shown = rel(root, path)
    sections = parse_sections(read_text(path))
    if not sections:
        report.add("Changelog", "Changelog file", WARN if chg["require_entry"] else SKIP,
                   f"`{shown}` has no Markdown headings; only Markdown changelogs can be checked",
                   path=shown)
        return

    # Only meaningful when checking an actual release (tag or explicit version);
    # on ordinary pull requests a populated Unreleased section is expected.
    releasing = report.version_origin is not None and report.version_origin != "version files"
    if chg["warn_unreleased"] and releasing:
        for sec in sections:
            if _UNRELEASED_RE.match(sec.title) and meaningful_lines(sec.body):
                report.add("Changelog", "Unreleased section", WARN,
                           f"`{shown}` line {sec.line}: the Unreleased section still has entries",
                           ["Move them under the release heading before tagging."], path=shown)
                break

    if version is None:
        report.add("Changelog", "Release entry", WARN if chg["require_entry"] else SKIP,
                   "Release version is unknown, so the changelog entry cannot be verified")
        return

    matches = [sec for sec in sections if heading_matches(sec.title, version)]
    missing_status = FAIL if chg["require_entry"] else WARN
    if not matches:
        recent = [sec.title for sec in sections
                  if re.search(r"\d", sec.title) and not _UNRELEASED_RE.match(sec.title)][:3]
        report.add("Changelog", "Release entry", missing_status,
                   f"`{shown}` has no entry for `{version}`",
                   ["Most recent headings: " + "; ".join(f"`{t}`" for t in recent)] if recent else [],
                   path=shown)
        return
    if len(matches) > 1:
        report.add("Changelog", "Release entry", FAIL,
                   f"`{shown}` has {len(matches)} entries for `{version}` (lines "
                   + ", ".join(str(m.line) for m in matches) + ")", path=shown)
        return
    sec = matches[0]
    if not meaningful_lines(sec.body):
        report.add("Changelog", "Release entry", missing_status,
                   f"`{shown}` line {sec.line}: the entry for `{version}` is empty", path=shown)
        return
    report.add("Changelog", "Release entry", PASS,
               f"`{shown}` line {sec.line}: entry for `{version}` "
               f"({len(meaningful_lines(sec.body))} line(s))", path=shown)

    if chg["date"] == "ignore":
        return
    date_match = _DATE_RE.search(sec.title)
    if not date_match:
        report.add("Changelog", "Release date", FAIL if chg["date"] == "require" else WARN,
                   f"Heading `{sec.title}` has no release date (YYYY-MM-DD)", path=shown)
        return
    try:
        released = dt.date(*(int(g) for g in date_match.groups()))
    except ValueError:
        report.add("Changelog", "Release date", FAIL,
                   f"`{date_match.group(0)}` is not a valid calendar date", path=shown)
        return
    today = dt.datetime.now(dt.timezone.utc).date()
    if released > today + dt.timedelta(days=1):
        report.add("Changelog", "Release date", WARN,
                   f"Release date `{released}` is in the future", path=shown)
    else:
        report.add("Changelog", "Release date", PASS, f"Dated `{released}`", path=shown)


# --------------------------------------------------------------------------
# Checksums
# --------------------------------------------------------------------------

_GNU_LINE = re.compile(r"^(?P<hash>[0-9a-fA-F]{32,128})[ \t]+\*?(?P<path>.+?)\s*$")
_BSD_LINE = re.compile(r"^(?P<algo>SHA256|SHA384|SHA512|SHA224|SHA1|MD5)\s?\((?P<path>.+)\)\s?=\s?"
                       r"(?P<hash>[0-9a-fA-F]+)\s*$", re.IGNORECASE)
_HASH_ONLY = re.compile(r"^(?P<hash>[0-9a-fA-F]{32,128})\s*$")


def parse_manifest(path: Path) -> tuple[list[tuple[int, str, str, str | None]], list[str]]:
    """Return ([(line, hash, entry_path, declared_algo)], [errors])."""
    entries: list[tuple[int, str, str, str | None]] = []
    errors: list[str] = []
    sidecar = path.name.lower().endswith(SIDECAR_SUFFIXES)
    for number, raw in enumerate(read_text(path).splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("\\"):  # GNU coreutils escaped file name
            line = line[1:]
            gnu = _GNU_LINE.match(line)
            if gnu:
                name = gnu.group("path").replace("\\n", "\n").replace("\\\\", "\\")
                entries.append((number, gnu.group("hash").lower(), name, None))
                continue
        bsd = _BSD_LINE.match(line)
        if bsd:
            entries.append((number, bsd.group("hash").lower(), bsd.group("path"),
                            bsd.group("algo").lower()))
            continue
        gnu = _GNU_LINE.match(line)
        if gnu:
            entries.append((number, gnu.group("hash").lower(), gnu.group("path"), None))
            continue
        hash_only = _HASH_ONLY.match(line) if sidecar else None
        if hash_only:
            target = path.name[: path.name.lower().rfind(".")]
            entries.append((number, hash_only.group("hash").lower(), target, None))
            continue
        errors.append(f"line {number}: unrecognised format")
    return entries, errors


def file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unsafe_entry(entry: str) -> bool:
    if PurePosixPath(entry).is_absolute() or PureWindowsPath(entry).is_absolute() \
            or PureWindowsPath(entry).drive:
        return True
    return ".." in re.split(r"[\\/]", entry)


def discover_manifests(root: Path, artifacts: Path, cfg: dict[str, Any],
                       report: Report) -> list[Path]:
    chk = cfg["checksums"]
    if chk["manifests"]:
        found = []
        for item in chk["manifests"]:
            manifest_rel = safe_relative(item, "checksum manifest path")
            matches = expand_glob(root, manifest_rel)
            if not matches:
                report.add("Checksums", manifest_rel, FAIL, "Configured checksum manifest not found",
                           path=manifest_rel)
            found.extend(matches)
        return found
    found = []
    if artifacts.is_dir():
        for name in MANIFEST_NAMES:
            hit = find_case_insensitive(artifacts, name)
            if hit and hit not in found and is_within(hit, artifacts):
                found.append(hit)
        found.extend(sorted(p for p in artifacts.iterdir()
                            if p.is_file() and p.name.lower().endswith(SIDECAR_SUFFIXES)
                            and p not in found and is_within(p, artifacts)))
    for name in ROOT_MANIFEST_NAMES:
        hit = find_case_insensitive(root, name)
        if hit and hit not in found and is_within(hit, root):
            found.append(hit)
    return found


def verify_manifest(root: Path, manifest: Path, cfg: dict[str, Any],
                    report: Report) -> set[Path]:
    shown = rel(root, manifest)
    base = manifest.parent
    entries, problems = parse_manifest(manifest)
    verified: list[str] = []
    listed: set[Path] = set()
    seen: dict[str, int] = {}
    algorithms: set[str] = set()
    configured = cfg["checksums"]["algorithm"]

    if not entries and not problems:
        problems.append("manifest has no entries")
    for number, digest, entry, declared in entries:
        algo = declared or HASH_BY_LENGTH.get(len(digest))
        if algo is None or (declared and HASH_BY_LENGTH.get(len(digest)) != declared):
            problems.append(f"line {number}: `{entry}` has a digest of unexpected length {len(digest)}")
            continue
        if algo in WEAK_HASHES:
            problems.append(f"line {number}: `{entry}` uses {algo.upper()}, which is not accepted; "
                            "use SHA-256 or stronger")
            continue
        if configured != "auto" and algo != configured:
            problems.append(f"line {number}: `{entry}` uses {algo}, but {configured} is required")
            continue
        if unsafe_entry(entry):
            problems.append(f"line {number}: `{entry}` is not a safe relative path")
            continue
        normalized = entry.replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized in seen:
            problems.append(f"line {number}: `{entry}` is listed again (first on line {seen[normalized]})")
            continue
        seen[normalized] = number
        target = base / normalized
        if not is_within(target, base):  # e.g. a symlink pointing elsewhere
            problems.append(f"line {number}: `{entry}` resolves outside the manifest directory")
            continue
        if not target.is_file():
            problems.append(f"line {number}: `{entry}` is listed but the file does not exist")
            continue
        listed.add(target.resolve())
        actual = file_digest(target, algo)
        if actual != digest:
            problems.append(f"line {number}: `{entry}` {algo} mismatch "
                            f"(manifest {digest[:12]}..., actual {actual[:12]}...)")
            continue
        algorithms.add(algo)
        verified.append(f"`{entry}` ({algo})")

    if problems:
        report.add("Checksums", shown, FAIL,
                   f"{len(problems)} problem(s), {len(verified)} file(s) verified",
                   problems + ([f"Verified: {', '.join(verified)}"] if verified else []), path=shown)
    else:
        report.add("Checksums", shown, PASS,
                   f"{len(verified)} file(s) verified with {', '.join(sorted(algorithms))}",
                   verified, path=shown)
    return listed


def check_checksums(root: Path, cfg: dict[str, Any], args: argparse.Namespace,
                    report: Report) -> None:
    chk = cfg["checksums"]
    if chk["mode"] == "off":
        report.add("Checksums", "Checksum manifests", SKIP, "Checksum verification is turned off")
        return
    if args.artifacts_dir:
        artifacts = Path(args.artifacts_dir)
        if not artifacts.is_absolute():
            artifacts = root / safe_relative(args.artifacts_dir, "artifacts directory")
    else:
        artifacts = root / safe_relative(chk["artifacts_dir"], "checksums.artifacts_dir")
    manifests = discover_manifests(root, artifacts, cfg, report)
    if not manifests:
        if chk["manifests"]:
            return  # missing configured manifests were already reported
        if chk["mode"] == "required":
            report.add("Checksums", "Checksum manifests", FAIL,
                       f"No checksum manifest found in `{rel(root, artifacts)}` or the project root",
                       ["Expected one of: " + ", ".join(MANIFEST_NAMES) + ", or *.sha256 sidecars."])
        else:
            report.add("Checksums", "Checksum manifests", SKIP,
                       "No checksum manifest found; nothing to verify")
        return
    listed: set[Path] = set()
    for manifest in manifests:
        listed |= verify_manifest(root, manifest, cfg, report)

    if chk["unlisted"] == "ignore" or not artifacts.is_dir():
        return
    manifest_set = {m.resolve() for m in manifests}
    unlisted = sorted(
        p.name for p in artifacts.iterdir()
        if p.is_file() and p.resolve() not in manifest_set and p.resolve() not in listed
        and not p.name.startswith(".")
        and not p.name.lower().endswith(SIGNATURE_SUFFIXES + SIDECAR_SUFFIXES)
    )
    if unlisted:
        status = FAIL if chk["unlisted"] == "fail" else WARN
        report.add("Checksums", "Unlisted artifacts", status,
                   f"{len(unlisted)} file(s) in `{rel(root, artifacts)}` have no checksum",
                   [f"`{name}`" for name in unlisted[:50]])
    else:
        report.add("Checksums", "Unlisted artifacts", PASS,
                   f"Every file in `{rel(root, artifacts)}` is covered by a manifest")


# --------------------------------------------------------------------------
# Output: Markdown summary, GitHub outputs, annotations
# --------------------------------------------------------------------------


def md_cell(text: str) -> str:
    """Escape text for a Markdown table cell (no raw HTML, no broken tables)."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("|", "\\|").replace("\r", "").replace("\n", " ")
    return text if len(text) <= 600 else text[:597] + "..."


def render_markdown(report: Report, config_label: str | None) -> str:
    result = report.result
    title = {PASS: "\u2705 PASS \u2014 ready to release",
             WARN: "\u26a0\ufe0f WARN \u2014 releasable, review the warnings",
             FAIL: "\u274c FAIL \u2014 not ready to release"}[result]
    out = [f"## Release readiness: {title}", ""]
    version = (f"`{md_cell(report.release_version)}` (from {md_cell(report.version_origin or '?')})"
               if report.release_version else "not determined")
    out.append(f"**Version:** {version} &nbsp;·&nbsp; **Profile:** {', '.join(report.profiles) or 'n/a'}"
               f" &nbsp;·&nbsp; **Project type:** {report.project_type}"
               f" &nbsp;·&nbsp; **Root:** `{md_cell(report.root)}`"
               f" &nbsp;·&nbsp; **Config:** {('`' + md_cell(config_label) + '`') if config_label else 'defaults'}")
    out.append("")
    out.append(f"**{report.count(PASS)} passed · {report.count(WARN)} warnings · "
               f"{report.count(FAIL)} failed · {report.count(SKIP)} skipped**")
    if report.fail_on_warnings and report.count(WARN) and not report.count(FAIL):
        out.append("")
        out.append("> Warnings are treated as failures (`fail-on-warnings` is enabled).")
    out += ["", "| Category | Status | Pass | Warn | Fail | Skip |", "|---|---|---:|---:|---:|---:|"]
    for category in CATEGORIES:
        items = [f for f in report.findings if f.category == category]
        if not items:
            continue
        worst = max(items, key=lambda f: _SEVERITY[f.status]).status
        counts = [sum(1 for f in items if f.status == s) for s in (PASS, WARN, FAIL, SKIP)]
        out.append(f"| {category} | {_LABEL[worst]} | " + " | ".join(map(str, counts)) + " |")
    for category in CATEGORIES:
        items = [f for f in report.findings if f.category == category]
        if not items:
            continue
        out += ["", f"### {category}", "", "| Status | Check | Result |", "|---|---|---|"]
        for f in sorted(items, key=lambda f: -_SEVERITY[f.status]):
            detail = md_cell(f.message)
            if f.details:
                detail += "<br>" + "<br>".join("• " + md_cell(d) for d in f.details[:20])
                if len(f.details) > 20:
                    detail += f"<br>• … {len(f.details) - 20} more"
            out.append(f"| {_LABEL[f.status]} | {md_cell(f.check)} | {detail} |")
    out += ["", "<sub>Legend: \u2705 PASS = requirement met · \u26a0\ufe0f WARN = should be fixed, "
            "does not block unless fail-on-warnings · \u274c FAIL = blocks the release · "
            "\u23ed\ufe0f SKIP = not applicable or not configured. "
            f"release-readiness {__version__}; read-only, publishes nothing.</sub>", ""]
    return "\n".join(out)


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def annotations(report: Report, path_prefix: str) -> list[str]:
    lines = []
    for f in report.findings:
        if f.status not in (FAIL, WARN):
            continue
        command = "error" if f.status == FAIL else "warning"
        props = [f"title={_escape_property('Release readiness: ' + f.category + ' / ' + f.check)}"]
        if f.path:
            file_path = f.path if path_prefix in ("", ".") else f"{path_prefix.rstrip('/')}/{f.path}"
            props.insert(0, f"file={_escape_property(file_path)}")
        message = f.message.replace("`", "")
        if f.details:
            message += " — " + "; ".join(f.details[:5]).replace("`", "")
        lines.append(f"::{command} {','.join(props)}::{_escape_data(message)}")
    return lines


_SAFE_OUTPUT = re.compile(r"^[0-9A-Za-z.+_~!-]{1,128}$")


def write_outputs(path: str, report: Report) -> None:
    version = report.release_version or ""
    values = {
        "result": report.result,
        "passed": str(report.count(PASS)),
        "warnings": str(report.count(WARN)),
        "failures": str(report.count(FAIL)),
        "skipped": str(report.count(SKIP)),
        "version": version if _SAFE_OUTPUT.match(version) else "",
    }
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    env = os.environ
    p = argparse.ArgumentParser(
        prog="release_readiness.py",
        description="Read-only release-readiness checks (documents, versions, changelog, checksums).",
    )
    p.add_argument("--root", default=".", help="project root to inspect (default: .)")
    p.add_argument("--config", default="", help="config file relative to the root "
                   "(default: .github/release-readiness.json when present)")
    p.add_argument("--profile", default="", help="auto, " + ", ".join(PROFILES))
    p.add_argument("--project-type", default="", help=", ".join(PROJECT_TYPES))
    p.add_argument("--version", default="", help="expected release version (overrides the tag)")
    p.add_argument("--tag-prefix", default=None, help="prefix stripped from tags (default: v)")
    p.add_argument("--version-scheme", default="", help=", ".join(VERSION_SCHEMES))
    p.add_argument("--required-documents", default="", help="comma/newline list, or 'none'")
    p.add_argument("--recommended-documents", default="", help="comma/newline list, or 'none'")
    p.add_argument("--changelog", default="", help="changelog path relative to the root")
    p.add_argument("--checksums", default="", help="off, auto or required")
    p.add_argument("--checksum-manifest", default="", help="manifest path(s) relative to the root")
    p.add_argument("--artifacts-dir", default="", help="directory with release artifacts "
                   "(absolute, or relative to the root)")
    p.add_argument("--fail-on-warnings", action="store_true")
    p.add_argument("--ref-type", default=env.get("GITHUB_REF_TYPE", ""))
    p.add_argument("--ref-name", default=env.get("GITHUB_REF_NAME", ""))
    p.add_argument("--summary-file", default="", help="append the Markdown summary here")
    p.add_argument("--github-output", default="", help="append step outputs here")
    p.add_argument("--json-out", default="", help="write a JSON report here")
    p.add_argument("--annotations", action="store_true", help="emit GitHub workflow annotations")
    p.add_argument("--annotation-path-prefix", default="",
                   help="prefix for annotation file paths (the working directory)")
    p.add_argument("--quiet", action="store_true", help="do not print the summary to stdout")
    p.add_argument("--tool-version", action="version", version=f"release-readiness {__version__}")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> tuple[Report, str | None, int]:
    root = Path(args.root)
    report = Report(root=args.root)
    config_label: str | None = None
    if not root.is_dir():
        report.add("Configuration", "Project root", FAIL, f"`{args.root}` is not a directory")
        return report, None, 2
    try:
        cfg, config_label = build_config(root, args)
    except ConfigError as exc:
        report.add("Configuration", "Configuration", FAIL, str(exc))
        return report, config_label, 2
    report.project_type = cfg["project_type"]
    report.fail_on_warnings = cfg["fail_on_warnings"]
    report.add("Configuration", "Configuration", PASS,
               f"Loaded `{config_label}`" if config_label else "Using built-in defaults")
    try:
        check_documents(root, cfg, report)
        version = check_version(root, cfg, args, report)
        check_changelog(root, cfg, version, report)
        check_checksums(root, cfg, args, report)
    except ConfigError as exc:
        report.add("Configuration", "Configuration", FAIL, str(exc))
        return report, config_label, 2
    return report, config_label, 1 if report.result == FAIL else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report, config_label, code = run(args)
    markdown = render_markdown(report, config_label)
    if not args.quiet:
        print(markdown)
    if args.annotations:
        for line in annotations(report, args.annotation_path_prefix):
            print(line)
    if args.summary_file:
        with open(args.summary_file, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    if args.github_output:
        write_outputs(args.github_output, report)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
