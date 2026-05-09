"""Tests for the npm package vetting script."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vet_npm import (
    _detect_anomalies,
    _extract_dependencies,
    _extract_maintainers,
    _resolve_previous_version,
    extract_github_owner_repo,
    vet_package,
)


# ---------------------------------------------------------------------------
# Unit tests -- pure extraction helpers
# ---------------------------------------------------------------------------


class TestExtractGithubOwnerRepo:
    """Tests for extract_github_owner_repo."""

    def test_git_plus_https_url(self) -> None:
        packument: dict[str, Any] = {}
        version_data = {
            "repository": {
                "url": "git+https://github.com/expressjs/express.git",
                "type": "git",
            },
        }
        assert extract_github_owner_repo(packument, version_data) == "expressjs/express"

    def test_plain_https_url(self) -> None:
        packument: dict[str, Any] = {}
        version_data = {
            "repository": {"url": "https://github.com/lodash/lodash.git", "type": "git"},
        }
        assert extract_github_owner_repo(packument, version_data) == "lodash/lodash"

    def test_git_protocol_url(self) -> None:
        packument: dict[str, Any] = {}
        version_data = {
            "repository": {"url": "git://github.com/user/repo", "type": "git"},
        }
        assert extract_github_owner_repo(packument, version_data) == "user/repo"

    def test_no_dot_git_suffix(self) -> None:
        packument: dict[str, Any] = {}
        version_data = {
            "repository": {"url": "https://github.com/org/project", "type": "git"},
        }
        assert extract_github_owner_repo(packument, version_data) == "org/project"

    def test_returns_none_for_non_github(self) -> None:
        packument: dict[str, Any] = {}
        version_data = {
            "repository": {"url": "https://bitbucket.org/user/repo.git", "type": "git"},
        }
        assert extract_github_owner_repo(packument, version_data) is None

    def test_returns_none_when_missing(self) -> None:
        assert extract_github_owner_repo({}, {}) is None

    def test_prefers_version_data_over_packument(self) -> None:
        packument = {
            "repository": {"url": "git+https://github.com/old/repo.git", "type": "git"},
        }
        version_data = {
            "repository": {"url": "git+https://github.com/new/repo.git", "type": "git"},
        }
        assert extract_github_owner_repo(packument, version_data) == "new/repo"

    def test_falls_back_to_packument(self) -> None:
        packument = {
            "repository": {"url": "git+https://github.com/org/repo.git", "type": "git"},
        }
        assert extract_github_owner_repo(packument, {}) == "org/repo"

    def test_trailing_slash(self) -> None:
        packument: dict[str, Any] = {}
        version_data = {
            "repository": {"url": "https://github.com/org/repo/", "type": "git"},
        }
        assert extract_github_owner_repo(packument, version_data) == "org/repo"


class TestExtractMaintainers:
    """Tests for _extract_maintainers."""

    def test_returns_maintainer_list(self) -> None:
        packument = {
            "maintainers": [
                {"name": "alice", "email": "alice@example.com"},
                {"name": "bob", "email": "bob@example.com"},
            ],
        }
        assert _extract_maintainers(packument) == [
            {"name": "alice", "email": "alice@example.com"},
            {"name": "bob", "email": "bob@example.com"},
        ]

    def test_returns_empty_when_missing(self) -> None:
        assert _extract_maintainers({}) == []


class TestExtractDependencies:
    """Tests for _extract_dependencies."""

    def test_returns_dependencies_dict(self) -> None:
        version_data = {"dependencies": {"lodash": "^4.17.21", "axios": "^1.0.0"}}
        assert _extract_dependencies(version_data) == {
            "lodash": "^4.17.21",
            "axios": "^1.0.0",
        }

    def test_returns_empty_when_missing(self) -> None:
        assert _extract_dependencies({}) == {}


class TestResolvePreviousVersion:
    """Tests for _resolve_previous_version."""

    def test_returns_version_before_target(self) -> None:
        packument = {
            "time": {
                "created": "2020-01-01T00:00:00.000Z",
                "modified": "2024-06-01T00:00:00.000Z",
                "1.0.0": "2020-01-01T00:00:00.000Z",
                "1.1.0": "2021-06-15T00:00:00.000Z",
                "2.0.0": "2023-03-01T00:00:00.000Z",
            },
        }
        assert _resolve_previous_version(packument, "2.0.0") == "1.1.0"

    def test_returns_none_for_first_version(self) -> None:
        packument = {
            "time": {
                "created": "2020-01-01T00:00:00.000Z",
                "modified": "2020-01-01T00:00:00.000Z",
                "1.0.0": "2020-01-01T00:00:00.000Z",
                "1.1.0": "2021-06-15T00:00:00.000Z",
            },
        }
        assert _resolve_previous_version(packument, "1.0.0") is None

    def test_returns_none_when_target_not_in_time(self) -> None:
        packument = {
            "time": {
                "created": "2020-01-01T00:00:00.000Z",
                "1.0.0": "2020-01-01T00:00:00.000Z",
            },
        }
        assert _resolve_previous_version(packument, "9.9.9") is None


class TestDetectAnomalies:
    """Tests for _detect_anomalies."""

    def test_flags_weekend_midnight_publish(self) -> None:
        # 2026-04-05 is a Sunday
        packument: dict[str, Any] = {
            "time": {"1.0.0": "2026-04-05T02:30:00.000Z"},
            "versions": {
                "1.0.0": {"dependencies": {}, "maintainers": []},
            },
        }
        anomalies, _ = _detect_anomalies(packument, "1.0.0", None)
        assert any("Sunday" in a for a in anomalies)

    def test_no_anomaly_for_weekday_afternoon(self) -> None:
        # 2026-04-07 is a Tuesday
        packument: dict[str, Any] = {
            "time": {"1.0.0": "2026-04-07T14:00:00.000Z"},
            "versions": {
                "1.0.0": {"dependencies": {}, "maintainers": []},
            },
        }
        anomalies, _ = _detect_anomalies(packument, "1.0.0", None)
        assert not any("outside typical" in a for a in anomalies)

    def test_flags_new_dependencies(self) -> None:
        packument: dict[str, Any] = {
            "time": {
                "1.0.0": "2024-01-01T12:00:00.000Z",
                "2.0.0": "2024-06-01T12:00:00.000Z",
            },
            "versions": {
                "1.0.0": {
                    "dependencies": {"lodash": "^4.0.0"},
                    "maintainers": [{"name": "alice"}],
                },
                "2.0.0": {
                    "dependencies": {"lodash": "^4.0.0", "evil-pkg": "^1.0.0"},
                    "maintainers": [{"name": "alice"}],
                },
            },
        }
        anomalies, new_deps = _detect_anomalies(packument, "2.0.0", "1.0.0")
        assert "evil-pkg" in new_deps
        assert any("evil-pkg" in a for a in anomalies)

    def test_flags_maintainer_change(self) -> None:
        packument: dict[str, Any] = {
            "time": {
                "1.0.0": "2024-01-01T12:00:00.000Z",
                "2.0.0": "2024-06-01T12:00:00.000Z",
            },
            "versions": {
                "1.0.0": {
                    "dependencies": {},
                    "maintainers": [{"name": "alice"}],
                },
                "2.0.0": {
                    "dependencies": {},
                    "maintainers": [{"name": "bob"}],
                },
            },
        }
        anomalies, _ = _detect_anomalies(packument, "2.0.0", "1.0.0")
        assert any("Maintainer list changed" in a for a in anomalies)
        assert any("added [bob]" in a for a in anomalies)
        assert any("removed [alice]" in a for a in anomalies)

    def test_no_anomalies_when_first_version(self) -> None:
        packument: dict[str, Any] = {
            "time": {"1.0.0": "2024-06-03T14:00:00.000Z"},
            "versions": {
                "1.0.0": {
                    "dependencies": {"lodash": "^4.0.0"},
                    "maintainers": [{"name": "alice"}],
                },
            },
        }
        anomalies, new_deps = _detect_anomalies(packument, "1.0.0", None)
        assert new_deps == []
        assert not any("New dependency" in a for a in anomalies)


# ---------------------------------------------------------------------------
# Mocked error / edge-case tests
# ---------------------------------------------------------------------------


class TestVetPackageNonexistent:
    """vet_package when the package does not exist on npm."""

    @patch("vet_npm.http_get_json")
    def test_exits_nonzero_with_error(
        self,
        mock_get: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from common import VetError

        mock_get.side_effect = VetError(
            "404 Not Found", url="https://registry.npmjs.org/nope"
        )

        with pytest.raises(SystemExit) as exc_info:
            vet_package("nope")

        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().err)
        assert output["package"] == "nope"
        assert len(output["errors"]) > 0


class TestVetPackageDeprecatedVersion:
    """vet_package when the target version is deprecated."""

    @patch("vet_npm.http_post_json", return_value={"vulns": []})
    @patch("vet_npm.http_get_json")
    def test_reports_deprecated_error(
        self,
        mock_get: MagicMock,
        mock_post: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_get.side_effect = [
            {
                "name": "old-pkg",
                "dist-tags": {"latest": "2.0.0"},
                "time": {
                    "created": "2023-01-01T00:00:00.000Z",
                    "modified": "2024-01-01T00:00:00.000Z",
                    "2.0.0": "2024-01-01T00:00:00.000Z",
                },
                "versions": {
                    "2.0.0": {
                        "deprecated": "Use new-pkg instead",
                        "dependencies": {},
                        "maintainers": [{"name": "alice", "email": "a@x.com"}],
                    },
                },
                "maintainers": [{"name": "alice", "email": "a@x.com"}],
            },
        ]

        with pytest.raises(SystemExit) as exc_info:
            vet_package("old-pkg")

        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert any("deprecated" in e.lower() for e in output["errors"])


# ---------------------------------------------------------------------------
# End-to-end tests -- hit real APIs, skipped unless --run-e2e is passed
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestVetPackageE2E:
    """End-to-end tests that run vet_package against real APIs.

    These tests validate that the script produces correct, complete output against real npm packages.  They require
    network access and are skipped by default.  Run with: ``pytest --run-e2e``
    """

    def test_express_report(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Vet express -- popular package with GitHub repo and multiple maintainers."""
        with pytest.raises(SystemExit) as exc_info:
            vet_package("express")

        assert exc_info.value.code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["package"] == "express"
        assert output["registry"] == "npm"
        assert isinstance(output["errors"], list)

        signals = output["signals"]
        assert signals["latest_version"]
        assert "latest" in signals["dist_tags"]
        assert signals["maintainers"]
        assert signals["maintainer_count"] >= 1
        assert isinstance(signals["dependencies"], dict)
        assert signals["source_repo"].startswith("https://github.com/")
        assert signals["has_source_repo"] is True
        assert signals["publish_date"]
        assert isinstance(signals["vulnerabilities"], list)
        assert isinstance(signals["anomalies"], list)
        assert isinstance(signals["new_dependencies_vs_previous"], list)

        gh = signals["github_signals"]
        assert gh["stars"] > 0
        assert gh["forks"] > 0
        assert gh["contributors"] > 0
        assert gh["license"]

    def test_lodash_report(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Vet lodash -- different data shape, single maintainer."""
        with pytest.raises(SystemExit) as exc_info:
            vet_package("lodash")

        assert exc_info.value.code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["package"] == "lodash"
        assert output["registry"] == "npm"

        signals = output["signals"]
        assert signals["latest_version"]
        assert signals["maintainers"]
        assert signals["source_repo"].startswith("https://github.com/")
        assert isinstance(signals["dependencies"], dict)
        assert isinstance(signals["vulnerabilities"], list)

    def test_scoped_package_report(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Vet @angular/core -- scoped package to verify URL handling."""
        with pytest.raises(SystemExit) as exc_info:
            vet_package("@angular/core")

        assert exc_info.value.code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["package"] == "@angular/core"
        assert output["registry"] == "npm"

        signals = output["signals"]
        assert signals["latest_version"]
        assert signals["maintainers"]
        assert signals["has_source_repo"] is True
