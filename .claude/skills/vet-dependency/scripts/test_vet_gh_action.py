"""Tests for the GitHub Actions vetting script."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from common import VetError
from vet_gh_action import (
    classify_runtime,
    extract_runtime,
    parse_action_ref,
    vet_action,
)


# ---------------------------------------------------------------------------
# Unit tests -- pure extraction helpers
# ---------------------------------------------------------------------------


class TestParseActionRef:
    """Tests for parse_action_ref."""

    def test_simple_owner_repo_ref(self) -> None:
        assert parse_action_ref("actions/checkout@v4") == (
            "actions", "checkout", None, "v4",
        )

    def test_with_subpath(self) -> None:
        assert parse_action_ref("actions/cache/save@v4") == (
            "actions", "cache", "save", "v4",
        )

    def test_deep_subpath(self) -> None:
        assert parse_action_ref("github/codeql-action/init@v3") == (
            "github", "codeql-action", "init", "v3",
        )

    def test_sha_ref(self) -> None:
        owner, repo, path, ref = parse_action_ref(
            "actions/checkout@692973e3d937129bcbf40652eb9f2f61becf3332"
        )
        assert owner == "actions"
        assert repo == "checkout"
        assert path is None
        assert ref == "692973e3d937129bcbf40652eb9f2f61becf3332"

    def test_branch_ref(self) -> None:
        assert parse_action_ref("actions/checkout@main") == (
            "actions", "checkout", None, "main",
        )

    def test_invalid_no_at_sign(self) -> None:
        with pytest.raises(ValueError, match="Invalid action reference"):
            parse_action_ref("actions/checkout")

    def test_invalid_no_owner(self) -> None:
        with pytest.raises(ValueError, match="Invalid action reference"):
            parse_action_ref("checkout@v4")

    def test_invalid_empty_ref(self) -> None:
        with pytest.raises(ValueError, match="Invalid action reference"):
            parse_action_ref("actions/checkout@")


class TestExtractRuntime:
    """Tests for extract_runtime."""

    def test_node20_unquoted(self) -> None:
        content = "runs:\n  using: node20\n  main: dist/index.js\n"
        assert extract_runtime(content) == "node20"

    def test_node20_double_quoted(self) -> None:
        content = 'runs:\n  using: "node20"\n  main: dist/index.js\n'
        assert extract_runtime(content) == "node20"

    def test_node20_single_quoted(self) -> None:
        content = "runs:\n  using: 'node20'\n  main: dist/index.js\n"
        assert extract_runtime(content) == "node20"

    def test_docker(self) -> None:
        content = "runs:\n  using: docker\n  image: Dockerfile\n"
        assert extract_runtime(content) == "docker"

    def test_composite(self) -> None:
        content = "runs:\n  using: composite\n  steps:\n    - run: echo hi\n"
        assert extract_runtime(content) == "composite"

    def test_no_runs_section(self) -> None:
        content = "name: My Action\ndescription: A thing\n"
        assert extract_runtime(content) is None


class TestClassifyRuntime:
    """Tests for classify_runtime."""

    def test_node12_deprecated(self) -> None:
        assert classify_runtime("node12") == "deprecated"

    def test_node16_deprecated(self) -> None:
        assert classify_runtime("node16") == "deprecated"

    def test_node20_supported(self) -> None:
        assert classify_runtime("node20") == "supported"

    def test_node22_supported(self) -> None:
        assert classify_runtime("node22") == "supported"

    def test_composite_na(self) -> None:
        assert classify_runtime("composite") == "n/a"

    def test_docker_na(self) -> None:
        assert classify_runtime("docker") == "n/a"

    def test_unknown_runtime(self) -> None:
        assert classify_runtime("node99") == "unknown"

    def test_none_runtime(self) -> None:
        assert classify_runtime(None) == "unknown"


# ---------------------------------------------------------------------------
# Mocked error / edge-case tests
# ---------------------------------------------------------------------------


class TestVetActionNonexistentRepo:
    """vet_action when the repository does not exist."""

    @patch("vet_gh_action._github_get_json")
    def test_exits_nonzero_with_error(
        self,
        mock_get: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_get.side_effect = VetError(
            "Not found: https://api.github.com/repos/fake/nope (HTTP 404)",
            url="https://api.github.com/repos/fake/nope",
        )

        with pytest.raises(SystemExit) as exc_info:
            vet_action("fake/nope@v1")

        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().err)
        assert output["package"] == "fake/nope"
        assert len(output["errors"]) > 0


class TestVetActionNonexistentRef:
    """vet_action when the ref does not exist in the repository."""

    @patch("vet_gh_action.fetch_vulnerabilities", return_value=[])
    @patch("vet_gh_action.fetch_github_signals", return_value={})
    @patch("vet_gh_action.fetch_action_yaml", return_value=None)
    @patch("vet_gh_action.fetch_major_version_tags", return_value=[])
    @patch("vet_gh_action.verify_ref", return_value=(False, None))
    @patch("vet_gh_action.fetch_repo_metadata")
    def test_exits_nonzero_ref_not_found(
        self,
        mock_meta: MagicMock,
        mock_verify: MagicMock,
        mock_tags: MagicMock,
        mock_yaml: MagicMock,
        mock_gh: MagicMock,
        mock_vulns: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_meta.return_value = {
            "archived": False,
            "pushed_at": "2025-01-01T00:00:00Z",
            "owner": {"type": "Organization"},
        }

        with pytest.raises(SystemExit) as exc_info:
            vet_action("actions/checkout@v999")

        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert any("v999" in e and "does not exist" in e for e in output["errors"])


class TestVetActionArchivedRepo:
    """vet_action when the repository is archived."""

    @patch("vet_gh_action.fetch_vulnerabilities", return_value=[])
    @patch("vet_gh_action.fetch_github_signals", return_value={})
    @patch("vet_gh_action.fetch_action_yaml", return_value=None)
    @patch("vet_gh_action.fetch_major_version_tags", return_value=[])
    @patch("vet_gh_action.verify_ref", return_value=(True, "tag"))
    @patch("vet_gh_action.fetch_repo_metadata")
    def test_reports_archived_error(
        self,
        mock_meta: MagicMock,
        mock_verify: MagicMock,
        mock_tags: MagicMock,
        mock_yaml: MagicMock,
        mock_gh: MagicMock,
        mock_vulns: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_meta.return_value = {
            "archived": True,
            "pushed_at": "2023-01-01T00:00:00Z",
            "owner": {"type": "Organization"},
        }

        with pytest.raises(SystemExit) as exc_info:
            vet_action("old-org/old-action@v1")

        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert any("archived" in e.lower() for e in output["errors"])


class TestVetActionDeprecatedRuntime:
    """vet_action when the action uses a deprecated Node.js runtime."""

    @patch("vet_gh_action.fetch_vulnerabilities", return_value=[])
    @patch("vet_gh_action.fetch_github_signals", return_value={})
    @patch(
        "vet_gh_action.fetch_action_yaml",
        return_value="runs:\n  using: node16\n  main: dist/index.js\n",
    )
    @patch("vet_gh_action.fetch_major_version_tags", return_value=["v1"])
    @patch("vet_gh_action.verify_ref", return_value=(True, "tag"))
    @patch("vet_gh_action.fetch_repo_metadata")
    def test_deprecated_runtime_is_not_hard_blocker(
        self,
        mock_meta: MagicMock,
        mock_verify: MagicMock,
        mock_tags: MagicMock,
        mock_yaml: MagicMock,
        mock_gh: MagicMock,
        mock_vulns: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_meta.return_value = {
            "archived": False,
            "pushed_at": "2025-01-01T00:00:00Z",
            "owner": {"type": "Organization"},
        }

        with pytest.raises(SystemExit) as exc_info:
            vet_action("some-org/some-action@v1")

        # Deprecated runtime is a soft signal, NOT a hard blocker.
        assert exc_info.value.code == 0
        output = json.loads(capsys.readouterr().out)
        assert output["signals"]["node_runtime"] == "node16"
        assert output["signals"]["node_runtime_status"] == "deprecated"


# ---------------------------------------------------------------------------
# End-to-end tests -- hit real APIs, skipped unless --run-e2e is passed
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestVetActionE2E:
    """End-to-end tests that run vet_action against real APIs.

    These tests validate that the script produces correct, complete output against real GitHub Actions.  They require
    network access and are skipped by default.  Run with: ``pytest --run-e2e``
    """

    def test_actions_checkout_v4(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Vet actions/checkout@v4 -- popular, official action."""
        with pytest.raises(SystemExit) as exc_info:
            vet_action("actions/checkout@v4")

        assert exc_info.value.code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["package"] == "actions/checkout"
        assert output["registry"] == "gh-action"
        assert isinstance(output["errors"], list)
        assert len(output["errors"]) == 0

        signals = output["signals"]
        assert signals["requested_ref"] == "v4"
        assert signals["ref_exists"] is True
        assert signals["ref_type"] == "tag"
        assert signals["is_archived"] is False
        assert "v4" in signals["available_major_tags"]
        assert signals["node_runtime"] is not None
        assert signals["node_runtime_status"] in ("supported", "deprecated")
        assert signals["is_official_github_action"] is True
        assert signals["owner_type"] == "Organization"
        assert isinstance(signals["vulnerabilities"], list)

        gh = signals["github_signals"]
        assert gh["stars"] > 0
        assert gh["forks"] > 0

    def test_actions_cache_save_v4(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Vet actions/cache/save@v4 -- sub-path action."""
        with pytest.raises(SystemExit) as exc_info:
            vet_action("actions/cache/save@v4")

        assert exc_info.value.code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["package"] == "actions/cache"
        assert output["registry"] == "gh-action"

        signals = output["signals"]
        assert signals["ref_exists"] is True
        assert signals["action_path"] == "save"
        assert signals["node_runtime"] is not None

    def test_nonexistent_tag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Vet actions/checkout@v999 -- tag that does not exist."""
        with pytest.raises(SystemExit) as exc_info:
            vet_action("actions/checkout@v999")

        assert exc_info.value.code == 1

        output = json.loads(capsys.readouterr().out)
        assert any(
            "v999" in e and "does not exist" in e for e in output["errors"]
        )
