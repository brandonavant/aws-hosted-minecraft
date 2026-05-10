#!/usr/bin/env python3
"""Idempotent nightly updater for Paper + Geyser + Floodgate + ViaVersion.

Invocation (run on the live host as root, normally via the ``minecraft-update.timer`` systemd timer)::

    sudo /usr/local/sbin/minecraft-update.py
    sudo /usr/local/sbin/minecraft-update.py --dry-run
    sudo /usr/local/sbin/minecraft-update.py --components geyser,floodgate
    sudo /usr/local/sbin/minecraft-update.py --install-systemd-units

This script is the follow-up the install-time provisioner (``install.py``) explicitly deferred. Its raison
d'être is **Bedrock client connectivity preservation**: when Mojang ships a Bedrock client update (every 2–6
weeks, auto-rolled to every console / mobile player within hours), the server-side Geyser jar must move to
the matching protocol or every Bedrock player sees "Unable to connect to world" with no recourse. Java
players are unaffected.

Priority order, hardest to softest:

1. Geyser — connectivity-critical, ship aggressively on the version stream the API publishes.
2. Floodgate — track Geyser; the two share API surfaces and want to bump in lockstep.
3. Paper — patch-level only. **MC-version bumps are out of scope.** The updater clamps Paper to the running
   MC version's build stream and refuses to cross. To move MC versions, the operator re-runs ``install.py``
   with ``--mc-version``.
4. ViaVersion — track the latest Release-channel build from Hangar.

CRITICAL invariants:

- ``plugins/floodgate/key.pem`` is **never touched**. Same hard guarantee as ``install.py``; regenerating
  the key breaks Bedrock auth for every existing player.
- Paper is never bumped across MC versions, regardless of what the API reports as ``latest``.
- Every download is sha256-verified against the API's reported hash before being placed.
- On any failure mid-update (checksum miss, post-restart probe failure, etc.), the previous jar(s) are
  restored from timestamped backups and the service is restarted on the rollback.

The flow is sha256-driven: read the API's reported sha256, compare against the on-disk jar's hash, and only
swap when they differ. The version-comparison helpers exist primarily for human-readable log lines and the
Paper MC-version clamp; the actual update decision rides on the sha mismatch.

Stdlib only — the script runs as root under systemd with no pip available.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from _common import (
    FLOODGATE_API_BASE,
    GEYSER_API_BASE,
    ChecksumMismatchError,
    DownloadError,
    JarArtifact,
    fetch_geyser_like_artifact,
    fetch_paper_artifact,
    fetch_viaversion_artifact,
    http_download_to_file,
    read_mc_version_from_history,
    read_plugin_version,
    sha256_of_file,
)

# --- Constants ---------------------------------------------------------------

DEFAULT_DATA_ROOT = Path("/srv/minecraft")
SERVER_SUBDIR = "server"
PLUGINS_SUBDIR = "plugins"
PAPER_JAR_NAME = "paper.jar"

# Plugin jar filenames in operator-preference order. The first existing match wins on disk; when no
# candidate exists, the first entry is treated as the canonical "fresh install" filename. Floodgate
# carries two candidates because the upstream-default ``floodgate-spigot.jar`` and the
# operator-renamed ``Floodgate-Spigot.jar`` (case-matched to Geyser-Spigot.jar) both occur in the
# wild — the live host this repo targets uses the capital-F variant.
GEYSER_JAR_CANDIDATES: tuple[str, ...] = ("Geyser-Spigot.jar",)
FLOODGATE_JAR_CANDIDATES: tuple[str, ...] = ("Floodgate-Spigot.jar", "floodgate-spigot.jar")
VIAVERSION_JAR_CANDIDATES: tuple[str, ...] = ("ViaVersion.jar",)

DEFAULT_USER = "minecraft"
DEFAULT_GROUP = "minecraft"

DEFAULT_LOCK_PATH = Path("/run/minecraft-update.lock")
DEFAULT_BACKUP_RETENTION = 3

MINECRAFT_SERVICE_NAME = "minecraft.service"
RESTART_WAIT_TIMEOUT_S = 120
RESTART_POLL_INTERVAL_S = 2
TCP_PROBE_HOST = "127.0.0.1"
TCP_PROBE_PORT = 25565
TCP_PROBE_TIMEOUT_S = 5
TCP_PROBE_GRACE_S = 5
# Paper's cold-restart binds 25565 well after systemd's ``is-active`` flips to active — Type=simple
# only requires the JVM to be running, not for the server to be accepting connections. Empirically
# 20–60s from restart to bind. Poll up to 180s total so even a heavy-world load completes inside the
# window. Anything slower than 180s is a real outage, not a slow boot.
TCP_PROBE_TOTAL_WINDOW_S = 180
TCP_PROBE_POLL_INTERVAL_S = 3

UPDATER_SERVICE_PATH = Path("/etc/systemd/system/minecraft-update.service")
UPDATER_TIMER_PATH = Path("/etc/systemd/system/minecraft-update.timer")
UPDATER_TIMER_NAME = "minecraft-update.timer"
UPDATER_SCRIPT_INSTALL_PATH = Path("/usr/local/sbin/minecraft-update.py")

# 10:00 UTC = ~5 AM CDT / 4 AM CST — genuine off-peak for the player base.
# DO NOT default to 04:00 UTC: that's ~11 PM CDT (peak late-night gaming).
DEFAULT_TIMER_ONCALENDAR = "*-*-* 10:00:00 UTC"
DEFAULT_TIMER_RANDOMIZED_DELAY = "3600"

UPDATER_SERVICE_TEMPLATE = """\
[Unit]
Description=Minecraft Paper + plugins updater (Geyser / Floodgate / Paper / ViaVersion)
Documentation=https://github.com/brandonavant/aws-hosted-minecraft
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={script_path}
User=root
"""

UPDATER_TIMER_TEMPLATE = """\
[Unit]
Description=Run minecraft-update.service daily during off-peak hours
Documentation=https://github.com/brandonavant/aws-hosted-minecraft

[Timer]
OnCalendar={oncalendar}
RandomizedDelaySec={randomized_delay}
Persistent=true
Unit=minecraft-update.service

[Install]
WantedBy=timers.target
"""

# Component identifiers. Order matters for the apply phase — Geyser first because it's the
# connectivity-critical bump; Floodgate immediately after (they share auth state). Paper and ViaVersion
# can move in either order.
COMPONENT_GEYSER = "geyser"
COMPONENT_FLOODGATE = "floodgate"
COMPONENT_PAPER = "paper"
COMPONENT_VIAVERSION = "viaversion"
ALL_COMPONENTS: tuple[str, ...] = (COMPONENT_GEYSER, COMPONENT_FLOODGATE, COMPONENT_PAPER, COMPONENT_VIAVERSION)

BACKUP_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"
_BACKUP_TIMESTAMP_RE = re.compile(r"\.(\d{8}T\d{6})$")
_SEMVER_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)*)")
_FLOODGATE_BUILD_RE = re.compile(r"\(b(\d+)-")
_TRAILING_DASH_INT_RE = re.compile(r"-(\d+)$")


# --- Exceptions --------------------------------------------------------------


class UpdateError(RuntimeError):
    """Base class for updater errors. Caught at the CLI boundary."""


class PreflightError(UpdateError):
    """Raised when a pre-flight assertion fails (root, lock)."""


class PaperMcVersionMismatchError(UpdateError):
    """Raised when the API's latest Paper build is for a different MC version than the running one."""


class ServiceRestartError(UpdateError):
    """Raised when systemctl restart or the post-restart probe fails."""


class LockAcquisitionError(UpdateError):
    """Raised when the lock file is held by another updater run."""


# --- Datatypes ---------------------------------------------------------------


_PLUGIN_JAR_CANDIDATES: dict[str, tuple[str, ...]] = {
    COMPONENT_GEYSER: GEYSER_JAR_CANDIDATES,
    COMPONENT_FLOODGATE: FLOODGATE_JAR_CANDIDATES,
    COMPONENT_VIAVERSION: VIAVERSION_JAR_CANDIDATES,
}


@dataclass(frozen=True)
class UpdatePaths:
    """Resolved on-disk locations the updater operates against.

    Attributes:
        data_root: Mountpoint of the data volume (default ``/srv/minecraft``).
        server_dir: Paper server directory.
        plugins_dir: Plugins directory.
        paper_jar: Paper jar path (always ``<server_dir>/paper.jar`` — install.py renames on placement).
        floodgate_key: ``plugins/floodgate/key.pem`` — read-only invariant: NEVER touched by the updater.
    """

    data_root: Path
    server_dir: Path
    plugins_dir: Path
    paper_jar: Path
    floodgate_key: Path

    @classmethod
    def from_data_root(cls, data_root: Path) -> "UpdatePaths":
        """Build an ``UpdatePaths`` from the volume root.

        Args:
            data_root: Mountpoint of the data volume.

        Returns:
            A fully-populated ``UpdatePaths``.
        """
        server_dir = data_root / SERVER_SUBDIR
        plugins_dir = server_dir / PLUGINS_SUBDIR
        return cls(
            data_root=data_root,
            server_dir=server_dir,
            plugins_dir=plugins_dir,
            paper_jar=server_dir / PAPER_JAR_NAME,
            floodgate_key=plugins_dir / "floodgate" / "key.pem",
        )

    def jar_for(self, component: str) -> Path:
        """Return the on-disk jar path for ``component``.

        For plugins, picks the first candidate filename that exists on disk; falls back to the first
        candidate (the canonical fresh-install name) when none exist, so the path is still well-defined
        for the ``"missing"`` plan branch.

        Args:
            component: One of ``COMPONENT_*`` identifiers.

        Returns:
            The jar's absolute path.

        Raises:
            ValueError: When ``component`` is unrecognized.
        """
        if component == COMPONENT_PAPER:
            return self.paper_jar
        candidates = _PLUGIN_JAR_CANDIDATES.get(component)
        if candidates is None:
            raise ValueError(f"unknown component {component!r}")
        for name in candidates:
            candidate_path = self.plugins_dir / name
            if candidate_path.exists():
                return candidate_path
        return self.plugins_dir / candidates[0]


@dataclass(frozen=True)
class ComponentPlan:
    """One component's update plan for this run.

    Attributes:
        component: Component identifier.
        installed_version: Human-readable installed version string for log lines (e.g. ``"1.21.8-58"``,
            ``"2.10.0-SNAPSHOT"``). ``None`` when no installed jar exists or version is unreadable.
        installed_sha256: sha256 hex digest of the installed jar, or ``None`` when no jar exists.
        artifact: API-reported latest artifact metadata.
        action: ``"apply"`` (sha differs), ``"skip"`` (sha matches), or ``"missing"`` (no installed jar —
            covered by ``install.py``, skipped here with a warning).
    """

    component: str
    installed_version: str | None
    installed_sha256: str | None
    artifact: JarArtifact
    action: str


# --- Logging -----------------------------------------------------------------


def log(message: str) -> None:
    """Emit a single log line to stdout (systemd journals via the service unit).

    Args:
        message: The line to print. Newline appended automatically.
    """
    print(message, flush=True)


# --- Pre-flight checks -------------------------------------------------------


def assert_root() -> None:
    """Refuse to proceed unless the process is running as UID 0.

    Raises:
        PreflightError: When ``geteuid()`` is non-zero.
    """
    if os.geteuid() != 0:
        raise PreflightError("minecraft-update.py must run as root (re-invoke with sudo)")


# --- Lock-file handling ------------------------------------------------------


def acquire_lock(lock_path: Path) -> "object":
    """Acquire an exclusive non-blocking ``flock`` on ``lock_path``.

    The lock is held for the lifetime of the returned file handle. Closing the handle releases the lock.

    Args:
        lock_path: Path to the lock file. Created if absent.

    Returns:
        The open file handle holding the lock. Caller closes when done.

    Raises:
        LockAcquisitionError: When the lock is already held by another process.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115 — caller manages lifetime.
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise LockAcquisitionError(f"another updater run holds {lock_path}: {exc}") from exc
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


# --- Version-comparison helpers (used for log lines + Paper clamp) ----------


def parse_semver_prefix(version_string: str) -> tuple[int, ...] | None:
    """Extract the leading dotted-integer (semver-ish) prefix from a version string.

    Handles real plugin.yml shapes:

    - ``"2.10.0-SNAPSHOT"`` → ``(2, 10, 0)``
    - ``"2.2.5-SNAPSHOT (b132-5a72b6a)"`` → ``(2, 2, 5)``
    - ``"5.9.1"`` → ``(5, 9, 1)``

    And API artifact-version shapes:

    - ``"2.10.0-1143"`` → ``(2, 10, 0)``
    - ``"1.21.8-60"`` → ``(1, 21, 8)``

    Args:
        version_string: Raw version string.

    Returns:
        Tuple of integer components, or ``None`` when no leading semver-ish prefix is present.
    """
    match = _SEMVER_PREFIX_RE.match(version_string)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def parse_floodgate_build_suffix(version_string: str) -> int | None:
    """Extract the build number from Floodgate's ``(b<N>-<sha>)`` plugin.yml suffix.

    Floodgate's plugin.yml carries a trailing parenthesized build descriptor like
    ``"2.2.5-SNAPSHOT (b132-5a72b6a)"``. Geyser and ViaVersion do not.

    Args:
        version_string: Raw version string from ``read_plugin_version``.

    Returns:
        The integer build number when the ``(b<N>-`` pattern is present, otherwise ``None``.
    """
    match = _FLOODGATE_BUILD_RE.search(version_string)
    return int(match.group(1)) if match else None


def parse_trailing_dash_int(version_string: str) -> int | None:
    """Extract the trailing ``-<int>`` from an API artifact version string.

    API ``artifact.version`` values for Geyser/Floodgate/Paper end in ``-<build>`` (e.g. ``"2.10.0-1143"``).
    ViaVersion does not (its version is a plain Hangar name like ``"5.9.1"``).

    Args:
        version_string: API artifact version string.

    Returns:
        The trailing integer, or ``None`` when no ``-<digits>`` suffix is present.
    """
    match = _TRAILING_DASH_INT_RE.search(version_string)
    return int(match.group(1)) if match else None


def is_paper_build_newer(installed_build: int, api_build: int) -> bool:
    """Paper integer-build comparison.

    Args:
        installed_build: The currently-running Paper build.
        api_build: The API's latest Paper build for the same MC version.

    Returns:
        True iff ``api_build > installed_build``.
    """
    return api_build > installed_build


def assert_paper_same_mc(
    installed_mc: str, api_mc: str, *, max_build: int | None = None, api_build: int | None = None
) -> None:
    """Refuse to apply a Paper update that crosses MC versions or exceeds ``max_build``.

    Args:
        installed_mc: MC version currently recorded in ``version_history.json``.
        api_mc: MC version the API's latest Paper build belongs to.
        max_build: Optional ``--max-paper-build`` clamp.
        api_build: The API's latest Paper build (required when ``max_build`` is set).

    Raises:
        PaperMcVersionMismatchError: When the MC versions differ or when ``api_build`` exceeds ``max_build``.
    """
    if installed_mc != api_mc:
        raise PaperMcVersionMismatchError(
            f"refusing Paper cross-MC bump: installed MC {installed_mc} vs API latest MC {api_mc}. "
            f"Run install.py with --mc-version to move MC versions explicitly."
        )
    if max_build is not None and api_build is not None and api_build > max_build:
        raise PaperMcVersionMismatchError(f"refusing Paper build {api_build}: exceeds --max-paper-build {max_build}")


def is_semver_update_available(installed_version: str, api_version: str) -> bool:
    """Compare semver prefixes; for equal prefixes, compare trailing build numbers when both are known.

    Used to decide direction (``"X → Y"`` log line) and to flag the rare case where the API's reported
    "latest" is somehow strictly older than what is installed (treated as no-op).

    Args:
        installed_version: Plugin's installed version string (from plugin.yml).
        api_version: API artifact version string.

    Returns:
        True when the API's version is strictly newer than installed (semver or trailing build), False
        when equal-or-older or when comparison is indeterminate.
    """
    installed_semver = parse_semver_prefix(installed_version)
    api_semver = parse_semver_prefix(api_version)
    if installed_semver is None or api_semver is None:
        return False
    if api_semver != installed_semver:
        return api_semver > installed_semver
    api_build = parse_trailing_dash_int(api_version)
    installed_build = parse_floodgate_build_suffix(installed_version)
    if api_build is not None and installed_build is not None:
        return api_build > installed_build
    return False


# --- API + on-disk reconciliation -------------------------------------------


def fetch_artifact_for(component: str, cli_overrides: dict[str, object]) -> JarArtifact:
    """Fetch the API's latest artifact metadata for ``component``, honoring optional pin overrides.

    Args:
        component: Component identifier.
        cli_overrides: A dict of overrides keyed by CLI flag name (e.g.
            ``{"paper_mc_version": "1.21.8", "geyser_version": None, ...}``).

    Returns:
        A populated ``JarArtifact``.

    Raises:
        DownloadError: When the API call fails.
        ValueError: When ``component`` is unrecognized.
    """
    if component == COMPONENT_PAPER:
        mc_version = cli_overrides.get("paper_mc_version")
        if not isinstance(mc_version, str):
            raise ValueError("fetch_artifact_for(paper) requires paper_mc_version in cli_overrides")
        return fetch_paper_artifact(mc_version, "latest")
    if component == COMPONENT_GEYSER:
        return fetch_geyser_like_artifact(COMPONENT_GEYSER, GEYSER_API_BASE, version=None, build=None)
    if component == COMPONENT_FLOODGATE:
        return fetch_geyser_like_artifact(COMPONENT_FLOODGATE, FLOODGATE_API_BASE, version=None, build=None)
    if component == COMPONENT_VIAVERSION:
        return fetch_viaversion_artifact(None)
    raise ValueError(f"unknown component {component!r}")


def installed_version_for(component: str, paths: UpdatePaths) -> str | None:
    """Read the installed version string for ``component`` from disk.

    Args:
        component: Component identifier.
        paths: Resolved on-disk paths.

    Returns:
        A human-readable version string (e.g. ``"1.21.8-58"`` for Paper, ``"2.10.0-SNAPSHOT"`` for plugins),
        or ``None`` when the source is missing / unreadable.
    """
    if component == COMPONENT_PAPER:
        history = read_mc_version_from_history(paths.server_dir)
        if history is None:
            return None
        mc, build = history
        return f"{mc}-{build}"
    jar = paths.jar_for(component)
    if not jar.exists():
        return None
    return read_plugin_version(jar)


# --- Planning ----------------------------------------------------------------


def build_plan(
    components: Sequence[str],
    paths: UpdatePaths,
    cli_overrides: dict[str, object],
    *,
    force: bool,
) -> list[ComponentPlan]:
    """Return one ``ComponentPlan`` per requested component.

    For each component, this resolves the API's latest artifact, hashes the installed jar (if any), and
    decides whether the update applies. For Paper specifically, the MC-version clamp is enforced here so
    a cross-MC API ``latest`` halts the run before any swap.

    Args:
        components: Components to plan for. Subset of ``ALL_COMPONENTS``.
        paths: Resolved on-disk paths.
        cli_overrides: CLI flag overrides (see ``fetch_artifact_for``).
        force: When true, every component plans an ``"apply"`` even if sha256 matches.

    Returns:
        A list of plans in the order ``components`` was given.

    Raises:
        PaperMcVersionMismatchError: When the API's latest Paper crosses MC versions or exceeds
            ``--max-paper-build``.
        DownloadError: From the API fetch.
    """
    plans: list[ComponentPlan] = []
    for component in components:
        installed_version = installed_version_for(component, paths)
        jar = paths.jar_for(component)
        installed_sha = sha256_of_file(jar) if jar.exists() else None

        if component == COMPONENT_PAPER:
            history = read_mc_version_from_history(paths.server_dir)
            if history is None:
                log(f"[skip] paper: no version_history.json yet (server hasn't booted) — try again tomorrow")
                continue
            installed_mc, _installed_build = history
            cli_overrides_with_mc = {**cli_overrides, "paper_mc_version": installed_mc}
            artifact = fetch_artifact_for(component, cli_overrides_with_mc)
            api_build = parse_trailing_dash_int(artifact.version)
            assert_paper_same_mc(
                installed_mc=installed_mc,
                api_mc=installed_mc,
                max_build=(
                    cli_overrides.get("max_paper_build")
                    if isinstance(cli_overrides.get("max_paper_build"), int)
                    else None
                ),
                api_build=api_build,
            )
        else:
            artifact = fetch_artifact_for(component, cli_overrides)

        if not jar.exists():
            action = "missing"
        elif force or installed_sha != artifact.sha256:
            action = "apply"
        else:
            action = "skip"

        plans.append(
            ComponentPlan(
                component=component,
                installed_version=installed_version,
                installed_sha256=installed_sha,
                artifact=artifact,
                action=action,
            )
        )
    return plans


# --- Atomic swap + backup ----------------------------------------------------


def staging_path_for(jar: Path) -> Path:
    """Return the staging path used for a fresh download before the atomic swap.

    Args:
        jar: Final destination jar.

    Returns:
        ``<jar>.new`` in the same directory.
    """
    return jar.with_suffix(jar.suffix + ".new")


def timestamped_backup_path(jar: Path, *, now: datetime | None = None) -> Path:
    """Return the timestamped backup path for ``jar``.

    Args:
        jar: Jar being backed up.
        now: Override the timestamp (for tests). Defaults to current UTC time.

    Returns:
        ``<jar>.<UTC timestamp>``.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    return jar.with_suffix(jar.suffix + "." + now.strftime(BACKUP_TIMESTAMP_FORMAT))


def list_backups(jar: Path) -> list[Path]:
    """List timestamped backups of ``jar`` in chronological order.

    Args:
        jar: Jar whose siblings to scan.

    Returns:
        List of backup paths sorted oldest-first.
    """
    if not jar.parent.is_dir():
        return []
    prefix = jar.name + "."
    matches: list[Path] = []
    for sibling in jar.parent.iterdir():
        if not sibling.name.startswith(prefix):
            continue
        suffix = sibling.name[len(prefix) :]
        if _BACKUP_TIMESTAMP_RE.fullmatch("." + suffix):
            matches.append(sibling)
    matches.sort(key=lambda p: p.name)
    return matches


def prune_backups(jar: Path, retention: int) -> list[Path]:
    """Delete the oldest backups beyond ``retention``.

    Args:
        jar: Jar whose backups to prune.
        retention: How many most-recent backups to keep.

    Returns:
        The list of removed backup paths.
    """
    backups = list_backups(jar)
    excess = backups[: max(0, len(backups) - retention)]
    for old in excess:
        log(f"[prune] backup {old.name}")
        old.unlink()
    return excess


def download_to_staging(artifact: JarArtifact, dest_jar: Path) -> Path:
    """Download ``artifact`` to the staging path and sha256-verify before returning.

    Args:
        artifact: API metadata.
        dest_jar: Final jar path (the staging path is derived from it).

    Returns:
        The staging path holding verified bytes.

    Raises:
        ChecksumMismatchError: When the downloaded bytes do not match ``artifact.sha256``. The staging
            file is removed.
        DownloadError: When the HTTP fetch fails.
    """
    staging = staging_path_for(dest_jar)
    staging.parent.mkdir(parents=True, exist_ok=True)
    if staging.exists():
        staging.unlink()
    log(f"[download] {artifact.label} {artifact.version} → {staging.name}")
    http_download_to_file(artifact.download_url, staging)
    observed = sha256_of_file(staging)
    if observed != artifact.sha256:
        staging.unlink()
        raise ChecksumMismatchError(
            f"{artifact.label} {artifact.version}: expected sha256 {artifact.sha256}, got {observed}"
        )
    return staging


def atomic_swap(jar: Path, staging: Path, *, owner_user: str, owner_group: str, now: datetime | None = None) -> Path:
    """Atomically swap ``jar`` for ``staging``, leaving a timestamped backup of the previous jar.

    Pre-condition: ``jar`` exists and ``staging`` exists. The directory entry for ``jar`` is renamed to
    a timestamped backup, then ``staging`` is renamed into place. Ownership is applied to the new file.

    Args:
        jar: Final jar path.
        staging: Verified replacement contents.
        owner_user: Owning user (chown applied to swapped-in jar).
        owner_group: Owning group.
        now: Timestamp override (for tests).

    Returns:
        The backup path the old jar was moved to.
    """
    backup = timestamped_backup_path(jar, now=now)
    log(f"[swap] {jar.name} → {backup.name}; {staging.name} → {jar.name}")
    jar.rename(backup)
    staging.rename(jar)
    _chown(jar, owner_user, owner_group)
    return backup


def rollback_swap(jar: Path, backup: Path) -> None:
    """Undo an ``atomic_swap`` by moving ``backup`` back into ``jar``'s position.

    If ``jar`` is present, it is moved aside to ``<jar>.failed.<timestamp>`` so the operator can inspect
    why the rollback was needed.

    Args:
        jar: The jar that was swapped in (now considered failed).
        backup: The pre-swap backup path.
    """
    if jar.exists():
        failed = jar.with_suffix(
            jar.suffix + ".failed." + datetime.now(tz=timezone.utc).strftime(BACKUP_TIMESTAMP_FORMAT)
        )
        log(f"[rollback] {jar.name} → {failed.name} (suspect)")
        jar.rename(failed)
    log(f"[rollback] {backup.name} → {jar.name}")
    backup.rename(jar)


def _chown(path: Path, owner_user: str, owner_group: str) -> None:
    """Run ``chown <owner_user>:<owner_group> <path>`` via subprocess.

    Args:
        path: Target path.
        owner_user: Owning user.
        owner_group: Owning group.
    """
    subprocess.run(  # noqa: S603 — argv as list, no shell.
        ["chown", f"{owner_user}:{owner_group}", str(path)],
        check=True,
    )


# --- Service lifecycle -------------------------------------------------------


def systemctl_restart(unit: str, *, dry_run: bool = False) -> None:
    """Restart a systemd unit.

    Args:
        unit: Unit name.
        dry_run: When true, log without running.
    """
    _run(["systemctl", "restart", unit], dry_run=dry_run)


def systemctl_is_active(unit: str) -> bool:
    """Return whether ``systemctl is-active --quiet <unit>`` reports active.

    Args:
        unit: Unit name.

    Returns:
        True when ``systemctl is-active`` exits 0.
    """
    proc = subprocess.run(  # noqa: S603 — argv as list, no shell.
        ["systemctl", "is-active", "--quiet", unit],
        check=False,
    )
    return proc.returncode == 0


def wait_for_active(unit: str, timeout_s: int = RESTART_WAIT_TIMEOUT_S, poll_s: int = RESTART_POLL_INTERVAL_S) -> bool:
    """Poll ``systemctl is-active`` until the unit is active or the timeout elapses.

    Args:
        unit: Unit name.
        timeout_s: Maximum seconds to wait.
        poll_s: Seconds between polls.

    Returns:
        True when the unit became active inside the window, False on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if systemctl_is_active(unit):
            return True
        time.sleep(poll_s)
    return systemctl_is_active(unit)


def tcp_probe(host: str = TCP_PROBE_HOST, port: int = TCP_PROBE_PORT, timeout_s: float = TCP_PROBE_TIMEOUT_S) -> bool:
    """Attempt a single TCP connect to ``(host, port)``; return whether the connect succeeded.

    UDP 19132 (Bedrock) is intentionally not probed — UDP is connectionless and a probe would only verify
    that the kernel accepts the socket, not that Geyser is actually serving. The TCP 25565 probe + the
    ``systemctl is-active`` check are sufficient as a smoke test.

    Args:
        host: Target host (default loopback).
        port: Target port (default Java 25565).
        timeout_s: Connect timeout.

    Returns:
        True on a successful connect, False on any socket error.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def wait_for_tcp(
    host: str = TCP_PROBE_HOST,
    port: int = TCP_PROBE_PORT,
    total_window_s: int = TCP_PROBE_TOTAL_WINDOW_S,
    poll_interval_s: int = TCP_PROBE_POLL_INTERVAL_S,
    per_attempt_timeout_s: float = TCP_PROBE_TIMEOUT_S,
) -> bool:
    """Poll ``tcp_probe`` until it succeeds or ``total_window_s`` elapses.

    Paper's cold restart leaves the JVM "active" per systemd well before 25565 is bound — empirically
    20–60s of world load + plugin enable happens after ``is-active`` flips. A one-shot probe right after
    is-active is too aggressive and causes false-positive rollbacks. Polling absorbs the variance.

    Args:
        host: Target host.
        port: Target port.
        total_window_s: Max seconds to keep polling before giving up.
        poll_interval_s: Delay between attempts.
        per_attempt_timeout_s: Per-attempt connect timeout (passed through to ``tcp_probe``).

    Returns:
        True when the port opens inside the window, False on timeout.
    """
    deadline = time.monotonic() + total_window_s
    while True:
        if tcp_probe(host, port, timeout_s=per_attempt_timeout_s):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval_s)


def restart_and_verify(unit: str = MINECRAFT_SERVICE_NAME, *, skip_probe: bool = False, dry_run: bool = False) -> None:
    """Restart ``unit`` and verify it comes back up via systemd + TCP probe.

    Args:
        unit: Service unit name.
        skip_probe: When true, skip the TCP probe (still wait for is-active).
        dry_run: When true, log the planned restart without running it.

    Raises:
        ServiceRestartError: When the unit fails to become active inside ``RESTART_WAIT_TIMEOUT_S``, or
            when the TCP probe never succeeds inside ``TCP_PROBE_TOTAL_WINDOW_S``.
    """
    systemctl_restart(unit, dry_run=dry_run)
    if dry_run:
        return
    if not wait_for_active(unit):
        raise ServiceRestartError(f"{unit} did not become active within {RESTART_WAIT_TIMEOUT_S}s")
    log(f"[ok] {unit} is active")
    if skip_probe:
        return
    time.sleep(TCP_PROBE_GRACE_S)
    log(f"[probe] polling TCP {TCP_PROBE_HOST}:{TCP_PROBE_PORT} for up to {TCP_PROBE_TOTAL_WINDOW_S}s")
    if not wait_for_tcp():
        raise ServiceRestartError(
            f"{unit} reports active but TCP {TCP_PROBE_HOST}:{TCP_PROBE_PORT} did not open within "
            f"{TCP_PROBE_TOTAL_WINDOW_S}s"
        )
    log(f"[ok] tcp probe {TCP_PROBE_HOST}:{TCP_PROBE_PORT} succeeded")


# --- Floodgate key invariant -------------------------------------------------


def assert_floodgate_key_untouched(paths: UpdatePaths, sha_before: str | None) -> None:
    """Assert ``plugins/floodgate/key.pem`` was not modified during the run.

    The updater never reads, writes, or touches the key. This assertion is paranoia — it catches
    accidental sibling-glob bugs that would otherwise destroy Bedrock auth silently.

    Args:
        paths: Resolved on-disk paths.
        sha_before: The key's sha256 captured at the start of the run, or ``None`` when no key existed.

    Raises:
        UpdateError: When the key's hash changed (or it gained/lost existence).
    """
    sha_after = sha256_of_file(paths.floodgate_key) if paths.floodgate_key.exists() else None
    if sha_before != sha_after:
        raise UpdateError(
            f"INVARIANT VIOLATED: floodgate {paths.floodgate_key} hash changed "
            f"({sha_before} → {sha_after}) during the updater run. Manual recovery required — the key "
            f"must NEVER be touched by the updater."
        )


# --- Apply phase -------------------------------------------------------------


def apply_plans(
    plans: Sequence[ComponentPlan],
    paths: UpdatePaths,
    *,
    owner_user: str,
    owner_group: str,
    no_restart: bool,
    skip_probe: bool,
    retention: int,
    dry_run: bool,
    now: datetime | None = None,
) -> int:
    """Execute the plans: download, swap, restart, verify, prune. Roll back on failure.

    Args:
        plans: Output of ``build_plan``.
        paths: Resolved on-disk paths.
        owner_user: Service-account user (applied to swapped-in jars).
        owner_group: Service-account group.
        no_restart: When true, skip the post-swap service restart entirely.
        skip_probe: When true, skip the TCP probe after restart.
        retention: Backup retention (per component).
        dry_run: When true, log the plan without performing it.
        now: Timestamp override (for tests).

    Returns:
        Number of components that actually updated.

    Raises:
        ChecksumMismatchError: Surfaced from download verification.
        ServiceRestartError: When the post-restart smoke check fails (after rollback).
        DownloadError: From the API/jar fetch.
    """
    for plan in plans:
        descriptor = plan.installed_version or "(none)"
        if plan.action == "skip":
            log(f"[skip] {plan.component} {descriptor} up-to-date (sha {plan.artifact.sha256[:12]}…)")
        elif plan.action == "missing":
            log(f"[skip] {plan.component} jar missing on disk — run install.py to place it; skipping")
        else:
            log(
                f"[plan] {plan.component} {descriptor} → {plan.artifact.version} "
                f"(sha {(plan.installed_sha256 or 'none')[:12]}… → {plan.artifact.sha256[:12]}…)"
            )

    to_apply = [p for p in plans if p.action == "apply"]
    if not to_apply:
        log("[ok] nothing to update")
        return 0

    if dry_run:
        log(f"[dry-run] would update {len(to_apply)} component(s); skipping download/swap/restart")
        return 0

    floodgate_key_sha = sha256_of_file(paths.floodgate_key) if paths.floodgate_key.exists() else None

    staged: list[tuple[ComponentPlan, Path]] = []
    swapped: list[tuple[ComponentPlan, Path, Path]] = []  # (plan, jar, backup)

    try:
        for plan in to_apply:
            jar = paths.jar_for(plan.component)
            staging = download_to_staging(plan.artifact, jar)
            staged.append((plan, staging))

        for plan, staging in staged:
            jar = paths.jar_for(plan.component)
            backup = atomic_swap(jar, staging, owner_user=owner_user, owner_group=owner_group, now=now)
            swapped.append((plan, jar, backup))

        if not no_restart:
            try:
                restart_and_verify(skip_probe=skip_probe, dry_run=False)
            except ServiceRestartError:
                _rollback_all(swapped)
                # Best-effort restart on the restored jars so the server is up regardless.
                try:
                    restart_and_verify(skip_probe=True, dry_run=False)
                except ServiceRestartError as exc2:
                    log(f"[error] post-rollback restart also failed: {exc2}")
                raise

        for plan, jar, _backup in swapped:
            prune_backups(jar, retention)
        for plan in to_apply:
            jar = paths.jar_for(plan.component)
            log(f"[done] {plan.component} {plan.installed_version or '(none)'} → {plan.artifact.version}")
    finally:
        for _plan, staging in staged:
            if staging.exists():
                # If a staging file is still around at this point, the swap never happened — clean it up.
                staging.unlink()

    assert_floodgate_key_untouched(paths, floodgate_key_sha)
    return len(swapped)


def _rollback_all(swapped: Sequence[tuple[ComponentPlan, Path, Path]]) -> None:
    """Roll back every swap in reverse order.

    Args:
        swapped: List of ``(plan, jar, backup)`` tuples from a successful swap phase.
    """
    for plan, jar, backup in reversed(list(swapped)):
        log(f"[rollback] {plan.component} {plan.artifact.version} → {plan.installed_version or '(none)'}")
        rollback_swap(jar, backup)


# --- Systemd unit install (--install-systemd-units) -------------------------


def render_updater_service(script_path: Path = UPDATER_SCRIPT_INSTALL_PATH) -> str:
    """Render the ``minecraft-update.service`` unit content.

    Args:
        script_path: Path to the installed updater script.

    Returns:
        Rendered unit content.
    """
    return UPDATER_SERVICE_TEMPLATE.format(script_path=script_path)


def render_updater_timer(
    oncalendar: str = DEFAULT_TIMER_ONCALENDAR, randomized_delay: str = DEFAULT_TIMER_RANDOMIZED_DELAY
) -> str:
    """Render the ``minecraft-update.timer`` unit content.

    Args:
        oncalendar: ``OnCalendar=`` expression.
        randomized_delay: ``RandomizedDelaySec=`` value.

    Returns:
        Rendered timer content.
    """
    return UPDATER_TIMER_TEMPLATE.format(oncalendar=oncalendar, randomized_delay=randomized_delay)


def write_unit_if_changed(path: Path, content: str, *, dry_run: bool = False) -> bool:
    """Write ``content`` to ``path`` only if the existing file differs.

    Args:
        path: Target unit path.
        content: Desired content.
        dry_run: When true, log without writing.

    Returns:
        True when the file was written (or would be in dry-run), False when content matched.
    """
    desired = content.encode("utf-8")
    if path.exists() and path.read_bytes() == desired:
        log(f"[skip] {path} already matches desired content")
        return False
    if dry_run:
        log(f"[dry-run] write {len(desired)} bytes to {path}")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(desired)
    path.chmod(0o644)
    log(f"[write] {path}")
    return True


def install_systemd_units(
    *,
    oncalendar: str = DEFAULT_TIMER_ONCALENDAR,
    randomized_delay: str = DEFAULT_TIMER_RANDOMIZED_DELAY,
    script_path: Path = UPDATER_SCRIPT_INSTALL_PATH,
    service_path: Path = UPDATER_SERVICE_PATH,
    timer_path: Path = UPDATER_TIMER_PATH,
    dry_run: bool = False,
) -> int:
    """Install / refresh ``minecraft-update.{service,timer}`` and enable the timer.

    Idempotent: rewrites unit files only when content differs, runs ``daemon-reload`` only when any unit
    actually changed, and runs ``systemctl enable --now`` on the timer (a no-op when already enabled-and-
    running per systemd semantics). Re-running against an already-configured host produces zero side
    effects: no rewritten files, no daemon-reload, no service flap.

    Args:
        oncalendar: Timer ``OnCalendar=`` expression.
        randomized_delay: Timer ``RandomizedDelaySec=`` value.
        script_path: Installed updater script path (referenced from the service unit's ExecStart).
        service_path: Where to install the .service unit.
        timer_path: Where to install the .timer unit.
        dry_run: When true, log without writing or invoking systemd.

    Returns:
        Process exit code. ``0`` on success.
    """
    service_changed = write_unit_if_changed(service_path, render_updater_service(script_path), dry_run=dry_run)
    timer_changed = write_unit_if_changed(
        timer_path, render_updater_timer(oncalendar, randomized_delay), dry_run=dry_run
    )
    if service_changed or timer_changed:
        _run(["systemctl", "daemon-reload"], dry_run=dry_run)
    else:
        log("[skip] daemon-reload (no unit changes)")
    _run(["systemctl", "enable", "--now", UPDATER_TIMER_NAME], dry_run=dry_run)
    return 0


def _run(argv: Sequence[str], *, dry_run: bool = False) -> None:
    """Run a local command, honoring ``--dry-run``.

    Args:
        argv: Command tokens.
        dry_run: When true, log only.
    """
    rendered = " ".join(shlex.quote(t) for t in argv)
    if dry_run:
        log(f"[dry-run] {rendered}")
        return
    log(f"[run] {rendered}")
    subprocess.run(list(argv), check=True)  # noqa: S603 — argv as list, no shell.


# --- Main --------------------------------------------------------------------


def update(args: argparse.Namespace) -> int:
    """Run the full updater pipeline once.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    if args.install_systemd_units:
        return install_systemd_units(
            oncalendar=args.timer_oncalendar,
            randomized_delay=args.timer_randomized_delay,
            dry_run=args.dry_run,
        )

    assert_root()

    try:
        lock_handle = acquire_lock(args.lock_file)
    except LockAcquisitionError as exc:
        log(f"[skip] {exc}")
        return 0

    try:
        paths = UpdatePaths.from_data_root(args.data_root)
        components = list(args.components) if args.components else list(ALL_COMPONENTS)

        cli_overrides: dict[str, object] = {
            "max_paper_build": args.max_paper_build,
        }
        plans = build_plan(components, paths, cli_overrides, force=args.force)
        # apply_plans returns the count of swapped components for callers/tests; the process exit code
        # is 0 on any non-raising completion (zero updates, dry-run, or N successful swaps all map to 0).
        apply_plans(
            plans,
            paths,
            owner_user=args.minecraft_user,
            owner_group=args.minecraft_group,
            no_restart=args.no_restart,
            skip_probe=args.skip_probe,
            retention=args.keep_backups,
            dry_run=args.dry_run,
        )
        return 0
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_handle.close()


def _parse_components_csv(value: str) -> list[str]:
    """Parse a ``--components`` CSV list and validate each entry.

    Args:
        value: Raw CLI string (e.g. ``"geyser,floodgate"``).

    Returns:
        Validated component identifiers in input order.

    Raises:
        argparse.ArgumentTypeError: When an entry is not a known component.
    """
    parts = [p.strip() for p in value.split(",") if p.strip()]
    unknown = [p for p in parts if p not in ALL_COMPONENTS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown component(s): {', '.join(unknown)}. Valid: {', '.join(ALL_COMPONENTS)}"
        )
    return parts


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="minecraft-update.py",
        description=(
            "Idempotent nightly updater for Paper + Geyser + Floodgate + ViaVersion. Designed to run as "
            "root under the minecraft-update.timer systemd timer. Connectivity preservation for Bedrock "
            "clients is the primary motivation: see the module docstring."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=f"Mountpoint of the data volume (default: {DEFAULT_DATA_ROOT}).",
    )
    parser.add_argument(
        "--components",
        type=_parse_components_csv,
        default=None,
        help=("Comma-separated subset of components to consider " f"(default: all of {','.join(ALL_COMPONENTS)})."),
    )
    parser.add_argument(
        "--minecraft-user",
        default=DEFAULT_USER,
        help=f"Service user (default: {DEFAULT_USER}).",
    )
    parser.add_argument(
        "--minecraft-group",
        default=DEFAULT_GROUP,
        help=f"Service group (default: {DEFAULT_GROUP}).",
    )
    parser.add_argument(
        "--max-paper-build",
        type=int,
        default=None,
        help="Refuse to apply Paper builds higher than this within the current MC version.",
    )
    parser.add_argument(
        "--keep-backups",
        type=int,
        default=DEFAULT_BACKUP_RETENTION,
        help=f"Backups to retain per component (default: {DEFAULT_BACKUP_RETENTION}).",
    )
    parser.add_argument(
        "--lock-file",
        dest="lock_file",
        type=Path,
        default=DEFAULT_LOCK_PATH,
        help=f"Override the lock-file path (default: {DEFAULT_LOCK_PATH}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download and swap even when sha256 matches (sha256-corruption recovery).",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Apply jar updates but skip the systemctl restart (operator orchestrates downtime).",
    )
    parser.add_argument(
        "--skip-probe",
        action="store_true",
        help="Skip the post-restart TCP 25565 probe (still wait for systemctl is-active).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print every mutation that would be performed without executing it.",
    )
    parser.add_argument(
        "--install-systemd-units",
        action="store_true",
        help=(
            "Install /etc/systemd/system/minecraft-update.{service,timer} (idempotently) and enable the "
            "timer. Skips the update flow entirely."
        ),
    )
    parser.add_argument(
        "--timer-oncalendar",
        default=DEFAULT_TIMER_ONCALENDAR,
        help=f"Timer OnCalendar= expression for --install-systemd-units (default: '{DEFAULT_TIMER_ONCALENDAR}').",
    )
    parser.add_argument(
        "--timer-randomized-delay",
        default=DEFAULT_TIMER_RANDOMIZED_DELAY,
        help=(
            "Timer RandomizedDelaySec= value for --install-systemd-units "
            f"(default: {DEFAULT_TIMER_RANDOMIZED_DELAY})."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Optional CLI arg list (``None`` reads from ``sys.argv``).

    Returns:
        Process exit code. ``0`` on success, ``2`` on pre-flight / clamp failure, ``3`` on
        download/checksum failure, ``4`` on service-restart failure, ``1`` on any other update error.
    """
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        return update(args)
    except PreflightError as exc:
        print(f"pre-flight check failed: {exc}", file=sys.stderr)
        return 2
    except PaperMcVersionMismatchError as exc:
        print(f"paper clamp refused update: {exc}", file=sys.stderr)
        return 2
    except (DownloadError, ChecksumMismatchError) as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        return 3
    except ServiceRestartError as exc:
        print(f"service restart failed: {exc}", file=sys.stderr)
        return 4
    except UpdateError as exc:
        print(f"update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
