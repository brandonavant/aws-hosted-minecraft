"""Tests for mounting an existing data disk without formatting or hiding files."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

import mount_data as mount_module
from mount_data import MountError, find_device, fstab_with_mount, mount_data, mounted_uuid, write_fstab

FILESYSTEM_UUID = "123e4567-e89b-12d3-a456-426614174000"


def test_fstab_entry_is_idempotent_and_preserves_existing_options(tmp_path: Path) -> None:
    """Keep an existing UUID entry without adding duplicates or changing options.

    Args:
        tmp_path: Temporary directory used as the mountpoint.
    """
    existing = f"UUID={FILESYSTEM_UUID} {tmp_path} ext4 defaults,nofail 0 2\n"
    assert fstab_with_mount(existing, FILESYSTEM_UUID, tmp_path) == existing


@pytest.mark.parametrize(
    "content",
    [
        "UUID=00000000-0000-0000-0000-000000000000 {target} ext4 defaults 0 2\n",
        "UUID={uuid} /somewhere-else ext4 defaults 0 2\n",
        "UUID={uuid} {target} ext4 defaults 0 2\nUUID={uuid} {target} ext4 defaults 0 2\n",
        "UUID={uuid} {target} ext4 ro 0 2\n",
    ],
)
def test_fstab_conflicts_are_rejected(tmp_path: Path, content: str) -> None:
    """Refuse conflicting target, duplicate UUID, and duplicate target entries.

    Args:
        tmp_path: Temporary directory used as the mountpoint.
        content: Fstab template containing a conflict.
    """
    with pytest.raises(MountError):
        fstab_with_mount(content.format(target=tmp_path, uuid=FILESYSTEM_UUID), FILESYSTEM_UUID, tmp_path)


def test_find_device_rejects_duplicate_uuids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to guess when multiple devices report the requested UUID.

    Args:
        monkeypatch: Pytest helper for replacing the device lookup.
    """
    monkeypatch.setattr(mount_module, "run_command", lambda argv, **kwargs: "/dev/one\n/dev/two")
    with pytest.raises(MountError, match="exactly one"):
        find_device(FILESYSTEM_UUID)


def test_find_device_queries_current_devices_without_using_stale_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Look up the UUID and filesystem type without trusting stale blkid cache entries.

    Args:
        monkeypatch: Pytest helper for replacing the device lookup.
    """
    commands: list[list[str]] = []

    def fake_run(argv: list[str], *, empty_on_one: bool = False) -> str:
        """Return a single ext4 device and record the lookup arguments.

        Args:
            argv: Command and arguments.
            empty_on_one: Whether an empty result is accepted.

        Returns:
            The device path for the UUID lookup, or its filesystem type.
        """
        commands.append(argv)
        return "ext4" if "-s" in argv else "/dev/example"

    monkeypatch.setattr(mount_module, "run_command", fake_run)
    assert find_device(FILESYSTEM_UUID) == "/dev/example"
    assert all(command[1:3] == ["-c", "/dev/null"] for command in commands)


def test_mounted_uuid_rejects_read_only_mounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to treat a read-only mount as a usable Minecraft data volume.

    Args:
        tmp_path: Temporary directory used as the mountpoint.
        monkeypatch: Pytest helper for replacing the mount status query.
    """
    monkeypatch.setattr(mount_module, "run_command", lambda argv, **kwargs: f"{FILESYSTEM_UUID} ro,relatime")
    with pytest.raises(MountError, match="not mounted read-write"):
        mounted_uuid(tmp_path)


def test_mount_rejects_backslash_in_target(tmp_path: Path) -> None:
    """Reject paths whose fstab representation could be interpreted as an escape.

    Args:
        tmp_path: Temporary directory used as the mountpoint parent.
    """
    with pytest.raises(MountError, match="backslashes"):
        mount_data(FILESYSTEM_UUID, tmp_path / "unsafe\\040target", fstab=tmp_path / "fstab", dry_run=True)


def test_mount_is_idempotent_and_never_formats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write one entry and issue one mount operation across repeated runs.

    Args:
        tmp_path: Temporary directory for fstab and the target.
        monkeypatch: Pytest helper for replacing host probes and mount operations.
    """
    fstab = tmp_path / "fstab"
    fstab.write_text("# existing\n", encoding="utf-8")
    target = tmp_path / "minecraft"
    mounted = False
    commands: list[list[str]] = []

    def fake_mounted_uuid(path: Path) -> str | None:
        """Return the simulated mount state.

        Args:
            path: Mountpoint being checked.

        Returns:
            The filesystem UUID after the simulated mount, otherwise ``None``.
        """
        assert path == target
        return FILESYSTEM_UUID if mounted else None

    def fake_run(argv: list[str], *, empty_on_one: bool = False) -> str:
        """Record a simulated mount operation.

        Args:
            argv: Command and arguments.
            empty_on_one: Whether an empty result is accepted.

        Returns:
            Empty command output.
        """
        nonlocal mounted
        commands.append(argv)
        mounted = True
        return ""

    monkeypatch.setattr(mount_module, "find_device", lambda value: "/dev/example")
    monkeypatch.setattr(mount_module, "mounted_uuid", fake_mounted_uuid)
    monkeypatch.setattr(mount_module, "device_mountpoints", lambda device: [str(target)] if mounted else [])
    monkeypatch.setattr(mount_module, "run_command", fake_run)
    monkeypatch.setattr(mount_module.os, "fchown", lambda *args: None)
    mount_data(FILESYSTEM_UUID, target, fstab=fstab, dry_run=False)
    first_content = fstab.read_text(encoding="utf-8")
    mount_data(FILESYSTEM_UUID, target, fstab=fstab, dry_run=False)
    assert fstab.read_text(encoding="utf-8") == first_content
    assert first_content.count(f"UUID={FILESYSTEM_UUID}") == 1
    assert commands == [["mount", str(target)]]


def test_dry_run_and_nonempty_target_do_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave fstab unchanged in dry-run and when mounting would hide existing files.

    Args:
        tmp_path: Temporary directory for fstab and the target.
        monkeypatch: Pytest helper for replacing device probes.
    """
    fstab = tmp_path / "fstab"
    fstab.write_text("# original\n", encoding="utf-8")
    target = tmp_path / "minecraft"
    monkeypatch.setattr(mount_module, "find_device", lambda value: "/dev/example")
    monkeypatch.setattr(mount_module, "mounted_uuid", lambda path: None)
    monkeypatch.setattr(mount_module, "device_mountpoints", lambda device: [])
    run = mock.Mock()
    monkeypatch.setattr(mount_module, "run_command", run)
    mount_data(FILESYSTEM_UUID, target, fstab=fstab, dry_run=True)
    assert not target.exists()
    assert fstab.read_text(encoding="utf-8") == "# original\n"
    run.assert_not_called()
    target.mkdir()
    (target / "valuable-file").write_text("keep", encoding="utf-8")
    with pytest.raises(MountError, match="not empty"):
        mount_data(FILESYSTEM_UUID, target, fstab=fstab, dry_run=False)
    assert fstab.read_text(encoding="utf-8") == "# original\n"


def test_mount_refuses_wrong_filesystem_already_at_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not modify fstab when the target already mounts another filesystem.

    Args:
        tmp_path: Temporary directory for the target.
        monkeypatch: Pytest helper for replacing device probes.
    """
    monkeypatch.setattr(mount_module, "find_device", lambda value: "/dev/example")
    monkeypatch.setattr(mount_module, "mounted_uuid", lambda path: "00000000-0000-0000-0000-000000000000")
    with pytest.raises(MountError, match="different filesystem"):
        mount_data(FILESYSTEM_UUID, tmp_path / "minecraft", fstab=tmp_path / "fstab", dry_run=False)


def test_write_fstab_does_not_overwrite_concurrent_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve fstab when its contents changed after the initial read.

    Args:
        tmp_path: Temporary directory for fstab.
        monkeypatch: Pytest helper for avoiding an ownership change.
    """
    fstab = tmp_path / "fstab"
    fstab.write_text("changed by another process\n", encoding="utf-8")
    monkeypatch.setattr(mount_module.os, "fchown", lambda *args: None)
    with pytest.raises(MountError, match="changed"):
        write_fstab(fstab, "old content\n", "replacement\n")
    assert fstab.read_text(encoding="utf-8") == "changed by another process\n"
    assert list(tmp_path.iterdir()) == [fstab]
