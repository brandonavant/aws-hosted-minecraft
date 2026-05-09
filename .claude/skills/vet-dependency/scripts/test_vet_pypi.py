"""Tests for the PyPI package vetting script."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from common import summarise_vulnerabilities
from vet_pypi import (
    _extract_direct_dependencies,
    _extract_maintainers,
    extract_github_owner_repo,
    vet_package,
)

# ---------------------------------------------------------------------------
# Fixtures — kept only for edge-case / error-path mocked tests
# ---------------------------------------------------------------------------

PYPI_RESPONSE_ALL_YANKED: dict[str, Any] = {
    "info": {
        "version": "1.0.0",
        "author": "someone",
        "maintainer": "",
        "maintainer_email": "",
        "requires_python": ">=3.8",
        "requires_dist": None,
        "home_page": "",
        "project_urls": {},
    },
    "releases": {
        "1.0.0": [{"upload_time_iso_8601": "2023-01-01T00:00:00.000000Z", "yanked": True}],
    },
}

OSV_WITH_VULNS: dict[str, Any] = {
    "vulns": [
        {
            "id": "PYSEC-2024-001",
            "summary": "Path traversal in upload handler",
            "affected": [
                {
                    "ranges": [
                        {
                            "type": "ECOSYSTEM",
                            "events": [
                                {"introduced": "0.100.0"},
                                {"fixed": "0.110.0"},
                            ],
                        },
                    ],
                },
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Unit tests — pure extraction helpers
# ---------------------------------------------------------------------------


class TestExtractMaintainers:
    """Tests for _extract_maintainers."""

    def test_uses_ownership_roles(self) -> None:
        pypi_data = {
            "info": {"maintainer": "ignored", "author": "ignored"},
            "ownership": {
                "roles": [
                    {"role": "Owner", "user": "samuelcolvin"},
                    {"role": "Maintainer", "user": "dmontagu"},
                ]
            },
        }
        assert _extract_maintainers(pypi_data) == ["samuelcolvin", "dmontagu"]

    def test_falls_back_to_info_when_roles_empty(self) -> None:
        pypi_data = {
            "info": {"maintainer": "alice", "author": "bob"},
            "ownership": {"roles": []},
        }
        assert _extract_maintainers(pypi_data) == ["alice"]

    def test_falls_back_to_author(self) -> None:
        pypi_data = {
            "info": {"maintainer": "", "author": "bob"},
            "ownership": {"roles": []},
        }
        assert _extract_maintainers(pypi_data) == ["bob"]

    def test_falls_back_to_author_email(self) -> None:
        pypi_data = {
            "info": {
                "maintainer": None,
                "author": None,
                "author_email": "a@x.com, b@x.com",
            },
            "ownership": {"roles": []},
        }
        assert _extract_maintainers(pypi_data) == ["a@x.com", "b@x.com"]

    def test_empty_when_no_metadata(self) -> None:
        assert _extract_maintainers({"info": {}}) == []


class TestExtractDirectDependencies:
    """Tests for _extract_direct_dependencies."""

    def test_strips_version_specifiers(self) -> None:
        info = {"requires_dist": ["starlette<0.39.0,>=0.37.2", "pydantic>=1.7.4"]}
        assert _extract_direct_dependencies(info) == ["starlette", "pydantic"]

    def test_excludes_extras(self) -> None:
        info = {"requires_dist": ["httpx>=0.23.0 ; extra == \"all\"", "pydantic>=1.7.4"]}
        assert _extract_direct_dependencies(info) == ["pydantic"]

    def test_none_requires_dist(self) -> None:
        assert _extract_direct_dependencies({"requires_dist": None}) == []


class TestExtractGithubOwnerRepo:
    """Tests for extract_github_owner_repo."""

    def test_finds_repo_in_project_urls(self) -> None:
        info = {"project_urls": {"Source": "https://github.com/owner/repo"}}
        assert extract_github_owner_repo(info) == "owner/repo"

    def test_strips_trailing_slash(self) -> None:
        info = {"project_urls": {"Source": "https://github.com/owner/repo/"}}
        assert extract_github_owner_repo(info) == "owner/repo"

    def test_falls_back_to_home_page(self) -> None:
        info = {"project_urls": {}, "home_page": "https://github.com/owner/repo"}
        assert extract_github_owner_repo(info) == "owner/repo"

    def test_returns_none_for_non_github(self) -> None:
        info = {"project_urls": {"Docs": "https://example.com"}, "home_page": ""}
        assert extract_github_owner_repo(info) is None

    def test_ignores_deep_github_paths(self) -> None:
        info = {"project_urls": {"Docs": "https://github.com/owner/repo/tree/main/docs"}}
        assert extract_github_owner_repo(info) is None

    def test_prefers_source_over_funding(self) -> None:
        info = {
            "project_urls": {
                "Changelog": "https://docs.pydantic.dev/latest/changelog/",
                "Documentation": "https://docs.pydantic.dev",
                "Funding": "https://github.com/sponsors/samuelcolvin",
                "Homepage": "https://github.com/pydantic/pydantic",
                "Source": "https://github.com/pydantic/pydantic",
            },
        }
        assert extract_github_owner_repo(info) == "pydantic/pydantic"

    def test_skips_sponsors_url(self) -> None:
        info = {"project_urls": {"Funding": "https://github.com/sponsors/samuelcolvin"}}
        assert extract_github_owner_repo(info) is None

    def test_skips_orgs_url(self) -> None:
        info = {"project_urls": {"Organization": "https://github.com/orgs/pydantic"}}
        assert extract_github_owner_repo(info) is None

    def test_prefers_repository_key(self) -> None:
        info = {
            "project_urls": {
                "Funding": "https://github.com/sponsors/someone",
                "Repository": "https://github.com/org/project",
            },
        }
        assert extract_github_owner_repo(info) == "org/project"

    def test_falls_back_to_non_preferred_key(self) -> None:
        info = {"project_urls": {"GitHub": "https://github.com/owner/repo"}}
        assert extract_github_owner_repo(info) == "owner/repo"

    def test_strips_dot_git_suffix(self) -> None:
        info = {"project_urls": {"Source": "https://github.com/owner/repo.git"}}
        assert extract_github_owner_repo(info) == "owner/repo"


class TestSummariseVulnerabilities:
    """Tests for _summarise_vulnerabilities."""

    def test_extracts_fixed_versions(self) -> None:
        result = summarise_vulnerabilities(OSV_WITH_VULNS["vulns"])
        assert len(result) == 1
        assert result[0]["id"] == "PYSEC-2024-001"
        assert result[0]["fixed_versions"] == ["0.110.0"]

    def test_empty_list_for_no_vulns(self) -> None:
        assert summarise_vulnerabilities([]) == []


# ---------------------------------------------------------------------------
# Mocked error / edge-case tests — appropriate use of mocks for scenarios
# that cannot be triggered against real packages
# ---------------------------------------------------------------------------


class TestVetPackageNonexistent:
    """vet_package when the package does not exist on PyPI."""

    @patch("vet_pypi.http_get_json")
    def test_exits_nonzero_with_error(
            self,
            mock_get: MagicMock,
            capsys: pytest.CaptureFixture[str],
    ) -> None:
        from common import VetError

        mock_get.side_effect = VetError("404 Not Found", url="https://pypi.org/pypi/nope/json")

        with pytest.raises(SystemExit) as exc_info:
            vet_package("nope")

        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().err)
        assert output["package"] == "nope"
        assert len(output["errors"]) > 0


class TestVetPackageAllYanked:
    """vet_package when every release is yanked."""

    @patch("vet_pypi.http_post_json", return_value={"vulns": []})
    @patch("vet_pypi.http_get_json")
    def test_reports_all_yanked_error(
            self,
            mock_get: MagicMock,
            mock_post: MagicMock,
            capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_get.side_effect = [PYPI_RESPONSE_ALL_YANKED]

        with pytest.raises(SystemExit) as exc_info:
            vet_package("yanked-pkg")

        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert any("yanked" in e.lower() for e in output["errors"])


# ---------------------------------------------------------------------------
# End-to-end tests — hit real APIs, skipped unless --run-e2e is passed
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestVetPackageE2E:
    """End-to-end tests that run vet_package against real APIs.

    These tests validate that the script produces correct, complete output against real PyPI packages. They require
    network access and are skipped by default. Run with: ``pytest --run-e2e``
    """

    def test_fastapi_report(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Vet fastapi — popular package with GitHub repo and single owner."""
        with pytest.raises(SystemExit) as exc_info:
            vet_package("fastapi")

        assert exc_info.value.code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["package"] == "fastapi"
        assert output["registry"] == "pypi"
        assert isinstance(output["errors"], list)

        signals = output["signals"]
        assert signals["latest_version"]
        assert signals["maintainers"]
        assert signals["maintainer_count"] >= 1
        assert signals["source_repo"].startswith("https://github.com/")
        assert signals["has_source_repo"] is True
        assert isinstance(signals["vulnerabilities"], list)
        assert signals["direct_dependencies"]
        assert signals["python_requires"]
        assert signals["publish_date"]

        gh = signals["github_signals"]
        assert gh["stars"] > 0
        assert gh["forks"] > 0
        assert gh["contributors"] > 0
        assert gh["license"]

    def test_requests_report(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Vet requests — different data shape (null home_page, different URL keys)."""
        with pytest.raises(SystemExit) as exc_info:
            vet_package("requests")

        assert exc_info.value.code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["package"] == "requests"
        assert output["registry"] == "pypi"

        signals = output["signals"]
        assert signals["latest_version"]
        assert signals["maintainers"]
        assert signals["source_repo"].startswith("https://github.com/")
        assert isinstance(signals["vulnerabilities"], list)
        assert signals["direct_dependencies"]
