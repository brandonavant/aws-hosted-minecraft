#!/usr/bin/env python3
"""Vet a GitHub Action by querying the GitHub API, raw content, and OSV.dev.

Collects raw health and security signals for a given GitHub Action and outputs a structured JSON report.  Uses only the
Python standard library.

Usage:
    python3 scripts/vet_gh_action.py <owner/repo@ref>
    python3 scripts/vet_gh_action.py <owner/repo/path@ref>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow importing common when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    DEFAULT_TIMEOUT_SECONDS,
    GITHUB_API_URL,
    MAX_RETRIES,
    RETRY_DELAY_SECONDS,
    VetError,
    VetReport,
    emit_report,
    fail,
    fetch_github_signals,
    github_headers,
    http_get_text,
    http_post_json,
    summarise_vulnerabilities,
)

OSV_QUERY_URL = "https://api.osv.dev/v1/query"

RAW_CONTENT_URL = (
    "https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
)

OFFICIAL_GITHUB_ORGS: frozenset[str] = frozenset({"actions", "github"})

MAJOR_VERSION_RE = re.compile(r"^v\d+$")

USES_RE = re.compile(
    r"^(?P<owner>[^/]+)/(?P<repo>[^/@]+)(?:/(?P<path>[^@]+))?@(?P<ref>.+)$"
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

RUNTIME_USING_RE = re.compile(
    r"^[ \t]+using:\s*[\"']?(\w+)[\"']?", re.MULTILINE
)

NODE_RUNTIME_STATUS: dict[str, str] = {
    "node12": "deprecated",
    "node16": "deprecated",
    "node20": "supported",
    "node22": "supported",
    "composite": "n/a",
    "docker": "n/a",
}


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_action_ref(ref_str: str) -> tuple[str, str, str | None, str]:
    """Parse a GitHub Actions ``uses:`` reference string.

    Accepts both ``owner/repo@ref`` and ``owner/repo/path@ref`` formats.

    Args:
        ref_str: The raw reference string (e.g. ``"actions/checkout@v4"``).

    Returns:
        A tuple of ``(owner, repo, path_or_none, ref)``.

    Raises:
        ValueError: If *ref_str* does not match the expected format.
    """
    match = USES_RE.match(ref_str)
    if not match:
        raise ValueError(
            f"Invalid action reference '{ref_str}'. "
            f"Expected owner/repo@ref or owner/repo/path@ref."
        )

    owner = match.group("owner")
    repo = match.group("repo")
    path = match.group("path")  # None when no sub-path
    ref = match.group("ref")

    if not ref:
        raise ValueError(
            f"Invalid action reference '{ref_str}'. Ref cannot be empty."
        )

    return owner, repo, path, ref


# ---------------------------------------------------------------------------
# Authenticated GitHub API helper
# ---------------------------------------------------------------------------


def _github_get_json(url: str) -> Any:
    """Fetch JSON from the GitHub API with authentication and retries.

    Uses ``github_headers()`` for token-based authentication.  Does not retry 404 responses (they are definitive).
    Retries on 5xx and network errors only.

    Args:
        url: The GitHub API URL to fetch.

    Returns:
        The parsed JSON response body.

    Raises:
        VetError: If the request fails after retries or returns a non-retryable error (404, 403).
    """
    headers = github_headers()
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(
                req, timeout=DEFAULT_TIMEOUT_SECONDS
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise VetError(
                    f"Not found: {url} (HTTP 404)", url=url
                ) from exc
            if exc.code == 403:
                raise VetError(
                    f"Forbidden: {url} (HTTP 403) -- possible rate limit",
                    url=url,
                ) from exc
            last_error = exc
            if attempt <= MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt <= MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    raise VetError(
        f"Failed to fetch {url} after {MAX_RETRIES + 1} attempts: {last_error}",
        url=url,
    )


# ---------------------------------------------------------------------------
# Data-fetching functions
# ---------------------------------------------------------------------------


def fetch_repo_metadata(owner: str, repo: str) -> dict[str, Any]:
    """Fetch repository metadata from the GitHub API.

    Args:
        owner: The repository owner (user or organization).
        repo: The repository name.

    Returns:
        The full repository JSON from ``GET /repos/{owner}/{repo}``.

    Raises:
        VetError: If the repository does not exist or the API is unreachable.
    """
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}"
    return _github_get_json(url)


def verify_ref(
    owner: str, repo: str, ref: str
) -> tuple[bool, str | None]:
    """Check whether a git ref exists in the repository.

    Tries tags first, then branches.  For 40-character hex strings, checks the commit endpoint directly.

    Args:
        owner: The repository owner.
        repo: The repository name.
        ref: The ref to verify (tag name, branch name, or commit SHA).

    Returns:
        A tuple of ``(exists, ref_type)`` where *ref_type* is ``"tag"``, ``"branch"``, ``"commit"``, or ``None`` if the
        ref was not found.
    """
    if SHA_RE.match(ref):
        try:
            _github_get_json(
                f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/commits/{ref}"
            )
            return True, "commit"
        except VetError:
            return False, None

    # Try tag first.
    try:
        _github_get_json(
            f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/ref/tags/{ref}"
        )
        return True, "tag"
    except VetError:
        pass

    # Fall back to branch.
    try:
        _github_get_json(
            f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/ref/heads/{ref}"
        )
        return True, "branch"
    except VetError:
        pass

    return False, None


def fetch_major_version_tags(owner: str, repo: str) -> list[str]:
    """List major-version alias tags (e.g. ``v1``, ``v2``, ``v4``).

    Fetches all tags starting with ``v`` and filters to those matching the ``v<number>`` pattern (no minor/patch
    suffixes).

    Args:
        owner: The repository owner.
        repo: The repository name.

    Returns:
        A sorted list of major-version tag names, or an empty list on failure.
    """
    try:
        url = (
            f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/matching-refs/tags/v"
        )
        refs = _github_get_json(url)
    except VetError:
        return []

    tags: list[str] = []
    for entry in refs:
        ref_name = entry.get("ref", "")
        tag = ref_name.removeprefix("refs/tags/")
        if MAJOR_VERSION_RE.match(tag):
            tags.append(tag)

    tags.sort(key=lambda t: int(t[1:]))
    return tags


def fetch_action_yaml(
    owner: str, repo: str, ref: str, path: str | None
) -> str | None:
    """Fetch the ``action.yml`` or ``action.yaml`` definition file.

    Reads from ``raw.githubusercontent.com`` to avoid base64 decoding. For sub-path actions, prefixes the filename with
    *path*.

    Args:
        owner: The repository owner.
        repo: The repository name.
        ref: The git ref (tag, branch, or SHA) to fetch from.
        path: Sub-directory path within the repo, or ``None`` for root.

    Returns:
        The raw YAML content as a string, or ``None`` if neither ``action.yml`` nor ``action.yaml`` exists at the target
        ref.
    """
    prefix = f"{path}/" if path else ""

    for filename in ("action.yml", "action.yaml"):
        url = RAW_CONTENT_URL.format(
            owner=owner, repo=repo, ref=ref, path=f"{prefix}{filename}"
        )
        try:
            return http_get_text(url)
        except VetError:
            continue

    return None


def fetch_vulnerabilities(owner_repo: str) -> list[dict[str, Any]]:
    """Query OSV.dev for known vulnerabilities affecting a GitHub Action.

    Args:
        owner_repo: The ``owner/repo`` slug (e.g. ``"tj-actions/changed-files"``).

    Returns:
        A list of vulnerability objects, or an empty list on failure.
    """
    try:
        data = http_post_json(
            OSV_QUERY_URL,
            {"package": {"name": owner_repo, "ecosystem": "GitHub Actions"}},
        )
        return data.get("vulns", [])
    except VetError:
        return []


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------


def extract_runtime(action_yaml_content: str) -> str | None:
    """Extract the ``runs.using`` value from action YAML content.

    Uses a regex to avoid depending on a YAML parser.  Handles unquoted, single-quoted, and double-quoted values.

    Args:
        action_yaml_content: The raw text of an ``action.yml`` file.

    Returns:
        The runtime string (e.g. ``"node20"``, ``"docker"``, ``"composite"``), or ``None`` if no ``using:`` field was
        found.
    """
    match = RUNTIME_USING_RE.search(action_yaml_content)
    if match:
        return match.group(1)
    return None


def classify_runtime(runtime: str | None) -> str:
    """Classify a Node.js runtime string by its GitHub Actions support status.

    Args:
        runtime: The ``runs.using`` value (e.g. ``"node20"``), or ``None``.

    Returns:
        One of ``"deprecated"``, ``"supported"``, ``"n/a"``, or ``"unknown"``.
    """
    if runtime is None:
        return "unknown"
    return NODE_RUNTIME_STATUS.get(runtime, "unknown")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def vet_action(action_ref: str) -> None:
    """Run all vetting checks for a GitHub Action and emit the report.

    Args:
        action_ref: The action reference in ``owner/repo@ref`` or ``owner/repo/path@ref`` format.
    """
    try:
        owner, repo, path, ref = parse_action_ref(action_ref)
    except ValueError as exc:
        fail(action_ref, "gh-action", str(exc))
        return  # unreachable -- fail() calls sys.exit

    owner_repo = f"{owner}/{repo}"

    try:
        repo_meta = fetch_repo_metadata(owner, repo)
    except VetError as exc:
        fail(owner_repo, "gh-action", str(exc))
        return  # unreachable

    report = VetReport(package=owner_repo, registry="gh-action")

    # -- Core metadata --
    report.signals["requested_ref"] = ref
    report.signals["action_path"] = path
    is_archived = repo_meta.get("archived", False)
    report.signals["is_archived"] = is_archived

    if is_archived:
        report.errors.append(
            f"Repository {owner_repo} is archived"
        )

    # -- Ref verification --
    ref_exists, ref_type = verify_ref(owner, repo, ref)
    report.signals["ref_exists"] = ref_exists
    report.signals["ref_type"] = ref_type

    if not ref_exists:
        report.errors.append(
            f"Requested ref '{ref}' does not exist in {owner_repo}"
        )

    # -- Major version tags --
    report.signals["available_major_tags"] = fetch_major_version_tags(
        owner, repo
    )

    # -- Node.js runtime --
    action_content = fetch_action_yaml(owner, repo, ref, path)
    runtime = extract_runtime(action_content) if action_content else None
    runtime_status = classify_runtime(runtime)
    report.signals["node_runtime"] = runtime
    report.signals["node_runtime_status"] = runtime_status

    # -- GitHub health signals --
    report.signals["github_signals"] = fetch_github_signals(owner_repo)

    # -- Vulnerabilities --
    vulns = fetch_vulnerabilities(owner_repo)
    report.signals["vulnerabilities"] = summarise_vulnerabilities(vulns)

    # -- Derived signals --
    pushed_at = repo_meta.get("pushed_at")
    if pushed_at:
        try:
            pushed = datetime.fromisoformat(
                pushed_at.replace("Z", "+00:00")
            )
            report.signals["days_since_last_commit"] = (
                datetime.now(timezone.utc) - pushed
            ).days
        except (ValueError, TypeError):
            report.signals["days_since_last_commit"] = None
    else:
        report.signals["days_since_last_commit"] = None

    report.signals["is_official_github_action"] = (
        owner.lower() in OFFICIAL_GITHUB_ORGS
    )
    report.signals["owner_type"] = repo_meta.get("owner", {}).get("type")

    emit_report(report)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI arguments and run the vetting process."""
    parser = argparse.ArgumentParser(
        description="Vet a GitHub Action for health and security signals.",
    )
    parser.add_argument(
        "action",
        help=(
            "Action reference in owner/repo@ref or owner/repo/path@ref "
            "format."
        ),
    )
    args = parser.parse_args()
    vet_action(args.action)


if __name__ == "__main__":
    main()
