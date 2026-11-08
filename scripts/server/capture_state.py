#!/usr/bin/env python3
"""Print a read-only, limited inventory of the mounted Minecraft data volume.

The output includes world directory names and plugin fingerprints, so it remains operator-specific. It never
copies player lists, raw configuration files, host identifiers, filesystem UUIDs, or Floodgate key contents.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from _common import read_mc_version_from_history, read_plugin_version, sha256_of_file

DEFAULT_DATA_ROOT = Path("/srv/minecraft")
PLUGIN_FILES: dict[str, tuple[str, ...]] = {
    "floodgate": ("Floodgate-Spigot.jar", "floodgate-spigot.jar"),
    "geyser": ("Geyser-Spigot.jar",),
    "viaversion": ("ViaVersion.jar",),
    "multiverse-core": ("Multiverse-Core.jar",),
    "multiverse-inventories": ("Multiverse-Inventories.jar",),
    "multiverse-portals": ("Multiverse-Portals.jar",),
}
CONFIG_FILES: dict[str, str] = {
    "geyser": "Geyser-Spigot/config.yml",
    "floodgate": "floodgate/config.yml",
    "multiverse-worlds": "Multiverse-Core/worlds.yml",
    "multiverse-portals": "Multiverse-Portals/portals.yml",
    "multiverse-inventories": "Multiverse-Inventories/groups.yml",
}
BOOLEAN_PROPERTIES = frozenset({"white-list", "enforce-whitelist", "online-mode"})
INTEGER_PROPERTIES = frozenset({"max-players", "server-port", "view-distance"})
ENUM_PROPERTIES: dict[str, frozenset[str]] = {
    "difficulty": frozenset({"peaceful", "easy", "normal", "hard", "0", "1", "2", "3"}),
    "gamemode": frozenset({"survival", "creative", "adventure", "spectator", "0", "1", "2", "3"}),
}


class CaptureError(RuntimeError):
    """Report a missing or unreadable source for the state inventory.

    Args:
        message: Details of the source or property that could not be inspected.
    """


def regular_file_exists(path: Path) -> bool:
    """Check whether a path is a regular file without reading its contents.

    Args:
        path: File to inspect.

    Returns:
        Whether the regular file exists.

    Raises:
        CaptureError: When the path cannot be inspected or is not a regular file.
    """
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CaptureError(f"cannot inspect {path}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise CaptureError(f"{path} is not a regular file")
    return True


def read_safe_properties(path: Path) -> dict[str, bool | int | str]:
    """Read only validated, non-secret server properties.

    Args:
        path: ``server.properties`` file.

    Returns:
        Validated allowlisted properties; other keys and comments are omitted.

    Raises:
        CaptureError: When a selected property is malformed or the file cannot be read.
    """
    properties: dict[str, bool | int | str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CaptureError(f"cannot read {path}: {exc}") from exc
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key.startswith("#"):
            continue
        key, value = key.strip(), value.strip()
        if key in BOOLEAN_PROPERTIES:
            if value not in {"true", "false"}:
                raise CaptureError(f"invalid boolean value for {key} in {path}")
            properties[key] = value == "true"
        elif key in INTEGER_PROPERTIES:
            if not value.isascii() or not value.isdigit():
                raise CaptureError(f"invalid numeric value for {key} in {path}")
            properties[key] = int(value)
        elif key in ENUM_PROPERTIES:
            if value not in ENUM_PROPERTIES[key]:
                raise CaptureError(f"unrecognized value for {key} in {path}")
            properties[key] = value
    return properties


def capture_state(data_root: Path, *, require_mount: bool = True) -> dict[str, object]:
    """Capture a limited inventory without modifying the server or its data.

    Args:
        data_root: Root of the Minecraft data volume.
        require_mount: Refuse to inspect the target unless it is a mountpoint.

    Returns:
        JSON-serializable Paper, world, plugin, and configuration-presence metadata.

    Raises:
        CaptureError: When the volume or required server data cannot be inspected.
        OSError: When reading a plugin or listing server directories fails.
    """
    if require_mount and not os.path.ismount(data_root):
        raise CaptureError(f"{data_root} is not a mountpoint; refusing to capture the wrong filesystem")
    server_dir = data_root / "server"
    if not server_dir.is_dir():
        raise CaptureError(f"server directory is missing or inaccessible: {server_dir}")
    plugins_dir = server_dir / "plugins"
    if not plugins_dir.is_dir():
        raise CaptureError(f"plugins directory is missing or inaccessible: {plugins_dir}")

    history = read_mc_version_from_history(server_dir)
    plugins: dict[str, object] = {}
    for label, candidates in PLUGIN_FILES.items():
        present = [plugins_dir / filename for filename in candidates if regular_file_exists(plugins_dir / filename)]
        if len(present) > 1:
            raise CaptureError(f"multiple jars found for {label}; review {plugins_dir}")
        if not present:
            plugins[label] = None
            continue
        jar = present[0]
        plugins[label] = {"filename": jar.name, "version": read_plugin_version(jar), "sha256": sha256_of_file(jar)}

    worlds = sorted(
        entry.name
        for entry in server_dir.iterdir()
        if not entry.is_symlink() and entry.is_dir() and regular_file_exists(entry / "level.dat")
    )
    configuration_files = {
        label: regular_file_exists(plugins_dir / relative_path) for label, relative_path in CONFIG_FILES.items()
    }
    return {
        "schema_version": 1,
        "paper": {
            "history_recognized": history is not None,
            "minecraft_version": history[0] if history else None,
            "build": history[1] if history else None,
            "jar_present": regular_file_exists(server_dir / "paper.jar"),
        },
        "worlds": worlds,
        "plugins": plugins,
        "floodgate_key_present": regular_file_exists(plugins_dir / "floodgate" / "key.pem"),
        "properties": read_safe_properties(server_dir / "server.properties"),
        "plugin_configuration_present": configuration_files,
    }


def main(argv: list[str] | None = None) -> int:
    """Print the inventory as JSON for operator-controlled local storage.

    Args:
        argv: Optional command-line arguments; ``None`` uses the process arguments.

    Returns:
        Zero on success, or two if the volume cannot be inspected safely.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT, help="Mounted Minecraft data volume.")
    args = parser.parse_args(argv)
    try:
        result = capture_state(args.data_root)
    except (CaptureError, OSError) as exc:
        print(f"capture-state: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
