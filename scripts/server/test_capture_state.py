"""Tests for the read-only, limited Minecraft state inventory."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import IO

import pytest

from capture_state import CaptureError, capture_state


def test_capture_reports_worlds_and_plugins_without_copying_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture useful metadata without exposing raw configs, player data, or the Floodgate key.

    Args:
        tmp_path: Temporary data-volume root.
        monkeypatch: Pytest helper for detecting attempts to open the Floodgate key.
    """
    server = tmp_path / "server"
    plugins = server / "plugins"
    (plugins / "floodgate").mkdir(parents=True)
    key = plugins / "floodgate" / "key.pem"
    key.write_text("private-key-secret", encoding="utf-8")
    (plugins / "floodgate" / "config.yml").write_text("password: config-secret\n", encoding="utf-8")
    (server / "server.properties").write_text(
        "online-mode=true\nwhite-list=true\nenforce-whitelist=true\ngamemode=survival\n"
        "difficulty=easy\nmax-players=20\nview-distance=10\nserver-port=25565\n"
        "motd=personal-secret\nrcon.password=password-secret\n",
        encoding="utf-8",
    )
    (server / "version_history.json").write_text(
        '{"currentVersion":"1.21.11-69-94d0c97 (MC: 1.21.11)"}', encoding="utf-8"
    )
    (server / "paper.jar").write_bytes(b"paper")
    (server / "creative").mkdir()
    (server / "creative" / "level.dat").write_bytes(b"world data")
    jar_path = plugins / "Multiverse-Core.jar"
    with zipfile.ZipFile(jar_path, "w") as jar:
        jar.writestr("plugin.yml", "name: Multiverse-Core\nversion: 5.4.0\n")
    before_key = key.read_bytes()
    before_jar = jar_path.read_bytes()

    original_open = Path.open

    def guarded_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[str] | IO[bytes]:
        """Reject attempts to open the key while allowing other file access.

        Args:
            path: File path being opened.
            mode: File access mode.
            buffering: Buffering policy.
            encoding: Optional text encoding.
            errors: Optional text decoding error policy.
            newline: Optional text newline policy.

        Returns:
            The requested file handle for a file other than the key.

        Raises:
            AssertionError: If the capture attempts to open the key.
        """
        if path == key:
            raise AssertionError("capture must not open the Floodgate key")
        return original_open(path, mode, buffering, encoding, errors, newline)

    with monkeypatch.context() as context:
        context.setattr(Path, "open", guarded_open)
        result = capture_state(tmp_path, require_mount=False)
    encoded = json.dumps(result)
    assert result["worlds"] == ["creative"]
    assert result["floodgate_key_present"] is True
    assert result["paper"] == {
        "history_recognized": True,
        "minecraft_version": "1.21.11",
        "build": 69,
        "jar_present": True,
    }
    plugins_result = result["plugins"]
    assert isinstance(plugins_result, dict)
    assert plugins_result["multiverse-core"]["version"] == "5.4.0"
    assert plugins_result["multiverse-core"]["sha256"] == hashlib.sha256(before_jar).hexdigest()
    assert "private-key-secret" not in encoded
    assert "config-secret" not in encoded
    assert "personal-secret" not in encoded
    assert "password-secret" not in encoded
    assert key.read_bytes() == before_key
    assert jar_path.read_bytes() == before_jar


@pytest.mark.parametrize("create_server", [False, True])
def test_capture_refuses_missing_server_directories(tmp_path: Path, create_server: bool) -> None:
    """Report a missing server or plugins directory instead of producing an empty inventory.

    Args:
        tmp_path: Temporary data-volume root.
        create_server: Whether to create the server directory without its plugins directory.
    """
    if create_server:
        (tmp_path / "server").mkdir()
    expected = "plugins directory" if create_server else "server directory"
    with pytest.raises(CaptureError, match=expected):
        capture_state(tmp_path, require_mount=False)


def test_capture_refuses_an_unmounted_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid mistaking files on the root filesystem for the mounted data volume.

    Args:
        tmp_path: Temporary directory used as the data root.
        monkeypatch: Pytest helper for replacing the mountpoint probe.
    """
    monkeypatch.setattr("capture_state.os.path.ismount", lambda path: False)
    with pytest.raises(CaptureError, match="not a mountpoint"):
        capture_state(tmp_path)


def test_capture_rejects_secret_text_in_allowlisted_property(tmp_path: Path) -> None:
    """Reject malformed allowlisted properties rather than emitting arbitrary text.

    Args:
        tmp_path: Temporary data-volume root.
    """
    server = tmp_path / "server"
    (server / "plugins").mkdir(parents=True)
    (server / "server.properties").write_text("online-mode=secret-value\n", encoding="utf-8")
    with pytest.raises(CaptureError, match="invalid boolean"):
        capture_state(tmp_path, require_mount=False)
