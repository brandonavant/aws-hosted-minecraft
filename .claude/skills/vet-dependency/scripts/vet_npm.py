#!/usr/bin/env python3
"""Vet an npm package by querying the npm registry, OSV.dev, and GitHub APIs.

Collects raw health and security signals for a given npm package and outputs a structured JSON report.  Uses only the
Python standard library.

Usage:
    python3 scripts/vet_npm.py <package-name> [--version <version>]
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

NPM_PACKAGE_URL = "https://registry.npmjs.org/{package}"
OSV_QUERY_URL = "https://api.osv.dev/v1/query"


# ---------------------------------------------------------------------------
# Data-fetching functions
# ---------------------------------------------------------------------------


def fetch_npm_metadata(package: str) -> dict[str, Any]:
    """Fetch the full packument from the npm registry.

    The full packument is required (rather than the abbreviated endpoint) because we need the ``time`` object for
    per-version publish timestamps and per-version ``maintainers``/``dependencies`` for anomaly detection.

    Args:
        package: The npm package name (supports scoped packages like ``@angular/core``).

    Returns:
        The full JSON packument from the npm registry.

    Raises:
        VetError: If the package does not exist or the registry is unreachable.
    """
    url = NPM_PACKAGE_URL.format(package=package)
    return http_get_json(url, timeout=30)


def fetch_vulnerabilities(package: str) -> list[dict[str, Any]]:
    """Query OSV.dev for known vulnerabilities affecting *package*.

    Args:
        package: The npm package name.

    Returns:
        A list of vulnerability objects, or an empty list on failure.
    """
    try:
        data = http_post_json(
            OSV_QUERY_URL,
            {"package": {"name": package, "ecosystem": "npm"}},
        )
        return data.get("vulns", [])
    except VetError:
        return []


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------


_REPO_URL_RE = re.compile(
    r"(?:git\+)?(?:https?|git)://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?$"
)


def extract_github_owner_repo(
    packument: dict[str, Any],
    version_data: dict[str, Any],
) -> str | None:
    """Derive the GitHub ``owner/repo`` slug from npm package metadata.

    Checks the version-level ``repository`` field first, then falls back to the top-level packument ``repository``.
    Handles common npm URL formats: ``git+https://``, ``git://``, ``https://``, with or without ``.git``.

    Args:
        packument: The full npm packument.
        version_data: The metadata dict for a specific version.

    Returns:
        A string like ``"expressjs/express"``, or ``None`` if no GitHub repository URL is found.
    """
    for source in (version_data, packument):
        repo = source.get("repository")
        if repo is None:
            continue

        url = repo.get("url", "") if isinstance(repo, dict) else str(repo)

        match = _REPO_URL_RE.match(url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"

    return None


def _extract_maintainers(packument: dict[str, Any]) -> list[dict[str, str]]:
    """Extract the current maintainer list from the npm packument.

    Args:
        packument: The full npm packument.

    Returns:
        A list of maintainer dicts with ``name`` and ``email`` fields.
    """
    return packument.get("maintainers", [])


def _extract_dependencies(version_data: dict[str, Any]) -> dict[str, str]:
    """Extract runtime dependencies for a specific version.

    Args:
        version_data: The metadata dict for a specific version.

    Returns:
        A dict mapping dependency names to version ranges.
    """
    return version_data.get("dependencies", {})


# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------


def _resolve_target_version(
    packument: dict[str, Any],
    requested: str | None,
) -> str:
    """Determine which version to inspect.

    Args:
        packument: The full npm packument.
        requested: An explicit version string from the CLI, or ``None``.

    Returns:
        The version string to use as the inspection target.
    """
    if requested and requested in packument.get("versions", {}):
        return requested
    return packument.get("dist-tags", {}).get("latest", "")


def _resolve_previous_version(
    packument: dict[str, Any],
    target_version: str,
) -> str | None:
    """Find the version published immediately before *target_version*.

    Uses the ``time`` object to sort by publish timestamp rather than semver, since this answers the real question:
    "what was the last thing published before this release?"

    Args:
        packument: The full npm packument.
        target_version: The version we are inspecting.

    Returns:
        The previous version string, or ``None`` if the target is the first published version.
    """
    time_map: dict[str, str] = packument.get("time", {})

    version_times: list[tuple[str, str]] = [
        (ver, ts)
        for ver, ts in time_map.items()
        if ver not in ("created", "modified")
    ]
    version_times.sort(key=lambda vt: vt[1])

    previous: str | None = None
    for ver, _ in version_times:
        if ver == target_version:
            return previous
        previous = ver

    return None


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


def _detect_anomalies(
    packument: dict[str, Any],
    target_version: str,
    previous_version: str | None,
) -> tuple[list[str], list[str]]:
    """Check for supply-chain anomaly signals.

    Compares the target version against the previous version for dependency and maintainer changes, and checks the
    publish timestamp for unusual timing.

    Args:
        packument: The full npm packument.
        target_version: The version being inspected.
        previous_version: The version published before the target, or ``None`` if the target is the first version.

    Returns:
        A tuple of ``(anomalies, new_dependencies)`` where *anomalies* is a list of human-readable anomaly strings and
        *new_dependencies* is a list of dependency names added vs. the previous version.
    """
    anomalies: list[str] = []
    new_deps: list[str] = []
    versions: dict[str, Any] = packument.get("versions", {})
    time_map: dict[str, str] = packument.get("time", {})

    # -- Unusual publish time --
    publish_ts = time_map.get(target_version)
    if publish_ts:
        try:
            dt = datetime.fromisoformat(publish_ts.replace("Z", "+00:00"))
            if dt.weekday() >= 5 and dt.hour < 6:
                day_name = dt.strftime("%A")
                anomalies.append(
                    f"Published at {dt.strftime('%H:%M')} UTC on a {day_name}"
                    f" -- outside typical release pattern"
                )
        except (ValueError, TypeError):
            pass

    if previous_version is None or previous_version not in versions:
        return anomalies, new_deps

    target_data = versions.get(target_version, {})
    prev_data = versions.get(previous_version, {})

    # -- New dependencies --
    target_deps = set(_extract_dependencies(target_data).keys())
    prev_deps = set(_extract_dependencies(prev_data).keys())
    new_deps = sorted(target_deps - prev_deps)
    for dep in new_deps:
        anomalies.append(
            f"New dependency '{dep}' not present in previous version"
            f" {previous_version}"
        )

    # -- Maintainer changes --
    target_maintainers = {
        m.get("name", "") for m in target_data.get("maintainers", [])
    }
    prev_maintainers = {
        m.get("name", "") for m in prev_data.get("maintainers", [])
    }
    added = sorted(target_maintainers - prev_maintainers)
    removed = sorted(prev_maintainers - target_maintainers)
    if added or removed:
        parts: list[str] = []
        if added:
            parts.append(f"added [{', '.join(added)}]")
        if removed:
            parts.append(f"removed [{', '.join(removed)}]")
        anomalies.append(
            f"Maintainer list changed between {previous_version} and"
            f" {target_version}: {', '.join(parts)}"
        )

    return anomalies, new_deps


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def vet_package(package: str, version: str | None = None) -> None:
    """Run all vetting checks for an npm package and emit the report.

    Args:
        package: The npm package name to vet.
        version: Optional specific version to inspect.  When ``None`` the latest version is used.
    """
    try:
        packument = fetch_npm_metadata(package)
    except VetError as exc:
        fail(package, "npm", str(exc))
        return  # unreachable -- fail() calls sys.exit

    versions: dict[str, Any] = packument.get("versions", {})
    dist_tags: dict[str, str] = packument.get("dist-tags", {})
    time_map: dict[str, str] = packument.get("time", {})
    latest_version: str = dist_tags.get("latest", "")

    report = VetReport(package=package, registry="npm")

    # -- Version resolution --
    target_version = _resolve_target_version(packument, version)
    previous_version = _resolve_previous_version(packument, target_version)
    target_data: dict[str, Any] = versions.get(target_version, {})

    # -- Core metadata --
    report.signals["latest_version"] = latest_version
    report.signals["requested_version"] = version
    report.signals["dist_tags"] = dist_tags

    if version and version not in versions:
        report.errors.append(f"Requested version {version} not found on npm")

    # -- Publish date --
    report.signals["publish_date"] = time_map.get(target_version)

    # -- Maintainers --
    maintainers = _extract_maintainers(packument)
    report.signals["maintainers"] = maintainers

    # -- Dependencies --
    report.signals["dependencies"] = _extract_dependencies(target_data)

    # -- Source repo --
    owner_repo = extract_github_owner_repo(packument, target_data)
    if owner_repo:
        report.signals["source_repo"] = f"https://github.com/{owner_repo}"
    else:
        report.signals["source_repo"] = None
        report.errors.append(
            "No GitHub repository URL found -- skipped GitHub health checks"
        )

    # -- GitHub signals --
    if owner_repo:
        report.signals["github_signals"] = fetch_github_signals(owner_repo)
    else:
        report.signals["github_signals"] = {}

    # -- Vulnerabilities --
    vulns = fetch_vulnerabilities(package)
    report.signals["vulnerabilities"] = summarise_vulnerabilities(vulns)

    # -- Anomaly detection --
    anomalies, new_deps = _detect_anomalies(
        packument, target_version, previous_version,
    )
    report.signals["anomalies"] = anomalies

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
    report.signals["new_dependencies_vs_previous"] = new_deps

    # -- Hard blockers --
    deprecated = target_data.get("deprecated")
    if deprecated:
        report.errors.append(
            f"Version {target_version} is deprecated: {deprecated}"
        )

    emit_report(report)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI arguments and run the vetting process."""
    parser = argparse.ArgumentParser(
        description="Vet an npm package for health and security signals.",
    )
    parser.add_argument("package", help="The npm package name to vet.")
    parser.add_argument(
        "--version",
        default=None,
        help="Specific version to check (defaults to latest).",
    )
    args = parser.parse_args()
    vet_package(args.package, args.version)


if __name__ == "__main__":
    main()
