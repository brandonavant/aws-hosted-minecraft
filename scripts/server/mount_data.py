#!/usr/bin/env python3
"""Mount an existing Minecraft data filesystem by its explicitly supplied UUID.

This tool never formats disks. It refuses ambiguous UUIDs, conflicting mounts or target/UUID fstab entries, and a
nonempty unmounted target directory. Run it on the future host only after identifying the restored data filesystem.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tempfile
import uuid as uuid_module
from pathlib import Path

DEFAULT_MOUNTPOINT = Path("/srv/minecraft")
FSTAB_PATH = Path("/etc/fstab")


class MountError(RuntimeError):
    """Report a mount precondition or command failure.

    Args:
        message: Details of the unsafe condition or unsuccessful command.
    """


def run_command(argv: list[str], *, empty_on_one: bool = False) -> str:
    """Run a system command and return its standard output.

    Args:
        argv: Command and arguments.
        empty_on_one: Treat status 1 as an empty result for ``findmnt``.

    Returns:
        Stripped standard output.

    Raises:
        MountError: When the command cannot be run or fails unexpectedly.
    """
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise MountError(f"cannot run {argv[0]}: {exc}") from exc
    if result.returncode == 1 and empty_on_one and not result.stderr.strip():
        return ""
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise MountError(f"{argv[0]} failed: {detail}")
    return result.stdout.strip()


def normalize_uuid(value: str) -> str:
    """Validate and normalize an ext4 filesystem UUID.

    Args:
        value: Filesystem UUID supplied by the operator.

    Returns:
        Canonical lowercase UUID.

    Raises:
        MountError: When the supplied value is not a UUID.
    """
    try:
        return str(uuid_module.UUID(value))
    except ValueError as exc:
        raise MountError(f"invalid filesystem UUID: {value!r}") from exc


def find_device(filesystem_uuid: str) -> str:
    """Find exactly one ext4 device for a filesystem UUID.

    Args:
        filesystem_uuid: Canonical UUID of the restored filesystem.

    Returns:
        Device path reported by ``blkid``.

    Raises:
        MountError: When there is not exactly one match or the filesystem is not ext4.
    """
    devices = run_command(["blkid", "-c", "/dev/null", "-t", f"UUID={filesystem_uuid}", "-o", "device"]).splitlines()
    if len(devices) != 1:
        raise MountError(f"expected exactly one device for UUID {filesystem_uuid}; found {len(devices)}")
    device = devices[0]
    filesystem_type = run_command(["blkid", "-c", "/dev/null", "-s", "TYPE", "-o", "value", device])
    if filesystem_type != "ext4":
        raise MountError(f"{device} has filesystem {filesystem_type!r}, not ext4; refusing to alter it")
    return device


def mounted_uuid(mountpoint: Path) -> str | None:
    """Read the UUID mounted read-write at exactly the requested mountpoint.

    Args:
        mountpoint: Absolute target directory.

    Returns:
        The mounted filesystem UUID, or ``None`` when the target is not a mountpoint.

    Raises:
        MountError: When ``findmnt`` fails, returns an unexpected result, or reports a read-only mount.
    """
    result = run_command(
        ["findmnt", "--noheadings", "--raw", "--mountpoint", str(mountpoint), "--output", "UUID,OPTIONS"],
        empty_on_one=True,
    )
    if not result:
        return None
    fields = result.split()
    if len(fields) != 2 or "rw" not in fields[1].split(","):
        raise MountError(f"{mountpoint} is not mounted read-write or has an unexpected mount status")
    return fields[0]


def device_mountpoints(device: str) -> list[str]:
    """List existing mountpoints for a block device.

    Args:
        device: Device path reported by ``blkid``.

    Returns:
        The device's existing mountpoint paths.

    Raises:
        MountError: When ``findmnt`` fails unexpectedly.
    """
    result = run_command(
        ["findmnt", "--noheadings", "--raw", "--source", device, "--output", "TARGET"],
        empty_on_one=True,
    )
    return result.splitlines()


def fstab_with_mount(content: str, filesystem_uuid: str, mountpoint: Path) -> str:
    """Append a UUID-based entry unless an equivalent entry already exists.

    Args:
        content: Existing fstab text.
        filesystem_uuid: Canonical UUID of the restored filesystem.
        mountpoint: Absolute target directory.

    Returns:
        Unchanged fstab text or text with one new ext4 mount entry.

    Raises:
        MountError: When existing entries conflict with the requested mount.
    """
    source = f"UUID={filesystem_uuid}"
    matching = 0
    for line in content.splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if len(fields) < 4:
            raise MountError("fstab contains an incomplete entry; refusing to modify it")
        entry_source, entry_target, filesystem_type = fields[:3]
        if entry_target == str(mountpoint):
            if entry_source.lower() != source.lower() or filesystem_type != "ext4":
                raise MountError(f"fstab already has a conflicting entry for {mountpoint}")
            if "ro" in fields[3].split(","):
                raise MountError(f"fstab mounts {mountpoint} read-only")
            matching += 1
        elif entry_source.lower() == source.lower():
            raise MountError(f"fstab already assigns UUID {filesystem_uuid} to {entry_target}")
    if matching > 1:
        raise MountError(f"fstab has multiple entries for {mountpoint}")
    if matching:
        return content
    separator = "" if not content or content.endswith("\n") else "\n"
    return f"{content}{separator}{source} {mountpoint} ext4 defaults,nofail 0 2\n"


def write_fstab(path: Path, original: str, updated: str) -> None:
    """Atomically update fstab while retaining its owner and permissions.

    Args:
        path: Existing fstab path.
        original: Content read before preparing the change.
        updated: Content to write.

    Raises:
        MountError: When fstab is a symlink or has changed concurrently.
        OSError: When reading or writing fstab fails.
    """
    if path.is_symlink():
        raise MountError(f"{path} is a symlink; refusing to replace it")
    existing_stat = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".minecraft-fstab-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchown(handle.fileno(), existing_stat.st_uid, existing_stat.st_gid)
            os.fchmod(handle.fileno(), stat.S_IMODE(existing_stat.st_mode))
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_text(encoding="utf-8") != original:
            raise MountError(f"{path} changed while preparing the mount; retry after reviewing it")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def mount_data(filesystem_uuid: str, mountpoint: Path, *, fstab: Path, dry_run: bool) -> None:
    """Verify, persist, and mount an existing Minecraft data filesystem.

    Args:
        filesystem_uuid: UUID explicitly identified by the operator.
        mountpoint: Directory where the filesystem should be mounted.
        fstab: Existing fstab path.
        dry_run: Report changes without writing or mounting.

    Raises:
        MountError: When a safety check or system command fails.
        OSError: When inspecting or updating local files fails.
    """
    filesystem_uuid = normalize_uuid(filesystem_uuid)
    if not mountpoint.is_absolute() or any(ch.isspace() or ch == "\\" for ch in str(mountpoint)):
        raise MountError("mountpoint must be an absolute, non-root path without whitespace or backslashes")
    if mountpoint.resolve() != mountpoint or mountpoint == Path("/"):
        raise MountError(f"{mountpoint} is not a canonical, non-root mountpoint")
    device = find_device(filesystem_uuid)
    current_uuid = mounted_uuid(mountpoint)
    if current_uuid is not None and current_uuid.lower() != filesystem_uuid:
        raise MountError(f"{mountpoint} is already mounted with a different filesystem")
    elsewhere = [target for target in device_mountpoints(device) if target != str(mountpoint)]
    if elsewhere:
        raise MountError(f"{device} is already mounted elsewhere: {', '.join(elsewhere)}")
    if current_uuid is None and mountpoint.exists():
        if not mountpoint.is_dir():
            raise MountError(f"{mountpoint} is not a directory")
        if any(mountpoint.iterdir()):
            raise MountError(f"{mountpoint} is not empty; refusing to hide its contents")

    original = fstab.read_text(encoding="utf-8")
    updated = fstab_with_mount(original, filesystem_uuid, mountpoint)
    if updated != original:
        if dry_run:
            print(f"[dry-run] add UUID {filesystem_uuid} to {fstab} for {mountpoint}")
        else:
            write_fstab(fstab, original, updated)
            print(f"[ok] added UUID {filesystem_uuid} to {fstab}")
    else:
        print(f"[skip] {fstab} already contains the requested mount")
    if current_uuid is not None:
        print(f"[skip] {mountpoint} already mounts UUID {filesystem_uuid}")
        return
    if dry_run:
        print(f"[dry-run] mount {device} at {mountpoint}")
        return
    mountpoint.mkdir(parents=True, exist_ok=True)
    run_command(["mount", str(mountpoint)])
    result_uuid = mounted_uuid(mountpoint)
    if result_uuid is None or result_uuid.lower() != filesystem_uuid:
        raise MountError(f"mount completed but {mountpoint} does not show UUID {filesystem_uuid}")
    print(f"[ok] mounted UUID {filesystem_uuid} at {mountpoint}")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and mount the explicitly identified data filesystem.

    Args:
        argv: Optional command-line arguments; ``None`` uses the process arguments.

    Returns:
        Zero on success, or two when a safety check or command fails.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uuid", required=True, help="UUID of the existing ext4 data filesystem.")
    parser.add_argument("--mountpoint", type=Path, default=DEFAULT_MOUNTPOINT, help="Target directory.")
    parser.add_argument("--dry-run", action="store_true", help="Check and report changes without modifying the host.")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise MountError("run as root so blkid can inspect the attached disk")
        mount_data(args.uuid, args.mountpoint, fstab=FSTAB_PATH, dry_run=args.dry_run)
    except (MountError, OSError) as exc:
        print(f"mount-data: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
