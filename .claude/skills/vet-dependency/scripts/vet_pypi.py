#!/usr/bin/env python3
"""Vet a PyPI package by querying PyPI, OSV.dev, and GitHub APIs.

Collects raw health and security signals for a given Python package and outputs a structured JSON report.  Uses only the
Python standard library.

Usage:
    python3 scripts/vet_pypi.py <package-name> [--version <version>]
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow importing common when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    VetError,
    VetReport,
    emit_report,
    fail,
    fetch_github_signals,
    http_get_json,
    http_post_json,
    summarise_vulnerabilities,
)

PYPI_PACKAGE_URL = "https://pypi.org/pypi/{package}/json"
OSV_QUERY_URL = "https://api.osv.dev/v1/query"


# ---------------------------------------------------------------------------
# Data-fetching functions
# ---------------------------------------------------------------------------


def fetch_pypi_metadata(package: str) -> dict[str, Any]:
    """Fetch package metadata from the PyPI JSON API.

    Args:
        package: The PyPI package name.

    Returns:
        The full JSON response from PyPI.

    Raises:
        VetError: If the package does not exist or PyPI is unreachable.
    """
    url = PYPI_PACKAGE_URL.format(package=package)
    return http_get_json(url)


def fetch_vulnerabilities(package: str) -> list[dict[str, Any]]:
    """Query OSV.dev for known vulnerabilities affecting *package*.

    Args:
        package: The PyPI package name.

    Returns:
        A list of vulnerability objects, or an empty list on failure.
    """
    try:
        data = http_post_json(
            OSV_QUERY_URL,
            {"package": {"name": package, "ecosystem": "PyPI"}},
        )
        return data.get("vulns", [])
    except VetError:
        return []


# Keys most likely to contain the canonical repo URL, checked first.
_REPO_KEY_PRIORITY = ("Source", "Repository", "Code", "Homepage")

# GitHub top-level path segments that are platform pages, not repos.
_NON_REPO_SEGMENTS = frozenset({"sponsors", "orgs"})


def extract_github_owner_repo(info: dict[str, Any]) -> str | None:
    """Derive the GitHub ``owner/repo`` slug from PyPI project metadata.

    Checks ``project_urls`` first (preferring keys like *Source* and *Repository* over unrelated links), then falls back
    to ``home_page``. URLs pointing to known non-repository GitHub pages (e.g. ``github.com/sponsors/...``) are skipped.

    Args:
        info: The ``info`` dict from the PyPI JSON response.

    Returns:
        A string like ``"fastapi/fastapi"``, or ``None`` if no GitHub repository URL is found.
    """
    project_urls: dict[str, str] = info.get("project_urls") or {}

    seen: set[str] = set()
    candidates: list[str] = []

    for key in _REPO_KEY_PRIORITY:
        url = project_urls.get(key)
        if url and url not in seen:
            candidates.append(url)
            seen.add(url)

    for url in project_urls.values():
        if url not in seen:
            candidates.append(url)
            seen.add(url)

    home = info.get("home_page") or ""
    if home and home not in seen:
        candidates.append(home)

    for url in candidates:
        if "github.com" not in url:
            continue
        match = re.match(
            r"https?://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?(?:[#?].*)?$",
            url,
        )
        if match and match.group(1) not in _NON_REPO_SEGMENTS:
            return f"{match.group(1)}/{match.group(2)}"

    return None


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------


def _extract_maintainers(pypi_data: dict[str, Any]) -> list[str]:
    """Extract maintainer usernames from the PyPI JSON response.

    Uses ``ownership.roles`` as the primary source (returns actual PyPI usernames).  Falls back to ``info.maintainer``,
    ``info.author``, or ``info.author_email`` when roles are unavailable.

    Args:
        pypi_data: The full JSON response from the PyPI package endpoint.

    Returns:
        A list of maintainer identifiers (usernames or, as a fallback, names/emails).
    """
    roles: list[dict[str, str]] = (
        pypi_data.get("ownership", {}).get("roles", [])
    )
    if roles:
        return [r["user"] for r in roles if "user" in r]

    info: dict[str, Any] = pypi_data.get("info", {})

    if info.get("maintainer"):
        return [info["maintainer"]]
    if info.get("author"):
        return [info["author"]]
    if info.get("author_email"):
        return [
            e.strip() for e in info["author_email"].split(",") if e.strip()
        ]

    return []


def _extract_direct_dependencies(info: dict[str, Any]) -> list[str]:
    """Extract non-optional direct dependency names from PyPI metadata.

    Args:
        info: The ``info`` dict from the PyPI JSON response.

    Returns:
        A list of package names (without version specifiers).
    """
    return [
        re.split(r"[<>=!;\s\[]", dep)[0]
        for dep in (info.get("requires_dist") or [])
        if "extra ==" not in dep
    ]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def vet_package(package: str, version: str | None = None) -> None:
    """Run all vetting checks for a PyPI package and emit the report.

    Args:
        package: The PyPI package name to vet.
        version: Optional specific version to inspect.  When ``None`` the latest version is used.
    """
    try:
        pypi_data = fetch_pypi_metadata(package)
    except VetError as exc:
        fail(package, "pypi", str(exc))
        return  # unreachable — fail() calls sys.exit

    info: dict[str, Any] = pypi_data.get("info", {})
    releases: dict[str, list[dict[str, Any]]] = pypi_data.get("releases", {})
    latest_version: str = info.get("version", "")

    report = VetReport(package=package, registry="pypi")

    # -- Core metadata --
    report.signals["latest_version"] = latest_version
    report.signals["requested_version"] = version

    if version and version not in releases:
        report.errors.append(f"Requested version {version} not found on PyPI")

    # Publish date for the latest (or requested) version
    target_version = version if version and version in releases else latest_version
    target_files = releases.get(target_version, [])
    report.signals["publish_date"] = (
        target_files[0].get("upload_time_iso_8601") if target_files else None
    )

    maintainers = _extract_maintainers(pypi_data)
    report.signals["maintainers"] = maintainers
    report.signals["python_requires"] = info.get("requires_python")
    report.signals["direct_dependencies"] = _extract_direct_dependencies(info)

    # -- Source repo --
    owner_repo = extract_github_owner_repo(info)
    if owner_repo:
        report.signals["source_repo"] = f"https://github.com/{owner_repo}"
    else:
        report.signals["source_repo"] = None
        report.errors.append(
            "No GitHub repository URL found — skipped GitHub health checks"
        )

    # -- GitHub signals --
    if owner_repo:
        report.signals["github_signals"] = fetch_github_signals(owner_repo)
    else:
        report.signals["github_signals"] = {}

    # -- Vulnerabilities --
    vulns = fetch_vulnerabilities(package)
    report.signals["vulnerabilities"] = summarise_vulnerabilities(vulns)

    # -- Derived signals --
    if report.signals["publish_date"]:
        try:
            pub = datetime.fromisoformat(
                report.signals["publish_date"].replace("Z", "+00:00")
            )
            report.signals["days_since_last_release"] = (
                datetime.now(timezone.utc) - pub
            ).days
        except (ValueError, TypeError):
            report.signals["days_since_last_release"] = None
    else:
        report.signals["days_since_last_release"] = None

    report.signals["maintainer_count"] = len(maintainers)
    report.signals["has_source_repo"] = owner_repo is not None

    # -- Hard blockers --
    all_yanked = releases and all(
        all(f.get("yanked", False) for f in files)
        for files in releases.values()
        if files
    )
    if all_yanked:
        report.errors.append("All versions are yanked")

    emit_report(report)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI arguments and run the vetting process."""
    parser = argparse.ArgumentParser(
        description="Vet a PyPI package for health and security signals.",
    )
    parser.add_argument("package", help="The PyPI package name to vet.")
    parser.add_argument(
        "--version",
        default=None,
        help="Specific version to check (defaults to latest).",
    )
    args = parser.parse_args()
    vet_package(args.package, args.version)


if __name__ == "__main__":
    main()
