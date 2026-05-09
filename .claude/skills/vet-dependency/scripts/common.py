#!/usr/bin/env python3
"""Shared utilities for vet-dependency scripts.

Provides HTTP helpers, structured JSON output formatting, and error handling used across all registry-specific vetting
scripts (vet_pypi.py, vet_npm.py, vet_gh_action.py).

All functions use only the Python standard library — no third-party dependencies.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

USER_AGENT = "vet-dependency/0.1"
DEFAULT_TIMEOUT_SECONDS = 15
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 3
GITHUB_API_URL = "https://api.github.com"


class VetError(Exception):
    """Raised when a vetting operation fails in a non-recoverable way.

    Args:
        message: Human-readable description of the failure.
        url: The URL that was being fetched when the error occurred, if any.
    """

    def __init__(self, message: str, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


@dataclass
class VetReport:
    """Structured output for a dependency vetting run.

    Attributes:
        package: The package name that was vetted.
        registry: The registry that was queried (e.g., "pypi", "npm", "gh-action").
        signals: Raw signals collected from the registry and security APIs.
        errors: Non-fatal errors encountered during vetting.
        timestamp: ISO 8601 timestamp of when the report was generated.
    """

    package: str
    registry: str
    signals: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        """Serialize the report to a JSON string.

        Returns:
            A pretty-printed JSON string suitable for stdout.
        """
        return json.dumps(asdict(self), indent=2)


def http_get_json(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Any:
    """Fetch JSON from a URL with retries and error handling.

    Args:
        url: The URL to fetch.
        timeout: Request timeout in seconds.

    Returns:
        The parsed JSON response body.

    Raises:
        VetError: If the request fails after all retry attempts.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt <= MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    raise VetError(
        f"Failed to fetch {url} after {MAX_RETRIES + 1} attempts: {last_error}",
        url=url,
    )


def http_get_text(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Fetch raw text from a URL with retries and error handling.

    Args:
        url: The URL to fetch.
        timeout: Request timeout in seconds.

    Returns:
        The response body as a string.

    Raises:
        VetError: If the request fails after all retry attempts.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            last_error = exc
            if attempt <= MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    raise VetError(
        f"Failed to fetch {url} after {MAX_RETRIES + 1} attempts: {last_error}",
        url=url,
    )


def emit_report(report: VetReport) -> None:
    """Print a VetReport as JSON to stdout and exit.

    Exits 0 if no errors were recorded, 1 if the report contains errors.

    Args:
        report: The completed vetting report.
    """
    print(report.to_json())
    sys.exit(1 if report.errors else 0)


def fail(package: str, registry: str, message: str) -> None:
    """Emit a minimal error report and exit with code 1.

    Use this for fatal errors that prevent any signals from being collected.

    Args:
        package: The package name that was being vetted.
        registry: The registry being queried.
        message: Human-readable error description.
    """
    report = VetReport(package=package, registry=registry, errors=[message])
    print(report.to_json(), file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def github_headers() -> dict[str, str]:
    """Build HTTP headers for GitHub API requests.

    Uses ``GITHUB_TOKEN`` or ``GH_TOKEN`` from the environment when available to avoid the unauthenticated rate limit.

    Returns:
        A header dict suitable for ``urllib.request.Request``.
    """
    headers: dict[str, str] = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def http_post_json(url: str, payload: dict[str, Any]) -> Any:
    """Send a JSON POST request and return the parsed response.

    Args:
        url: The URL to POST to.
        payload: The JSON-serializable request body.

    Returns:
        The parsed JSON response body.

    Raises:
        VetError: If the request fails.
    """
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise VetError(f"POST {url} failed: {exc}", url=url) from exc


def fetch_github_signals(owner_repo: str) -> dict[str, Any]:
    """Fetch repository health signals from the GitHub API.

    Collects stars, open issues, last commit date, contributor count, security-policy presence, and license information.

    Args:
        owner_repo: The ``owner/repo`` slug (e.g. ``"fastapi/fastapi"``).

    Returns:
        A dict of GitHub health signals.  Missing signals are set to sensible zero-values rather than omitted.
    """
    headers = github_headers()
    signals: dict[str, Any] = {
        "stars": 0,
        "forks": 0,
        "open_issues": 0,
        "last_commit": None,
        "contributors": 0,
        "has_security_policy": False,
        "license": None,
    }

    # --- Repo metadata (single request) ---
    try:
        repo_url = f"{GITHUB_API_URL}/repos/{owner_repo}"
        repo = http_get_json(repo_url)
    except VetError:
        return signals

    signals["stars"] = repo.get("stargazers_count", 0)
    signals["forks"] = repo.get("forks_count", 0)
    signals["open_issues"] = repo.get("open_issues_count", 0)
    signals["last_commit"] = repo.get("pushed_at")

    license_info = repo.get("license")
    if license_info:
        signals["license"] = license_info.get("spdx_id")

    # --- Contributor count (pagination trick) ---
    try:
        contrib_url = (
            f"{GITHUB_API_URL}/repos/{owner_repo}/contributors?per_page=1&anon=true"
        )
        req = urllib.request.Request(contrib_url, headers=headers)
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as resp:
            link = resp.headers.get("Link", "")
            last_match = re.search(r'page=(\d+)>; rel="last"', link)
            if last_match:
                signals["contributors"] = int(last_match.group(1))
            else:
                signals["contributors"] = len(json.loads(resp.read().decode("utf-8")))
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass

    # --- Security policy ---
    signals["has_security_policy"] = has_security_policy(owner_repo, headers)

    return signals


def has_security_policy(owner_repo: str, headers: dict[str, str]) -> bool:
    """Check whether the repository has a security policy.

    Tries the community-profile endpoint first (one request), then falls back to checking for a ``SECURITY.md`` file.

    Args:
        owner_repo: The ``owner/repo`` slug.
        headers: Pre-built GitHub API headers.

    Returns:
        ``True`` if a security policy is detected.
    """
    try:
        community_url = f"{GITHUB_API_URL}/repos/{owner_repo}/community/profile"
        community = http_get_json(community_url)
        if community.get("files", {}).get("security_policy") is not None:
            return True
    except VetError:
        pass

    try:
        sec_url = f"{GITHUB_API_URL}/repos/{owner_repo}/contents/SECURITY.md"
        req = urllib.request.Request(sec_url, headers=headers)
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass

    return False


def summarise_vulnerabilities(vulns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Condense raw OSV vulnerability records into a summary list.

    Args:
        vulns: Raw vulnerability dicts from the OSV.dev response.

    Returns:
        A list of summary dicts with ``id``, ``summary``, and ``fixed_versions``.
    """
    summaries: list[dict[str, Any]] = []
    for v in vulns:
        fixed: list[str] = []
        for affected in v.get("affected", []):
            for rng in affected.get("ranges", []):
                for event in rng.get("events", []):
                    if "fixed" in event:
                        fixed.append(event["fixed"])

        summaries.append(
            {
                "id": v.get("id"),
                "summary": v.get("summary", ""),
                "fixed_versions": fixed,
            }
        )
    return summaries
