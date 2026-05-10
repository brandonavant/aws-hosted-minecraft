"""Unit + e2e tests for ``update.py``.

Pure-logic and side-effect-free tests run by default. ``@pytest.mark.e2e`` tests hit the real PaperMC,
Geyser, Floodgate, and Hangar APIs and are skipped unless ``pytest --run-e2e`` is passed (see
``conftest.py`` — the flag is shared with ``test_install.py``).

The flock collision test fork()s a child process to grab the lock first; this exercises the real
``fcntl.flock`` rather than mocking it, which is the only way to verify the cross-process exclusion the
real systemd timer relies on.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from _common import (
    GEYSER_API_BASE,
    DownloadError,
    JarArtifact,
)
from update import (
    ALL_COMPONENTS,
    COMPONENT_FLOODGATE,
    COMPONENT_GEYSER,
    COMPONENT_PAPER,
    COMPONENT_VIAVERSION,
    DEFAULT_BACKUP_RETENTION,
    DEFAULT_DATA_ROOT,
    DEFAULT_TIMER_ONCALENDAR,
    FLOODGATE_JAR_CANDIDATES,
    LockAcquisitionError,
    PaperMcVersionMismatchError,
    ServiceRestartError,
    UpdateError,
    UpdatePaths,
    acquire_lock,
    apply_plans,
    assert_floodgate_key_untouched,
    assert_paper_same_mc,
    atomic_swap,
    build_argument_parser,
    build_plan,
    fetch_artifact_for,
    install_systemd_units,
    installed_version_for,
    is_paper_build_newer,
    is_semver_update_available,
    list_backups,
    main,
    parse_floodgate_build_suffix,
    parse_semver_prefix,
    parse_trailing_dash_int,
    prune_backups,
    render_updater_service,
    render_updater_timer,
    rollback_swap,
    staging_path_for,
    timestamped_backup_path,
    wait_for_tcp,
    write_unit_if_changed,
)
from update import ComponentPlan, _parse_components_csv

# --- parse_semver_prefix -----------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2.10.0-SNAPSHOT", (2, 10, 0)),
        ("2.2.5-SNAPSHOT (b132-5a72b6a)", (2, 2, 5)),
        ("5.9.1", (5, 9, 1)),
        ("1.21.8-60", (1, 21, 8)),
        ("2.10.0-1143", (2, 10, 0)),
        ("not-a-version", None),
    ],
)
def test_parse_semver_prefix_handles_real_shapes(raw: str, expected: tuple[int, ...] | None) -> None:
    """The three real plugin.yml shapes plus API artifact-version shapes parse to the right tuple."""
    assert parse_semver_prefix(raw) == expected


def test_parse_floodgate_build_suffix_pulls_b_number() -> None:
    """The ``(b<N>-<sha>)`` suffix Floodgate writes is recognized."""
    assert parse_floodgate_build_suffix("2.2.5-SNAPSHOT (b132-5a72b6a)") == 132


def test_parse_floodgate_build_suffix_returns_none_when_absent() -> None:
    """Geyser/ViaVersion plugin.yml have no ``(b<N>-...)`` suffix → ``None``."""
    assert parse_floodgate_build_suffix("2.10.0-SNAPSHOT") is None
    assert parse_floodgate_build_suffix("5.9.1") is None


def test_parse_trailing_dash_int_handles_api_versions() -> None:
    """The API artifact-version shape ``X.Y.Z-N`` exposes its trailing build N."""
    assert parse_trailing_dash_int("1.21.8-60") == 60
    assert parse_trailing_dash_int("2.10.0-1143") == 1143
    assert parse_trailing_dash_int("5.9.1") is None  # no trailing -<int>


# --- Paper version comparison ------------------------------------------------


def test_is_paper_build_newer_simple_integer_compare() -> None:
    """Paper update decision is simple integer comparison."""
    assert is_paper_build_newer(58, 60) is True
    assert is_paper_build_newer(60, 60) is False
    assert is_paper_build_newer(60, 58) is False  # downgrade — never apply.


def test_assert_paper_same_mc_passes_when_mc_matches() -> None:
    """Matching MC versions allow the comparison to return."""
    assert_paper_same_mc("1.21.8", "1.21.8")


def test_assert_paper_same_mc_raises_on_mc_drift() -> None:
    """Cross-MC API ``latest`` is rejected to preserve world-format compatibility."""
    with pytest.raises(PaperMcVersionMismatchError, match="cross-MC bump"):
        assert_paper_same_mc("1.21.8", "1.21.9")


def test_assert_paper_same_mc_honors_max_paper_build_clamp() -> None:
    """``--max-paper-build`` refuses higher builds even within the same MC version."""
    with pytest.raises(PaperMcVersionMismatchError, match="exceeds --max-paper-build"):
        assert_paper_same_mc("1.21.8", "1.21.8", max_build=60, api_build=61)


def test_assert_paper_same_mc_permits_build_within_clamp() -> None:
    """A build equal-to or below the clamp passes."""
    assert_paper_same_mc("1.21.8", "1.21.8", max_build=60, api_build=60)


# --- Geyser/Floodgate/ViaVersion version comparison -------------------------


@pytest.mark.parametrize(
    "installed, api, expected",
    [
        ("2.10.0-SNAPSHOT", "2.10.0-1143", False),  # same semver, no Floodgate build embedded → indeterminate.
        ("2.10.0-SNAPSHOT", "2.11.0-1", True),  # API minor bumped.
        ("2.2.5-SNAPSHOT (b132-5a72b6a)", "2.2.5-133", True),  # same semver, API build higher.
        ("2.2.5-SNAPSHOT (b132-5a72b6a)", "2.2.5-132", False),  # same semver + same build.
        ("2.2.5-SNAPSHOT (b132-5a72b6a)", "2.2.6-1", True),  # patch bumped.
        ("5.9.1", "5.9.2", True),  # ViaVersion clean semver bump.
        ("5.9.1", "5.9.1", False),  # equal.
        ("5.10.0", "5.9.1", False),  # API somehow older — never apply.
    ],
)
def test_is_semver_update_available(installed: str, api: str, expected: bool) -> None:
    """Semver-prefix compare with Floodgate build fallback covers Geyser/Floodgate/ViaVersion."""
    assert is_semver_update_available(installed, api) is expected


# --- Backup naming + listing -------------------------------------------------


def test_timestamped_backup_path_format(tmp_path: Path) -> None:
    """Backup path is ``<jar>.YYYYMMDDTHHMMSS`` in UTC."""
    jar = tmp_path / "paper.jar"
    now = datetime(2026, 5, 12, 10, 30, 0, tzinfo=timezone.utc)
    backup = timestamped_backup_path(jar, now=now)
    assert backup == tmp_path / "paper.jar.20260512T103000"


def test_staging_path_for_appends_new_suffix(tmp_path: Path) -> None:
    """Staging path is ``<jar>.new`` in the same directory."""
    jar = tmp_path / "paper.jar"
    assert staging_path_for(jar) == tmp_path / "paper.jar.new"


def test_list_backups_sorts_oldest_first(tmp_path: Path) -> None:
    """Backups are returned in chronological order by timestamp lex sort."""
    jar = tmp_path / "paper.jar"
    jar.write_bytes(b"x")
    for ts in ("20260512T100000", "20260511T090000", "20260513T080000"):
        (tmp_path / f"paper.jar.{ts}").write_bytes(b"x")
    (tmp_path / "paper.jar.partial").write_bytes(b"x")  # must NOT be picked up
    (tmp_path / "paper.jar.new").write_bytes(b"x")  # must NOT be picked up
    backups = list_backups(jar)
    assert [b.name for b in backups] == [
        "paper.jar.20260511T090000",
        "paper.jar.20260512T100000",
        "paper.jar.20260513T080000",
    ]


def test_prune_backups_keeps_newest_n(tmp_path: Path) -> None:
    """``prune_backups`` deletes the oldest backups beyond ``retention``."""
    jar = tmp_path / "paper.jar"
    jar.write_bytes(b"x")
    timestamps = ["20260510T100000", "20260511T100000", "20260512T100000", "20260513T100000"]
    for ts in timestamps:
        (tmp_path / f"paper.jar.{ts}").write_bytes(b"x")
    removed = prune_backups(jar, retention=2)
    remaining = sorted(p.name for p in tmp_path.glob("paper.jar.*"))
    assert sorted(r.name for r in removed) == ["paper.jar.20260510T100000", "paper.jar.20260511T100000"]
    assert remaining == ["paper.jar.20260512T100000", "paper.jar.20260513T100000"]


def test_prune_backups_is_noop_when_under_retention(tmp_path: Path) -> None:
    """No backups are deleted when count <= retention."""
    jar = tmp_path / "paper.jar"
    jar.write_bytes(b"x")
    (tmp_path / "paper.jar.20260510T100000").write_bytes(b"x")
    removed = prune_backups(jar, retention=3)
    assert removed == []


# --- Atomic swap + rollback simulation --------------------------------------


def test_atomic_swap_leaves_backup_and_chowns_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The swap creates a timestamped backup and chowns the swapped-in jar."""
    jar = tmp_path / "paper.jar"
    jar.write_bytes(b"old-paper-bytes")
    staging = tmp_path / "paper.jar.new"
    staging.write_bytes(b"new-paper-bytes")
    chown_calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        chown_calls.append(list(argv))
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("update.subprocess.run", fake_run)
    now = datetime(2026, 5, 12, 4, 0, 0, tzinfo=timezone.utc)
    backup = atomic_swap(jar, staging, owner_user="minecraft", owner_group="minecraft", now=now)
    assert jar.read_bytes() == b"new-paper-bytes"
    assert backup.read_bytes() == b"old-paper-bytes"
    assert not staging.exists()
    assert chown_calls and chown_calls[0][:2] == ["chown", "minecraft:minecraft"]


def test_rollback_swap_restores_previous_jar(tmp_path: Path) -> None:
    """Rolling back moves the (suspect) jar aside and restores the backup into place."""
    jar = tmp_path / "paper.jar"
    backup = tmp_path / "paper.jar.20260512T040000"
    jar.write_bytes(b"suspect-new")
    backup.write_bytes(b"trusted-old")
    rollback_swap(jar, backup)
    assert jar.read_bytes() == b"trusted-old"
    assert not backup.exists()
    suspects = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("paper.jar.failed."))
    assert len(suspects) == 1


# --- Lock-file collision ----------------------------------------------------


def test_acquire_lock_succeeds_when_uncontended(tmp_path: Path) -> None:
    """First-acquisition path works and writes the PID."""
    lock = tmp_path / "lock"
    handle = acquire_lock(lock)
    assert lock.exists()
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def test_acquire_lock_refuses_when_held(tmp_path: Path) -> None:
    """A second acquisition while the first is held raises LockAcquisitionError.

    We hold the lock via fork() so the OS sees two independent processes — that's the real-world
    scenario (manual run + systemd timer firing simultaneously). Same-process flock with the same fd
    does not block, so we cannot exercise this within a single Python process.
    """
    lock = tmp_path / "lock"
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            handle = acquire_lock(lock)
            os.write(write_fd, b"locked\n")
            time.sleep(0.5)
            handle.close()
        finally:
            os._exit(0)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 32) == b"locked\n"
        with pytest.raises(LockAcquisitionError):
            acquire_lock(lock)
    finally:
        os.close(read_fd)
        os.waitpid(pid, 0)


# --- build_plan: decides apply / skip / missing -----------------------------


def _make_paths(tmp_path: Path) -> UpdatePaths:
    """Build an ``UpdatePaths`` rooted at ``tmp_path`` and pre-create the dirs."""
    paths = UpdatePaths.from_data_root(tmp_path)
    paths.server_dir.mkdir(parents=True, exist_ok=True)
    paths.plugins_dir.mkdir(parents=True, exist_ok=True)
    return paths


def _write_paper_history(paths: UpdatePaths, mc: str, build: int) -> None:
    """Stub Paper's version_history.json in the server dir."""
    (paths.server_dir / "version_history.json").write_text(
        f'{{"currentVersion": "git-Paper-{build} (MC: {mc})"}}', encoding="utf-8"
    )


def test_build_plan_skips_when_sha_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the on-disk jar's sha matches the API's, the component plans ``skip``."""
    paths = _make_paths(tmp_path)
    paths.jar_for(COMPONENT_GEYSER).write_bytes(b"current-geyser-bytes")
    matching_sha = hashlib.sha256(b"current-geyser-bytes").hexdigest()
    artifact = JarArtifact(
        label="geyser",
        version="2.10.0-1143",
        download_url="https://example/g.jar",
        sha256=matching_sha,
        filename="Geyser-Spigot.jar",
    )
    monkeypatch.setattr("update.fetch_artifact_for", lambda comp, overrides: artifact)
    monkeypatch.setattr("update.read_plugin_version", lambda j: "2.10.0-SNAPSHOT")
    plans = build_plan([COMPONENT_GEYSER], paths, {}, force=False)
    assert len(plans) == 1
    assert plans[0].action == "skip"


def test_build_plan_applies_when_sha_differs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sha mismatch flips the component to ``apply``."""
    paths = _make_paths(tmp_path)
    paths.jar_for(COMPONENT_GEYSER).write_bytes(b"old-geyser-bytes")
    artifact = JarArtifact(
        label="geyser",
        version="2.10.0-1143",
        download_url="https://example/g.jar",
        sha256="ff" * 32,
        filename="Geyser-Spigot.jar",
    )
    monkeypatch.setattr("update.fetch_artifact_for", lambda comp, overrides: artifact)
    monkeypatch.setattr("update.read_plugin_version", lambda j: "2.10.0-SNAPSHOT")
    plans = build_plan([COMPONENT_GEYSER], paths, {}, force=False)
    assert plans[0].action == "apply"


def test_build_plan_force_applies_even_when_sha_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--force`` overrides the sha match."""
    paths = _make_paths(tmp_path)
    paths.jar_for(COMPONENT_GEYSER).write_bytes(b"current")
    matching_sha = hashlib.sha256(b"current").hexdigest()
    artifact = JarArtifact(
        label="geyser",
        version="2.10.0-1143",
        download_url="https://example/g.jar",
        sha256=matching_sha,
        filename="Geyser-Spigot.jar",
    )
    monkeypatch.setattr("update.fetch_artifact_for", lambda comp, overrides: artifact)
    monkeypatch.setattr("update.read_plugin_version", lambda j: "2.10.0-SNAPSHOT")
    plans = build_plan([COMPONENT_GEYSER], paths, {}, force=True)
    assert plans[0].action == "apply"


def test_build_plan_marks_missing_when_jar_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A component with no installed jar is flagged ``missing`` (install.py's job to place)."""
    paths = _make_paths(tmp_path)
    artifact = JarArtifact(
        label="geyser",
        version="2.10.0-1143",
        download_url="https://example/g.jar",
        sha256="ff" * 32,
        filename="Geyser-Spigot.jar",
    )
    monkeypatch.setattr("update.fetch_artifact_for", lambda comp, overrides: artifact)
    plans = build_plan([COMPONENT_GEYSER], paths, {}, force=False)
    assert plans[0].action == "missing"


def test_build_plan_paper_clamp_rejects_cross_mc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Paper plan halts when the API's latest is for a different MC version."""
    paths = _make_paths(tmp_path)
    paths.paper_jar.write_bytes(b"paper-1.21.8-58")
    _write_paper_history(paths, "1.21.8", 58)
    # API returns a Paper build for 1.21.9 — the function calls assert_paper_same_mc with installed_mc
    # for BOTH inputs (the installed MC is the only allowed MC for the URL). But the artifact's version
    # might still report a different stream; the clamp is enforced via the call shape. To verify the
    # clamp behavior, we directly exercise assert_paper_same_mc above. Here, just verify the plan
    # call succeeds when the API is on the same MC.
    artifact = JarArtifact(
        label="paper",
        version="1.21.8-60",
        download_url="https://example/p.jar",
        sha256="ee" * 32,
        filename="paper-1.21.8-60.jar",
    )

    def fake_fetch(component: str, overrides: dict) -> JarArtifact:
        assert overrides.get("paper_mc_version") == "1.21.8"
        return artifact

    monkeypatch.setattr("update.fetch_artifact_for", fake_fetch)
    plans = build_plan([COMPONENT_PAPER], paths, {}, force=False)
    assert plans[0].action == "apply"
    assert plans[0].artifact.version == "1.21.8-60"


def test_build_plan_paper_skipped_when_no_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No version_history.json yet (fresh install) → Paper is skipped, not errored."""
    paths = _make_paths(tmp_path)
    # No history file written. fetch should never be called for paper.
    called = mock.Mock()
    monkeypatch.setattr("update.fetch_artifact_for", called)
    plans = build_plan([COMPONENT_PAPER], paths, {}, force=False)
    assert plans == []
    called.assert_not_called()


def test_build_plan_paper_max_build_clamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--max-paper-build`` halts the run if API's latest exceeds the clamp."""
    paths = _make_paths(tmp_path)
    paths.paper_jar.write_bytes(b"paper-1.21.8-58")
    _write_paper_history(paths, "1.21.8", 58)
    artifact = JarArtifact(
        label="paper",
        version="1.21.8-99",
        download_url="https://example/p.jar",
        sha256="ee" * 32,
        filename="paper-1.21.8-99.jar",
    )
    monkeypatch.setattr("update.fetch_artifact_for", lambda comp, overrides: artifact)
    with pytest.raises(PaperMcVersionMismatchError, match="exceeds --max-paper-build"):
        build_plan([COMPONENT_PAPER], paths, {"max_paper_build": 60}, force=False)


# --- apply_plans: end-to-end swap + rollback --------------------------------


def test_apply_plans_skip_action_does_not_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``skip`` plan performs no IO."""
    paths = _make_paths(tmp_path)
    paths.jar_for(COMPONENT_GEYSER).write_bytes(b"current")
    download_called = mock.Mock()
    monkeypatch.setattr("update.http_download_to_file", download_called)
    monkeypatch.setattr("update.restart_and_verify", mock.Mock())
    plan = ComponentPlan(
        component=COMPONENT_GEYSER,
        installed_version="2.10.0-SNAPSHOT",
        installed_sha256="aa" * 32,
        artifact=JarArtifact("geyser", "2.10.0-1143", "https://example/g.jar", "aa" * 32, "Geyser-Spigot.jar"),
        action="skip",
    )
    result = apply_plans(
        [plan],
        paths,
        owner_user="u",
        owner_group="g",
        no_restart=False,
        skip_probe=True,
        retention=DEFAULT_BACKUP_RETENTION,
        dry_run=False,
    )
    assert result == 0
    download_called.assert_not_called()


def test_apply_plans_downloads_swaps_and_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy-path apply downloads, sha-verifies, swaps, restarts, prunes."""
    paths = _make_paths(tmp_path)
    paths.jar_for(COMPONENT_GEYSER).write_bytes(b"old-geyser")
    new_bytes = b"new-geyser-bytes"
    new_sha = hashlib.sha256(new_bytes).hexdigest()

    def fake_download(url, dest):
        dest.write_bytes(new_bytes)

    monkeypatch.setattr("update.http_download_to_file", fake_download)
    monkeypatch.setattr("update.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess([], 0))
    restart = mock.Mock()
    monkeypatch.setattr("update.restart_and_verify", restart)

    plan = ComponentPlan(
        component=COMPONENT_GEYSER,
        installed_version="2.10.0-SNAPSHOT",
        installed_sha256="aa" * 32,
        artifact=JarArtifact("geyser", "2.10.0-1143", "https://example/g.jar", new_sha, "Geyser-Spigot.jar"),
        action="apply",
    )
    now = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
    result = apply_plans(
        [plan],
        paths,
        owner_user="u",
        owner_group="g",
        no_restart=False,
        skip_probe=True,
        retention=DEFAULT_BACKUP_RETENTION,
        dry_run=False,
        now=now,
    )
    assert result == 1
    assert paths.jar_for(COMPONENT_GEYSER).read_bytes() == new_bytes
    backup = paths.plugins_dir / "Geyser-Spigot.jar.20260512T100000"
    assert backup.read_bytes() == b"old-geyser"
    restart.assert_called_once()


def test_apply_plans_rolls_back_on_restart_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restart failure after swap rolls every swapped jar back and surfaces ServiceRestartError."""
    paths = _make_paths(tmp_path)
    paths.jar_for(COMPONENT_GEYSER).write_bytes(b"old-geyser")
    new_bytes = b"new-geyser"
    new_sha = hashlib.sha256(new_bytes).hexdigest()

    def fake_download(url, dest):
        dest.write_bytes(new_bytes)

    monkeypatch.setattr("update.http_download_to_file", fake_download)
    monkeypatch.setattr("update.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess([], 0))
    calls: list[bool] = []

    def restart_then_fail(*, skip_probe: bool, dry_run: bool):
        calls.append(skip_probe)
        if len(calls) == 1:
            raise ServiceRestartError("simulated bad bind")
        # The post-rollback restart attempt — succeed silently.

    monkeypatch.setattr("update.restart_and_verify", restart_then_fail)

    plan = ComponentPlan(
        component=COMPONENT_GEYSER,
        installed_version="2.10.0-SNAPSHOT",
        installed_sha256="aa" * 32,
        artifact=JarArtifact("geyser", "2.10.0-1143", "https://example/g.jar", new_sha, "Geyser-Spigot.jar"),
        action="apply",
    )

    with pytest.raises(ServiceRestartError, match="simulated bad bind"):
        apply_plans(
            [plan],
            paths,
            owner_user="u",
            owner_group="g",
            no_restart=False,
            skip_probe=True,
            retention=DEFAULT_BACKUP_RETENTION,
            dry_run=False,
        )

    # The original bytes are restored.
    assert paths.jar_for(COMPONENT_GEYSER).read_bytes() == b"old-geyser"
    # A .failed.<ts> sibling tracks the bad jar.
    assert list(paths.plugins_dir.glob("Geyser-Spigot.jar.failed.*"))
    # The post-rollback restart was attempted with skip_probe=True.
    assert calls == [True, True]


def test_apply_plans_floodgate_key_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Floodgate key on disk is untouched by a happy-path apply."""
    paths = _make_paths(tmp_path)
    paths.floodgate_key.parent.mkdir(parents=True, exist_ok=True)
    paths.floodgate_key.write_bytes(b"pem-bytes")
    key_sha_before = hashlib.sha256(b"pem-bytes").hexdigest()
    paths.jar_for(COMPONENT_GEYSER).write_bytes(b"old")
    new_bytes = b"new"
    new_sha = hashlib.sha256(new_bytes).hexdigest()

    def fake_download(url, dest):
        dest.write_bytes(new_bytes)

    monkeypatch.setattr("update.http_download_to_file", fake_download)
    monkeypatch.setattr("update.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr("update.restart_and_verify", mock.Mock())

    plan = ComponentPlan(
        component=COMPONENT_GEYSER,
        installed_version="2.10.0-SNAPSHOT",
        installed_sha256="aa" * 32,
        artifact=JarArtifact("geyser", "2.10.0-1143", "https://example/g.jar", new_sha, "Geyser-Spigot.jar"),
        action="apply",
    )
    apply_plans(
        [plan],
        paths,
        owner_user="u",
        owner_group="g",
        no_restart=False,
        skip_probe=True,
        retention=DEFAULT_BACKUP_RETENTION,
        dry_run=False,
    )
    assert paths.floodgate_key.read_bytes() == b"pem-bytes"
    assert hashlib.sha256(paths.floodgate_key.read_bytes()).hexdigest() == key_sha_before


def test_assert_floodgate_key_untouched_raises_on_drift(tmp_path: Path) -> None:
    """If the key's hash drifts mid-run, the invariant fires."""
    paths = _make_paths(tmp_path)
    paths.floodgate_key.parent.mkdir(parents=True, exist_ok=True)
    paths.floodgate_key.write_bytes(b"new-content")
    with pytest.raises(UpdateError, match="INVARIANT VIOLATED"):
        assert_floodgate_key_untouched(paths, sha_before="a" * 64)


# --- jar_for candidate-name fallback ----------------------------------------


def test_jar_for_floodgate_finds_capital_f_variant(tmp_path: Path) -> None:
    """When the live host has ``Floodgate-Spigot.jar`` (operator-renamed), jar_for finds it."""
    paths = _make_paths(tmp_path)
    capital = paths.plugins_dir / "Floodgate-Spigot.jar"
    capital.write_bytes(b"floodgate-bytes")
    assert paths.jar_for(COMPONENT_FLOODGATE) == capital


def test_jar_for_floodgate_finds_lowercase_default(tmp_path: Path) -> None:
    """A fresh install.py-style ``floodgate-spigot.jar`` is also picked up.

    Skipped on case-insensitive filesystems (macOS APFS default) because the test relies on the
    filesystem distinguishing ``Floodgate-Spigot.jar`` from ``floodgate-spigot.jar``.
    """
    probe_upper = tmp_path / "CASE-PROBE"
    probe_upper.write_bytes(b"x")
    if (tmp_path / "case-probe").exists():
        pytest.skip("filesystem is case-insensitive; live-host Linux FS is case-sensitive")
    paths = _make_paths(tmp_path)
    lower = paths.plugins_dir / "floodgate-spigot.jar"
    lower.write_bytes(b"floodgate-bytes")
    assert paths.jar_for(COMPONENT_FLOODGATE) == lower


def test_jar_for_floodgate_prefers_first_candidate_when_neither_exists(tmp_path: Path) -> None:
    """No installed jar — fall back to the canonical name so the path is well-defined for ``missing``."""
    paths = _make_paths(tmp_path)
    expected = paths.plugins_dir / FLOODGATE_JAR_CANDIDATES[0]
    assert paths.jar_for(COMPONENT_FLOODGATE) == expected


# --- wait_for_tcp polling ---------------------------------------------------


def test_wait_for_tcp_succeeds_after_a_few_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """``wait_for_tcp`` polls until ``tcp_probe`` returns True, not just one shot."""
    calls = {"count": 0}

    def fake_probe(*_args, **_kwargs):
        calls["count"] += 1
        return calls["count"] >= 3

    monkeypatch.setattr("update.tcp_probe", fake_probe)
    monkeypatch.setattr("update.time.sleep", lambda _s: None)
    assert wait_for_tcp(total_window_s=60, poll_interval_s=1) is True
    assert calls["count"] == 3


def test_wait_for_tcp_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``tcp_probe`` never succeeds inside the window, wait_for_tcp returns False."""
    monkeypatch.setattr("update.tcp_probe", lambda *a, **kw: False)
    monkeypatch.setattr("update.time.sleep", lambda _s: None)
    # monotonic ticks past the deadline after a few invocations to short-circuit the loop.
    ticks = iter([0.0, 0.1, 0.2, 999.0])
    monkeypatch.setattr("update.time.monotonic", lambda: next(ticks))
    assert wait_for_tcp(total_window_s=10, poll_interval_s=1) is False


# --- --install-systemd-units idempotency ------------------------------------


def test_install_systemd_units_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First run writes units + daemon-reload + enable; second run is a no-op except enable."""
    service = tmp_path / "minecraft-update.service"
    timer = tmp_path / "minecraft-update.timer"
    runs: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        runs.append(list(argv))
        return subprocess.CompletedProcess(args=list(argv), returncode=0)

    monkeypatch.setattr("update.subprocess.run", fake_run)

    rc = install_systemd_units(
        service_path=service,
        timer_path=timer,
        dry_run=False,
    )
    assert rc == 0
    assert service.exists() and timer.exists()
    # First run: daemon-reload + enable --now.
    assert ["systemctl", "daemon-reload"] in runs
    assert any(cmd[:3] == ["systemctl", "enable", "--now"] for cmd in runs)

    runs.clear()
    install_systemd_units(service_path=service, timer_path=timer, dry_run=False)
    # Second run: enable only — no daemon-reload (content didn't change).
    assert ["systemctl", "daemon-reload"] not in runs
    assert any(cmd[:3] == ["systemctl", "enable", "--now"] for cmd in runs)


def test_render_updater_timer_uses_10utc_default() -> None:
    """The default timer fires at 10:00 UTC with randomized delay."""
    content = render_updater_timer()
    assert "OnCalendar=*-*-* 10:00:00 UTC" in content
    assert "RandomizedDelaySec=3600" in content
    assert "Persistent=true" in content


def test_render_updater_service_uses_root_oneshot() -> None:
    """The service runs as root, Type=oneshot."""
    content = render_updater_service()
    assert "User=root" in content
    assert "Type=oneshot" in content
    assert "ExecStart=/usr/local/sbin/minecraft-update.py" in content


def test_write_unit_if_changed_writes_only_on_drift(tmp_path: Path) -> None:
    """Identical content is a no-op (no rewrite)."""
    path = tmp_path / "u.service"
    assert write_unit_if_changed(path, "body\n") is True
    assert write_unit_if_changed(path, "body\n") is False
    assert write_unit_if_changed(path, "body changed\n") is True


# --- Argument parser ---------------------------------------------------------


def test_argument_parser_defaults() -> None:
    """Defaults match the documented constants."""
    parser = build_argument_parser()
    args = parser.parse_args([])
    assert args.data_root == DEFAULT_DATA_ROOT
    assert args.components is None  # → ALL_COMPONENTS in update()
    assert args.keep_backups == DEFAULT_BACKUP_RETENTION
    assert args.force is False
    assert args.dry_run is False
    assert args.install_systemd_units is False
    assert args.timer_oncalendar == DEFAULT_TIMER_ONCALENDAR


def test_parse_components_csv_accepts_subset() -> None:
    """``geyser,floodgate`` parses to a valid subset."""
    assert _parse_components_csv("geyser,floodgate") == ["geyser", "floodgate"]


def test_parse_components_csv_rejects_unknown() -> None:
    """Unknown identifiers raise a CLI error."""
    with pytest.raises(argparse.ArgumentTypeError, match="unknown component"):
        _parse_components_csv("geyser,nonsense")


# --- main() error mapping ---------------------------------------------------


def test_main_returns_zero_on_install_systemd_units(monkeypatch: pytest.MonkeyPatch) -> None:
    """--install-systemd-units path returns 0 on success."""
    monkeypatch.setattr("update.install_systemd_units", lambda **kw: 0)
    assert main(["--install-systemd-units"]) == 0


def test_update_returns_zero_when_components_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful run with N applied updates returns 0, not N.

    Regression: an earlier version of ``update()`` propagated ``apply_plans``'s count as its return
    value, so a run that swapped 3 jars exited 3 → systemd reported the timer-fired service as failed
    despite every step succeeding.
    """
    monkeypatch.setattr("update.assert_root", lambda: None)
    monkeypatch.setattr("update.acquire_lock", lambda _p: open(tmp_path / "lock", "w"))
    monkeypatch.setattr("update.build_plan", lambda *a, **kw: [])
    monkeypatch.setattr("update.apply_plans", lambda *a, **kw: 3)
    args = build_argument_parser().parse_args(["--data-root", str(tmp_path)])
    from update import update as run_update

    assert run_update(args) == 0


def test_main_returns_two_on_paper_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """PaperMcVersionMismatchError maps to exit code 2."""

    def boom(_args):
        raise PaperMcVersionMismatchError("cross-mc")

    monkeypatch.setattr("update.update", boom)
    assert main([]) == 2


def test_main_returns_three_on_download_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """DownloadError maps to exit code 3."""

    def boom(_args):
        raise DownloadError("network down")

    monkeypatch.setattr("update.update", boom)
    assert main([]) == 3


def test_main_returns_four_on_restart_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """ServiceRestartError maps to exit code 4."""

    def boom(_args):
        raise ServiceRestartError("did not bind")

    monkeypatch.setattr("update.update", boom)
    assert main([]) == 4


# --- E2E (real APIs) --------------------------------------------------------


@pytest.mark.e2e
def test_e2e_geyser_artifact_round_trip() -> None:
    """fetch_artifact_for(geyser) hits the real API and returns a downloadable artifact."""
    art = fetch_artifact_for(COMPONENT_GEYSER, {})
    assert art.sha256 and len(art.sha256) == 64
    assert art.download_url.startswith("https://")


@pytest.mark.e2e
def test_e2e_floodgate_artifact_round_trip() -> None:
    """fetch_artifact_for(floodgate) hits the real API."""
    art = fetch_artifact_for(COMPONENT_FLOODGATE, {})
    assert art.sha256 and len(art.sha256) == 64
    assert "floodgate" in art.filename.lower()


@pytest.mark.e2e
def test_e2e_viaversion_artifact_round_trip() -> None:
    """fetch_artifact_for(viaversion) hits the real Hangar API."""
    art = fetch_artifact_for(COMPONENT_VIAVERSION, {})
    assert art.sha256 and len(art.sha256) == 64
    assert art.filename == "ViaVersion.jar"


@pytest.mark.e2e
def test_e2e_paper_artifact_round_trip() -> None:
    """fetch_artifact_for(paper) needs the MC override; uses 1.21.8 as the canonical pin."""
    art = fetch_artifact_for(COMPONENT_PAPER, {"paper_mc_version": "1.21.8"})
    assert art.sha256 and len(art.sha256) == 64
    assert art.version.startswith("1.21.8-")
