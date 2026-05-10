"""Unit + e2e tests for ``install.py``.

Pure-logic and idempotency tests run by default. Tests marked ``@pytest.mark.e2e`` hit the real PaperMC, Geyser,
Floodgate, and Hangar APIs and are skipped unless ``pytest --run-e2e`` is passed (see ``conftest.py``). Per
``.claude/rules/external-api-grounding.md``, the unit tests use hand-crafted fixtures derived from real responses
to exercise extraction logic, while the e2e tests are the contract check against the real API shape.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from _common import (
    FLOODGATE_API_BASE,
    GEYSER_API_BASE,
    PAPER_API_BASE,
    ChecksumMismatchError,
    DownloadError,
    JarArtifact,
    _geyser_like_artifact_from_payload,
    _paper_artifact_from_build_payload,
    _viaversion_artifact_from_payload,
    fetch_geyser_like_artifact,
    fetch_paper_artifact,
    fetch_viaversion_artifact,
    read_mc_version_from_history,
    sha256_of_file,
)
from install import (
    CORRETTO_REPO_LINE,
    DEFAULT_DATA_ROOT,
    DEFAULT_GROUP,
    DEFAULT_HEAP,
    DEFAULT_USER,
    EULA_CONTENT,
    SYSTEMD_UNIT_NAME,
    UPDATER_SCRIPT_NAME,
    FloodgateKeyMissingError,
    InstallError,
    InstallPaths,
    PreflightError,
    VersionDiscoveryError,
    apt_install,
    apt_packages_installed,
    assert_floodgate_key_present,
    assert_ubuntu_2204,
    assert_volume_mounted,
    build_argument_parser,
    deploy_updater,
    download_and_place_jar,
    ensure_directory,
    ensure_eula,
    ensure_group,
    ensure_start_script,
    ensure_systemd_unit,
    ensure_user,
    invoke_updater_install_systemd_units,
    main,
    parse_os_release,
    render_start_script,
    render_systemd_unit,
    resolve_versions,
    write_file_if_changed,
)

# --- parse_os_release / assert_ubuntu_2204 -----------------------------------


def test_parse_os_release_strips_quotes_and_handles_blanks() -> None:
    """Quoted values strip; comment/blank lines are ignored."""
    text = "# header\nID=ubuntu\n\nVERSION_ID=\"22.04\"\nPRETTY_NAME='Ubuntu 22.04'\n"
    parsed = parse_os_release(text)
    assert parsed == {"ID": "ubuntu", "VERSION_ID": "22.04", "PRETTY_NAME": "Ubuntu 22.04"}


def test_assert_ubuntu_2204_accepts_real_lightsail_format() -> None:
    """The exact /etc/os-release shape captured from the live LightSail host passes."""
    assert_ubuntu_2204('PRETTY_NAME="Ubuntu 22.04.5 LTS"\nVERSION_ID="22.04"\nID=ubuntu\n')


@pytest.mark.parametrize(
    "text",
    [
        'ID=ubuntu\nVERSION_ID="20.04"\n',  # wrong version
        'ID=debian\nVERSION_ID="22.04"\n',  # wrong distro
        "",  # empty
    ],
)
def test_assert_ubuntu_2204_rejects_other_systems(text: str) -> None:
    """Anything other than Ubuntu 22.04 raises a ``PreflightError``."""
    with pytest.raises(PreflightError):
        assert_ubuntu_2204(text)


# --- assert_volume_mounted ---------------------------------------------------


def test_assert_volume_mounted_accepts_when_target_present() -> None:
    """A line with the target as the second column is enough."""
    text = "/dev/xvdf1 /srv/minecraft ext4 rw,relatime 0 0\n"
    assert_volume_mounted(Path("/srv/minecraft"), text)


def test_assert_volume_mounted_rejects_when_missing() -> None:
    """No matching line raises ``PreflightError``."""
    text = "/dev/xvda1 / ext4 rw 0 0\n"
    with pytest.raises(PreflightError, match="not a mountpoint"):
        assert_volume_mounted(Path("/srv/minecraft"), text)


def test_assert_volume_mounted_does_not_match_prefix() -> None:
    """``/srv/minecraft-other`` must not satisfy the check for ``/srv/minecraft``."""
    text = "/dev/xvdf1 /srv/minecraft-other ext4 rw 0 0\n"
    with pytest.raises(PreflightError):
        assert_volume_mounted(Path("/srv/minecraft"), text)


# --- read_mc_version_from_history --------------------------------------------


def test_read_mc_version_from_history_parses_real_shape(tmp_path: Path) -> None:
    """The ``"git-Paper-<build> (MC: <ver>)"`` shape Paper writes parses correctly."""
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    (server_dir / "version_history.json").write_text(
        '{"oldVersion": "git-Paper-58 (MC: 1.21.8)", "currentVersion": "git-Paper-60 (MC: 1.21.8)"}\n',
        encoding="utf-8",
    )
    result = read_mc_version_from_history(server_dir)
    assert result == ("1.21.8", 60)


def test_read_mc_version_from_history_returns_none_when_missing(tmp_path: Path) -> None:
    """Missing ``version_history.json`` → ``None`` (not an error)."""
    assert read_mc_version_from_history(tmp_path) is None


def test_read_mc_version_from_history_returns_none_on_unrecognized_shape(tmp_path: Path) -> None:
    """A file with no ``currentVersion`` field returns ``None``."""
    (tmp_path / "version_history.json").write_text('{"foo": "bar"}\n', encoding="utf-8")
    assert read_mc_version_from_history(tmp_path) is None


# --- sha256_of_file ----------------------------------------------------------


def test_sha256_of_file_matches_hashlib(tmp_path: Path) -> None:
    """Streaming hash matches a one-shot hash of the same bytes."""
    payload = b"hello\n" * 100_000
    target = tmp_path / "file.bin"
    target.write_bytes(payload)
    assert sha256_of_file(target) == hashlib.sha256(payload).hexdigest()


# --- write_file_if_changed ---------------------------------------------------


def test_write_file_if_changed_writes_when_absent(tmp_path: Path) -> None:
    """A missing path is written and reports change."""
    target = tmp_path / "out.txt"
    changed = write_file_if_changed(target, "hi\n", owner_user=None, owner_group=None, mode=0o644)
    assert changed is True
    assert target.read_text() == "hi\n"
    assert target.stat().st_mode & 0o7777 == 0o644


def test_write_file_if_changed_skips_when_identical(tmp_path: Path) -> None:
    """A path with matching content + mode is skipped."""
    target = tmp_path / "out.txt"
    target.write_text("hi\n")
    target.chmod(0o644)
    changed = write_file_if_changed(target, "hi\n", owner_user=None, owner_group=None, mode=0o644)
    assert changed is False


def test_write_file_if_changed_writes_when_content_differs(tmp_path: Path) -> None:
    """Content drift triggers a re-write."""
    target = tmp_path / "out.txt"
    target.write_text("old\n")
    target.chmod(0o644)
    changed = write_file_if_changed(target, "new\n", owner_user=None, owner_group=None, mode=0o644)
    assert changed is True
    assert target.read_text() == "new\n"


def test_write_file_if_changed_writes_when_mode_differs(tmp_path: Path) -> None:
    """Mode drift alone is enough to trigger a re-write (chmod applied)."""
    target = tmp_path / "out.txt"
    target.write_text("hi\n")
    target.chmod(0o600)
    changed = write_file_if_changed(target, "hi\n", owner_user=None, owner_group=None, mode=0o644)
    assert changed is True
    assert target.stat().st_mode & 0o7777 == 0o644


def test_write_file_if_changed_dry_run_does_not_touch_disk(tmp_path: Path) -> None:
    """Dry-run reports the planned write but never opens the file."""
    target = tmp_path / "out.txt"
    changed = write_file_if_changed(target, "hi\n", owner_user=None, owner_group=None, mode=0o644, dry_run=True)
    assert changed is True
    assert not target.exists()


# --- ensure_directory --------------------------------------------------------


def test_ensure_directory_creates_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing dir is created; chown is invoked."""
    target = tmp_path / "subdir"
    runs: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        runs.append(list(argv))
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("install.subprocess.run", fake_run)
    monkeypatch.setattr("install.run_command", lambda argv, **kw: fake_run(argv, **kw))
    created = ensure_directory(target, owner_user="me", owner_group="me")
    assert created is True
    assert target.is_dir()
    assert any(cmd[:2] == ["chown", "me:me"] for cmd in runs)


def test_ensure_directory_dry_run_does_not_mkdir(tmp_path: Path) -> None:
    """Dry-run reports mkdir without actually creating."""
    target = tmp_path / "subdir"
    ensure_directory(target, owner_user="me", owner_group="me", dry_run=True)
    assert not target.exists()


# --- Paper artifact parser ---------------------------------------------------


PAPER_BUILD_FIXTURE = {
    "id": 60,
    "time": "2025-09-06T21:50:11.982Z",
    "channel": "STABLE",
    "downloads": {
        "server:default": {
            "name": "paper-1.21.8-60.jar",
            "checksums": {"sha256": "8de7c52c3b02403503d16fac58003f1efef7dd7a0256786843927fa92ee57f1e"},
            "size": 52811717,
            "url": "https://fill-data.papermc.io/v1/objects/8de7.../paper-1.21.8-60.jar",
        }
    },
}


def test_paper_artifact_extracts_url_sha_and_filename() -> None:
    """The Paper Fill v3 build shape extracts cleanly."""
    artifact = _paper_artifact_from_build_payload("1.21.8", PAPER_BUILD_FIXTURE)
    assert artifact.label == "paper"
    assert artifact.version == "1.21.8-60"
    assert artifact.filename == "paper-1.21.8-60.jar"
    assert artifact.sha256 == "8de7c52c3b02403503d16fac58003f1efef7dd7a0256786843927fa92ee57f1e"
    assert artifact.download_url.startswith("https://fill-data.papermc.io/")


@pytest.mark.parametrize(
    "broken",
    [
        {},  # no downloads
        {"id": 1, "downloads": {}},  # no server:default
        {"id": 1, "downloads": {"server:default": {"checksums": {"sha256": "x"}}}},  # no url
        {"id": 1, "downloads": {"server:default": {"url": "u", "name": "n"}}},  # no checksums
        "not a dict",  # wrong type
    ],
)
def test_paper_artifact_raises_on_bad_shape(broken: object) -> None:
    """Missing/wrong-type fields raise ``DownloadError``, not silent garbage."""
    with pytest.raises(DownloadError):
        _paper_artifact_from_build_payload("1.21.8", broken)


# --- Geyser-like artifact parser ---------------------------------------------


GEYSER_BUILD_FIXTURE = {
    "project_id": "geyser",
    "project_name": "Geyser",
    "version": "2.10.0",
    "build": 1143,
    "downloads": {
        "spigot": {
            "name": "Geyser-Spigot.jar",
            "sha256": "10ea362fa631b6d08a94ebde20a320d6aa76e31c317f2495f2709b238ad757c2",
        },
        "bungeecord": {"name": "Geyser-BungeeCord.jar", "sha256": "acb5..."},
    },
}


def test_geyser_artifact_extracts_spigot_variant() -> None:
    """The spigot entry is selected and the download URL is constructed."""
    artifact = _geyser_like_artifact_from_payload("geyser", GEYSER_API_BASE, GEYSER_BUILD_FIXTURE)
    assert artifact.label == "geyser"
    assert artifact.version == "2.10.0-1143"
    assert artifact.filename == "Geyser-Spigot.jar"
    assert artifact.sha256.startswith("10ea362f")
    assert artifact.download_url == f"{GEYSER_API_BASE}/versions/2.10.0/builds/1143/downloads/spigot"


def test_floodgate_artifact_uses_floodgate_base() -> None:
    """The Floodgate base URL (not Geyser's) is interpolated for Floodgate builds."""
    floodgate_fixture = {
        "project_id": "floodgate",
        "version": "2.2.5",
        "build": 132,
        "downloads": {"spigot": {"name": "floodgate-spigot.jar", "sha256": "651d..."}},
    }
    artifact = _geyser_like_artifact_from_payload("floodgate", FLOODGATE_API_BASE, floodgate_fixture)
    assert artifact.download_url.startswith(FLOODGATE_API_BASE)
    assert artifact.filename == "floodgate-spigot.jar"


@pytest.mark.parametrize(
    "broken",
    [
        {},  # missing fields
        {"version": "2.10.0", "build": 1, "downloads": {}},  # no spigot
        {"version": "2.10.0", "build": 1, "downloads": {"spigot": {}}},  # spigot missing fields
        "not a dict",
    ],
)
def test_geyser_artifact_raises_on_bad_shape(broken: object) -> None:
    """Shape mismatches raise ``DownloadError``."""
    with pytest.raises(DownloadError):
        _geyser_like_artifact_from_payload("geyser", GEYSER_API_BASE, broken)


# --- ViaVersion (Hangar) parser ----------------------------------------------


HANGAR_VERSION_FIXTURE = {
    "name": "5.9.1",
    "downloads": {
        "PAPER": {
            "fileInfo": {
                "name": "ViaVersion-5.9.1.jar",
                "sizeBytes": 6000000,
                "sha256Hash": "abcd1234",
            },
            "downloadUrl": "https://hangarcdn.papermc.io/plugins/ViaVersion/ViaVersion/versions/5.9.1/PAPER/ViaVersion-5.9.1.jar",
        }
    },
}


def test_viaversion_artifact_extracts_paper_platform() -> None:
    """The PAPER platform's download is selected."""
    artifact = _viaversion_artifact_from_payload(HANGAR_VERSION_FIXTURE)
    assert artifact.label == "viaversion"
    assert artifact.version == "5.9.1"
    assert artifact.filename == "ViaVersion.jar"
    assert artifact.sha256 == "abcd1234"
    assert "PAPER" in artifact.download_url


def test_viaversion_artifact_raises_on_missing_paper_platform() -> None:
    """A version with no PAPER download (e.g., a Velocity-only release) errors."""
    payload = {"name": "5.9.1", "downloads": {"VELOCITY": {"fileInfo": {}, "downloadUrl": "u"}}}
    with pytest.raises(DownloadError):
        _viaversion_artifact_from_payload(payload)


# --- render functions --------------------------------------------------------


def test_render_systemd_unit_substitutes_user_group_and_dir() -> None:
    """User, group, and server-dir substitutions land in the unit body."""
    rendered = render_systemd_unit(Path("/srv/minecraft/server"), "minecraft", "minecraft")
    assert "User=minecraft" in rendered
    assert "Group=minecraft" in rendered
    assert "WorkingDirectory=/srv/minecraft/server" in rendered
    assert "ExecStart=/srv/minecraft/server/start.sh" in rendered


def test_render_start_script_includes_heap_flags() -> None:
    """Heap size flows through into both -Xms and -Xmx."""
    rendered = render_start_script(Path("/srv/minecraft/server"), "8G")
    assert "-Xms8G -Xmx8G" in rendered
    assert "/srv/minecraft/server/paper.jar --nogui" in rendered
    assert rendered.startswith("#!/usr/bin/env bash")


def test_render_start_script_is_idempotent_on_same_inputs() -> None:
    """Two renders with the same inputs produce byte-identical output."""
    a = render_start_script(Path("/srv/minecraft/server"), "6G")
    b = render_start_script(Path("/srv/minecraft/server"), "6G")
    assert a == b


# --- Floodgate key check -----------------------------------------------------


def test_floodgate_key_present_passes(tmp_path: Path) -> None:
    """An existing key.pem permits the run."""
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / "floodgate").mkdir(parents=True)
    (plugins_dir / "floodgate" / "key.pem").write_bytes(b"PEM")
    assert_floodgate_key_present(plugins_dir, allow_fresh=False)


def test_floodgate_key_missing_raises_by_default(tmp_path: Path) -> None:
    """A missing key without override raises ``FloodgateKeyMissingError``."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    with pytest.raises(FloodgateKeyMissingError, match="key.pem"):
        assert_floodgate_key_present(plugins_dir, allow_fresh=False)


def test_floodgate_key_missing_with_override_passes(tmp_path: Path) -> None:
    """``--allow-fresh-floodgate-key`` permits a missing key with a warning."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    assert_floodgate_key_present(plugins_dir, allow_fresh=True)


# --- ensure_eula / ensure_systemd_unit / ensure_start_script -----------------


def _write_dummy_paths(tmp_path: Path) -> InstallPaths:
    """Build an ``InstallPaths`` rooted at ``tmp_path`` for filesystem tests.

    Args:
        tmp_path: Pytest tmp fixture.

    Returns:
        The constructed paths, with ``server_dir`` already created.
    """
    paths = InstallPaths.from_data_root(tmp_path)
    paths.server_dir.mkdir(parents=True, exist_ok=True)
    return paths


def test_ensure_eula_writes_then_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First call writes; second call detects the existing file and skips."""
    monkeypatch.setattr("install.run_command", lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
    paths = _write_dummy_paths(tmp_path)
    assert ensure_eula(paths, "u", "g") is True
    assert paths.eula_file.read_text() == EULA_CONTENT
    assert ensure_eula(paths, "u", "g") is False


def test_ensure_start_script_re_renders_on_heap_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing the heap-size flag rewrites the script."""
    monkeypatch.setattr("install.run_command", lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
    paths = _write_dummy_paths(tmp_path)
    assert ensure_start_script(paths, heap="6G", owner_user="u", owner_group="g") is True
    assert "-Xms6G -Xmx6G" in paths.start_script.read_text()
    assert ensure_start_script(paths, heap="6G", owner_user="u", owner_group="g") is False
    assert ensure_start_script(paths, heap="8G", owner_user="u", owner_group="g") is True
    assert "-Xms8G -Xmx8G" in paths.start_script.read_text()


def test_ensure_systemd_unit_writes_to_target_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``SYSTEMD_UNIT_PATH`` to a tmp file and verify the writer drops it there."""
    target = tmp_path / "minecraft.service"
    monkeypatch.setattr("install.SYSTEMD_UNIT_PATH", target)
    monkeypatch.setattr("install.run_command", lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
    paths = _write_dummy_paths(tmp_path / "data")
    assert ensure_systemd_unit(paths, user="minecraft", group="minecraft") is True
    body = target.read_text()
    assert "User=minecraft" in body
    assert "WorkingDirectory=" in body


# --- apt_install --------------------------------------------------------------


def test_apt_install_skips_when_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing is installed when every package is already present."""
    monkeypatch.setattr("install.apt_packages_installed", lambda pkgs: {p: True for p in pkgs})
    runs: list[list[str]] = []
    monkeypatch.setattr(
        "install.run_command",
        lambda argv, **kw: runs.append(list(argv)) or subprocess.CompletedProcess([], 0, "", ""),
    )
    installed = apt_install(["unzip", "wget"])
    assert installed == ()
    assert runs == []  # no apt-get install called


def test_apt_install_only_installs_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Already-installed packages are filtered out of the apt-get argv."""
    monkeypatch.setattr(
        "install.apt_packages_installed",
        lambda pkgs: {"unzip": True, "wget": False, "ca-certificates": False},
    )
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "install.run_command",
        lambda argv, **kw: captured.append(list(argv)) or subprocess.CompletedProcess([], 0, "", ""),
    )
    installed = apt_install(["unzip", "wget", "ca-certificates"])
    assert set(installed) == {"wget", "ca-certificates"}
    assert any("apt-get" in tok for cmd in captured for tok in cmd)
    flat = " ".join(captured[0])
    assert "wget" in flat and "ca-certificates" in flat
    assert "unzip" not in flat


# --- ensure_group / ensure_user ----------------------------------------------


def test_ensure_group_skips_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing group is detected and not re-created."""
    monkeypatch.setattr("install.group_exists", lambda name: True)
    runs: list[list[str]] = []
    monkeypatch.setattr(
        "install.run_command",
        lambda argv, **kw: runs.append(list(argv)) or subprocess.CompletedProcess([], 0, "", ""),
    )
    assert ensure_group("minecraft") is False
    assert runs == []


def test_ensure_group_creates_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing group triggers a ``groupadd``."""
    monkeypatch.setattr("install.group_exists", lambda name: False)
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "install.run_command",
        lambda argv, **kw: captured.append(list(argv)) or subprocess.CompletedProcess([], 0, "", ""),
    )
    assert ensure_group("minecraft") is True
    assert captured[0][:2] == ["groupadd", "--system"]
    assert "minecraft" in captured[0]


def test_ensure_user_creates_with_correct_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Useradd is called with --system, --gid, --home-dir, --shell."""
    monkeypatch.setattr("install.user_exists", lambda name: False)
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "install.run_command",
        lambda argv, **kw: captured.append(list(argv)) or subprocess.CompletedProcess([], 0, "", ""),
    )
    ensure_user("minecraft", group="minecraft", home=Path("/srv/minecraft"), shell="/usr/sbin/nologin")
    assert captured[0][0] == "useradd"
    assert "--system" in captured[0]
    assert "--gid" in captured[0] and "minecraft" in captured[0]
    assert "--home-dir" in captured[0] and "/srv/minecraft" in captured[0]
    assert "--shell" in captured[0] and "/usr/sbin/nologin" in captured[0]


# --- download_and_place_jar --------------------------------------------------


def test_download_and_place_jar_skips_when_hash_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing file with the expected sha256 is left in place."""
    payload = b"jar-bytes"
    sha = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "paper.jar"
    target.write_bytes(payload)
    artifact = JarArtifact(
        label="paper", version="1.21.8-60", download_url="https://example/paper.jar", sha256=sha, filename="paper.jar"
    )
    download_called = mock.Mock()
    monkeypatch.setattr("install.http_download_to_file", download_called)
    monkeypatch.setattr("install.run_command", lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
    changed = download_and_place_jar(artifact, target, owner_user="u", owner_group="g")
    assert changed is False
    download_called.assert_not_called()


def test_download_and_place_jar_downloads_then_verifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing file is downloaded; the sha256 is checked post-download."""
    payload = b"newer-bytes"
    sha = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "paper.jar"
    artifact = JarArtifact(
        label="paper", version="1.21.8-60", download_url="https://example/paper.jar", sha256=sha, filename="paper.jar"
    )

    def fake_download(url: str, dest: Path) -> None:
        dest.write_bytes(payload)

    monkeypatch.setattr("install.http_download_to_file", fake_download)
    monkeypatch.setattr("install.run_command", lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
    changed = download_and_place_jar(artifact, target, owner_user="u", owner_group="g")
    assert changed is True
    assert target.read_bytes() == payload


def test_download_and_place_jar_raises_on_checksum_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong-hash download is unlinked and ``ChecksumMismatchError`` raised."""
    target = tmp_path / "paper.jar"
    artifact = JarArtifact(
        label="paper",
        version="1.21.8-60",
        download_url="https://example/paper.jar",
        sha256="0" * 64,
        filename="paper.jar",
    )

    def fake_download(url: str, dest: Path) -> None:
        dest.write_bytes(b"actual-different-bytes")

    monkeypatch.setattr("install.http_download_to_file", fake_download)
    monkeypatch.setattr("install.run_command", lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
    with pytest.raises(ChecksumMismatchError):
        download_and_place_jar(artifact, target, owner_user="u", owner_group="g")
    assert not target.exists()


# --- resolve_versions --------------------------------------------------------


def _make_args(**overrides: object) -> argparse.Namespace:
    """Build an ``argparse.Namespace`` matching the parser's defaults plus overrides."""
    defaults = {
        "data_root": Path("/tmp/data"),
        "minecraft_user": DEFAULT_USER,
        "minecraft_group": DEFAULT_GROUP,
        "minecraft_shell": "/usr/sbin/nologin",
        "heap_size": DEFAULT_HEAP,
        "mc_version": None,
        "paper_build": None,
        "geyser_version": None,
        "geyser_build": None,
        "floodgate_version": None,
        "floodgate_build": None,
        "viaversion": None,
        "allow_fresh_floodgate_key": False,
        "skip_volume_check": False,
        "dry_run": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_resolve_versions_uses_cli_flag_when_provided() -> None:
    """An explicit ``--mc-version`` overrides volume discovery."""
    args = _make_args(mc_version="1.21.8", paper_build=42)
    paths = InstallPaths.from_data_root(Path("/nonexistent"))
    versions = resolve_versions(args, paths)
    assert versions.mc_version == "1.21.8"
    assert versions.paper_build == 42


def test_resolve_versions_reads_history_when_flag_absent(tmp_path: Path) -> None:
    """Missing CLI flag triggers ``version_history.json`` discovery."""
    paths = InstallPaths.from_data_root(tmp_path)
    paths.server_dir.mkdir(parents=True)
    (paths.server_dir / "version_history.json").write_text(
        '{"currentVersion": "git-Paper-60 (MC: 1.21.8)"}', encoding="utf-8"
    )
    args = _make_args(data_root=tmp_path)
    versions = resolve_versions(args, paths)
    assert versions.mc_version == "1.21.8"
    assert versions.paper_build == 60


def test_resolve_versions_raises_when_neither_source_available(tmp_path: Path) -> None:
    """No flag + no volume → ``VersionDiscoveryError``."""
    paths = InstallPaths.from_data_root(tmp_path)
    paths.server_dir.mkdir(parents=True)
    args = _make_args(data_root=tmp_path)
    with pytest.raises(VersionDiscoveryError):
        resolve_versions(args, paths)


# --- Argument parser ---------------------------------------------------------


def test_argument_parser_defaults() -> None:
    """Defaults match the constants documented in the script."""
    parser = build_argument_parser()
    args = parser.parse_args([])
    assert args.data_root == DEFAULT_DATA_ROOT
    assert args.minecraft_user == DEFAULT_USER
    assert args.heap_size == DEFAULT_HEAP
    assert args.mc_version is None
    assert args.dry_run is False
    assert args.allow_fresh_floodgate_key is False


def test_argument_parser_accepts_mc_version_and_dry_run() -> None:
    """Pinning the MC version and engaging dry-run flow through to the namespace."""
    parser = build_argument_parser()
    args = parser.parse_args(["--mc-version", "1.21.8", "--paper-build", "60", "--dry-run"])
    assert args.mc_version == "1.21.8"
    assert args.paper_build == 60
    assert args.dry_run is True


# --- main() error mapping ----------------------------------------------------


def test_main_returns_two_on_preflight_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A preflight error maps to exit code 2."""

    def boom(_args: argparse.Namespace) -> int:
        raise PreflightError("not root")

    monkeypatch.setattr("install.install", boom)
    assert main([]) == 2


def test_main_returns_three_on_download_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A download error maps to exit code 3 (distinct from preflight)."""

    def boom(_args: argparse.Namespace) -> int:
        raise DownloadError("network down")

    monkeypatch.setattr("install.install", boom)
    assert main([]) == 3


# --- Repo-line constant ------------------------------------------------------


def test_corretto_repo_line_signed_by_keyring() -> None:
    """The committed apt source line includes ``signed-by=…/corretto-keyring.gpg``."""
    assert "signed-by=/usr/share/keyrings/corretto-keyring.gpg" in CORRETTO_REPO_LINE
    assert "https://apt.corretto.aws stable main" in CORRETTO_REPO_LINE


# --- Updater deployment ------------------------------------------------------


def test_deploy_updater_copies_both_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first deploy writes both files with the right modes and renames update.py."""
    source = tmp_path / "src"
    install_dir = tmp_path / "sbin"
    source.mkdir()
    (source / "update.py").write_text("#!/usr/bin/env python3\nprint('updater')\n")
    (source / "_common.py").write_text("# shared\n")
    monkeypatch.setattr("install.UPDATER_INSTALL_DIR", install_dir)
    monkeypatch.setattr("install.UPDATER_SCRIPT_PATH", install_dir / UPDATER_SCRIPT_NAME)
    changed = deploy_updater(source)
    assert changed is True
    target_update = install_dir / UPDATER_SCRIPT_NAME
    target_common = install_dir / "_common.py"
    assert target_update.read_text() == "#!/usr/bin/env python3\nprint('updater')\n"
    assert target_common.read_text() == "# shared\n"
    assert target_update.stat().st_mode & 0o7777 == 0o755
    assert target_common.stat().st_mode & 0o7777 == 0o644


def test_deploy_updater_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second deploy with identical source content is a no-op."""
    source = tmp_path / "src"
    install_dir = tmp_path / "sbin"
    source.mkdir()
    (source / "update.py").write_text("body\n")
    (source / "_common.py").write_text("common\n")
    monkeypatch.setattr("install.UPDATER_INSTALL_DIR", install_dir)
    monkeypatch.setattr("install.UPDATER_SCRIPT_PATH", install_dir / UPDATER_SCRIPT_NAME)
    deploy_updater(source)
    assert deploy_updater(source) is False


def test_deploy_updater_rewrites_on_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Source-content drift causes a rewrite."""
    source = tmp_path / "src"
    install_dir = tmp_path / "sbin"
    source.mkdir()
    (source / "update.py").write_text("v1\n")
    (source / "_common.py").write_text("common\n")
    monkeypatch.setattr("install.UPDATER_INSTALL_DIR", install_dir)
    monkeypatch.setattr("install.UPDATER_SCRIPT_PATH", install_dir / UPDATER_SCRIPT_NAME)
    deploy_updater(source)
    (source / "update.py").write_text("v2\n")
    assert deploy_updater(source) is True
    assert (install_dir / UPDATER_SCRIPT_NAME).read_text() == "v2\n"


def test_deploy_updater_raises_when_source_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing source file fails loudly rather than silently skipping."""
    source = tmp_path / "src"
    install_dir = tmp_path / "sbin"
    source.mkdir()
    (source / "update.py").write_text("ok\n")  # _common.py intentionally missing
    monkeypatch.setattr("install.UPDATER_INSTALL_DIR", install_dir)
    with pytest.raises(InstallError, match="_common.py"):
        deploy_updater(source)


def test_invoke_updater_install_systemd_units_runs_self_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper shells out the updater with --install-systemd-units."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "install.run_command",
        lambda argv, **kw: captured.append(list(argv)) or subprocess.CompletedProcess([], 0),
    )
    invoke_updater_install_systemd_units()
    assert captured and "--install-systemd-units" in captured[0]
    assert "minecraft-update.py" in captured[0][1]


# --- E2E (real APIs) ---------------------------------------------------------


@pytest.mark.e2e
def test_e2e_paper_latest_for_1_21_8_returns_usable_artifact() -> None:
    """The Paper Fill v3 ``latest`` endpoint produces an artifact that downloads."""
    artifact = fetch_paper_artifact("1.21.8", "latest")
    assert artifact.sha256 and len(artifact.sha256) == 64
    assert artifact.download_url.startswith("https://")
    assert artifact.filename.endswith(".jar")


@pytest.mark.e2e
def test_e2e_geyser_latest_returns_usable_artifact() -> None:
    """The Geyser v2 latest version → latest build flow produces a real artifact."""
    artifact = fetch_geyser_like_artifact("geyser", GEYSER_API_BASE, version=None, build=None)
    assert artifact.sha256 and len(artifact.sha256) == 64
    assert artifact.download_url.startswith("https://")
    assert artifact.filename == "Geyser-Spigot.jar"


@pytest.mark.e2e
def test_e2e_floodgate_latest_returns_usable_artifact() -> None:
    """The Floodgate v2 latest version → latest build flow produces a real artifact."""
    artifact = fetch_geyser_like_artifact("floodgate", FLOODGATE_API_BASE, version=None, build=None)
    assert artifact.sha256 and len(artifact.sha256) == 64
    assert artifact.download_url.startswith("https://")
    assert "floodgate" in artifact.filename.lower()


@pytest.mark.e2e
def test_e2e_viaversion_latest_returns_usable_artifact() -> None:
    """The Hangar latest-Release flow produces a real ViaVersion PAPER download."""
    artifact = fetch_viaversion_artifact(None)
    assert artifact.sha256 and len(artifact.sha256) == 64
    assert artifact.download_url.startswith("https://")
    assert artifact.filename == "ViaVersion.jar"
