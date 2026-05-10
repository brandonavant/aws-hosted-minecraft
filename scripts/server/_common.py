"""Shared API/IO primitives for ``install.py`` and ``update.py``.

This module is stdlib-only and side-effect-free at import time. It centralizes the artifact-metadata fetchers
(PaperMC Fill v3, Geyser/Floodgate v2, Hangar v1), the supporting HTTP/sha256/file helpers, and the on-disk
version-discovery readers that both the provisioner and the nightly updater depend on. The split is by side
effects: anything that runs apt, useradd, chown, or systemctl stays in ``install.py``; anything that just
fetches bytes or parses bytes lives here.

Both ``install.py`` and ``update.py`` are deployed to the same directory on the live host (``/usr/local/sbin``
for the updater path; the repo clone for ``install.py``), so a sibling ``_common.py`` is reachable via the
default script-directory import path. Renaming to a package would force a wrapper or PYTHONPATH gymnastics
without buying anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# --- Constants ---------------------------------------------------------------

PAPER_API_BASE = "https://fill.papermc.io/v3/projects/paper"
GEYSER_API_BASE = "https://download.geysermc.org/v2/projects/geyser"
FLOODGATE_API_BASE = "https://download.geysermc.org/v2/projects/floodgate"
HANGAR_API_BASE = "https://hangar.papermc.io/api/v1/projects"
HTTP_TIMEOUT_S = 30
USER_AGENT = "aws-hosted-minecraft/0.1 (+https://github.com/brandonavant/aws-hosted-minecraft)"

PAPER_DOWNLOAD_KEY = "server:default"
GEYSER_LIKE_DOWNLOAD_KEY = "spigot"
HANGAR_DOWNLOAD_PLATFORM = "PAPER"

VERSION_HISTORY_FILE = "version_history.json"
_VERSION_HISTORY_RE = re.compile(r'"currentVersion"\s*:\s*"git-Paper-(?P<build>\d+)\s*\(MC:\s*(?P<mc>[^)]+)\)"')
_PLUGIN_YML_VERSION_RE = re.compile(r"^version:\s*(?P<version>.+?)\s*$", re.MULTILINE)


# --- Exceptions --------------------------------------------------------------


class DownloadError(RuntimeError):
    """Raised when an HTTP fetch fails after retries or returns a malformed body."""


class ChecksumMismatchError(RuntimeError):
    """Raised when a downloaded artifact's sha256 does not match its expected hash."""


# --- Datatypes ---------------------------------------------------------------


@dataclass(frozen=True)
class JarArtifact:
    """A jar to download and place under the server tree.

    Attributes:
        label: Short human-readable label for log messages (e.g. ``"paper"``).
        version: Human-readable version string for log messages (e.g. ``"1.21.8-60"``, ``"2.10.0-1143"``,
            ``"5.9.1"``).
        download_url: Direct HTTPS URL to the jar.
        sha256: Expected sha256 hex digest of the downloaded bytes.
        filename: Final on-disk filename (not necessarily the URL's basename).
    """

    label: str
    version: str
    download_url: str
    sha256: str
    filename: str


# --- HTTP helpers ------------------------------------------------------------


def http_get(url: str) -> bytes:
    """Fetch ``url`` and return the body bytes.

    Args:
        url: HTTPS URL to fetch.

    Returns:
        Raw response body.

    Raises:
        DownloadError: When the request fails or the response is non-2xx.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:  # noqa: S310 — https only.
            if response.status >= 400:
                raise DownloadError(f"GET {url} returned HTTP {response.status}")
            return response.read()
    except urllib.error.URLError as exc:
        raise DownloadError(f"GET {url} failed: {exc}") from exc


def http_get_json(url: str) -> dict | list:
    """Fetch ``url`` and parse the body as JSON.

    Args:
        url: HTTPS URL returning JSON.

    Returns:
        The decoded JSON document.

    Raises:
        DownloadError: When the fetch fails or the body is not valid JSON.
    """
    body = http_get(url)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DownloadError(f"GET {url}: response was not UTF-8 JSON ({exc})") from exc


def http_get_text(url: str) -> str:
    """Fetch ``url`` and decode the body as UTF-8 text.

    Args:
        url: HTTPS URL returning text.

    Returns:
        Decoded body as a string.

    Raises:
        DownloadError: When the fetch fails or the body is not valid UTF-8.
    """
    body = http_get(url)
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DownloadError(f"GET {url}: response was not UTF-8 ({exc})") from exc


def http_download_to_file(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest``, replacing any existing file.

    The download is written to a sibling tempfile then atomically renamed so a crash never leaves a half-written
    jar in place.

    Args:
        url: HTTPS URL to download.
        dest: Destination path. Parent directory must exist.

    Raises:
        DownloadError: When the request fails.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:  # noqa: S310 — https only.
            if response.status >= 400:
                raise DownloadError(f"GET {url} returned HTTP {response.status}")
            with tmp.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except urllib.error.URLError as exc:
        if tmp.exists():
            tmp.unlink()
        raise DownloadError(f"download {url} failed: {exc}") from exc
    tmp.replace(dest)


# --- SHA256 helpers ----------------------------------------------------------


def sha256_of_file(path: Path) -> str:
    """Compute the sha256 hex digest of a file's bytes.

    Args:
        path: Path to a regular file.

    Returns:
        Lowercase hex digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- Version discovery -------------------------------------------------------


def read_mc_version_from_history(server_dir: Path) -> tuple[str, int] | None:
    """Read the running MC version + Paper build from ``version_history.json``.

    Paper writes ``version_history.json`` in the server directory after the first start; the file's
    ``currentVersion`` field looks like ``"git-Paper-60 (MC: 1.21.8)"``.

    Args:
        server_dir: Paper server directory.

    Returns:
        A ``(mc_version, paper_build)`` tuple, or ``None`` when the file does not exist or the field is missing
        / malformed.
    """
    history_path = server_dir / VERSION_HISTORY_FILE
    if not history_path.exists():
        return None
    try:
        text = history_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _VERSION_HISTORY_RE.search(text)
    if not match:
        return None
    try:
        build = int(match["build"])
    except ValueError:
        return None
    return match["mc"].strip(), build


def read_plugin_version(jar_path: Path) -> str | None:
    """Extract the ``version`` field from a plugin jar's ``plugin.yml``.

    Reads the jar's local ``plugin.yml`` entry via ``unzip -p``. ``unzip`` is in ``install.py``'s apt-install
    list so it is always present after a fresh provision.

    Real plugin.yml shapes (sampled from current upstream jars):

    - Geyser-Spigot: ``version: 2.10.0-SNAPSHOT`` (no build number embedded).
    - floodgate-spigot: ``version: 2.2.5-SNAPSHOT (b132-5a72b6a)`` (build embedded as ``b<N>``).
    - ViaVersion: ``version: "5.9.1"`` (yaml-quoted; clean semver).

    Args:
        jar_path: Path to the plugin jar.

    Returns:
        The raw version string with surrounding quotes stripped (e.g. ``"2.10.0-SNAPSHOT"``,
        ``"2.2.5-SNAPSHOT (b132-5a72b6a)"``, ``"5.9.1"``), or ``None`` when the jar lacks ``plugin.yml`` /
        the version field, or ``unzip`` is not installed.
    """
    if not shutil.which("unzip"):
        return None
    proc = subprocess.run(  # noqa: S603 — argv as list, no shell.
        ["unzip", "-p", str(jar_path), "plugin.yml"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    match = _PLUGIN_YML_VERSION_RE.search(proc.stdout)
    if not match:
        return None
    return match["version"].strip().strip("'\"")


# --- Paper artifact metadata -------------------------------------------------


def fetch_paper_artifact(mc_version: str, build: int | str = "latest") -> JarArtifact:
    """Fetch metadata for one Paper build and return a ``JarArtifact``.

    Calls the PaperMC Fill v3 API (``GET /v3/projects/paper/versions/{mc_version}/builds/{build}``) and pulls
    out the ``server:default`` download.

    Args:
        mc_version: Minecraft version (e.g. ``"1.21.8"``).
        build: Paper build number, or the literal string ``"latest"``.

    Returns:
        A populated ``JarArtifact``.

    Raises:
        DownloadError: When the API call fails or the expected fields are missing from the response.
    """
    url = f"{PAPER_API_BASE}/versions/{mc_version}/builds/{build}"
    payload = http_get_json(url)
    return _paper_artifact_from_build_payload(mc_version, payload)


def _paper_artifact_from_build_payload(mc_version: str, payload: object) -> JarArtifact:
    """Convert a Paper build response into a ``JarArtifact``.

    Args:
        mc_version: MC version the build belongs to (used in the label).
        payload: Decoded JSON from the Fill v3 single-build endpoint.

    Returns:
        A populated ``JarArtifact``.

    Raises:
        DownloadError: When the payload shape is unexpected.
    """
    if not isinstance(payload, dict):
        raise DownloadError(f"paper build response was not an object: {type(payload).__name__}")
    build_id = payload.get("id")
    downloads = payload.get("downloads")
    if not isinstance(downloads, dict) or PAPER_DOWNLOAD_KEY not in downloads:
        raise DownloadError(f"paper build {build_id!r}: missing downloads.{PAPER_DOWNLOAD_KEY!r}")
    entry = downloads[PAPER_DOWNLOAD_KEY]
    if not isinstance(entry, dict):
        raise DownloadError(f"paper build {build_id!r}: downloads.{PAPER_DOWNLOAD_KEY!r} is not an object")
    sha256 = entry.get("checksums", {}).get("sha256") if isinstance(entry.get("checksums"), dict) else None
    download_url = entry.get("url")
    filename = entry.get("name", "paper.jar")
    if not isinstance(sha256, str) or not isinstance(download_url, str) or not isinstance(filename, str):
        raise DownloadError(f"paper build {build_id!r}: missing url / sha256 / name fields")
    return JarArtifact(
        label="paper",
        version=f"{mc_version}-{build_id}",
        download_url=download_url,
        sha256=sha256,
        filename=filename,
    )


# --- Geyser / Floodgate artifact metadata -----------------------------------


def fetch_geyser_like_artifact(
    project_label: str,
    api_base: str,
    *,
    version: str | None,
    build: int | None,
) -> JarArtifact:
    """Fetch metadata for a Geyser-style project build (geyser or floodgate).

    Both projects share the v2 ``download.geysermc.org`` API shape. When ``version`` is ``None``, the latest
    version published by the API is used; same for ``build`` (latest in that version's build list — last
    element of the ``builds`` array, which is sorted ascending).

    Args:
        project_label: ``"geyser"`` or ``"floodgate"``.
        api_base: Project base URL.
        version: Pin to this version, or ``None`` for the latest published.
        build: Pin to this numeric build, or ``None`` for the latest in version.

    Returns:
        A populated ``JarArtifact`` for the spigot variant.

    Raises:
        DownloadError: On API failure or unexpected shape.
    """
    if version is None:
        project_payload = http_get_json(api_base)
        if not isinstance(project_payload, dict):
            raise DownloadError(f"{project_label} project response was not an object")
        versions = project_payload.get("versions")
        if not isinstance(versions, list) or not versions:
            raise DownloadError(f"{project_label} project response: empty 'versions' list")
        version = str(versions[-1])
    if build is None:
        version_payload = http_get_json(f"{api_base}/versions/{version}")
        if not isinstance(version_payload, dict):
            raise DownloadError(f"{project_label} version {version!r}: response was not an object")
        builds = version_payload.get("builds")
        if not isinstance(builds, list) or not builds:
            raise DownloadError(f"{project_label} version {version!r}: empty 'builds' list")
        build = int(builds[-1])
    build_payload = http_get_json(f"{api_base}/versions/{version}/builds/{build}")
    return _geyser_like_artifact_from_payload(project_label, api_base, build_payload)


def _geyser_like_artifact_from_payload(project_label: str, api_base: str, payload: object) -> JarArtifact:
    """Convert a Geyser/Floodgate single-build payload into a ``JarArtifact``.

    Args:
        project_label: ``"geyser"`` or ``"floodgate"``.
        api_base: Project base URL (used to construct the download URL).
        payload: Decoded JSON from the v2 single-build endpoint.

    Returns:
        A populated ``JarArtifact``.

    Raises:
        DownloadError: When the payload shape is unexpected.
    """
    if not isinstance(payload, dict):
        raise DownloadError(f"{project_label} build response was not an object")
    version = payload.get("version")
    build = payload.get("build")
    downloads = payload.get("downloads")
    if not isinstance(version, str) or not isinstance(build, int) or not isinstance(downloads, dict):
        raise DownloadError(f"{project_label} build response: missing version / build / downloads")
    entry = downloads.get(GEYSER_LIKE_DOWNLOAD_KEY)
    if not isinstance(entry, dict):
        raise DownloadError(f"{project_label} build {build}: downloads.{GEYSER_LIKE_DOWNLOAD_KEY!r} missing")
    sha256 = entry.get("sha256")
    filename = entry.get("name")
    if not isinstance(sha256, str) or not isinstance(filename, str):
        raise DownloadError(f"{project_label} build {build}: spigot entry missing sha256 / name")
    download_url = f"{api_base}/versions/{version}/builds/{build}/downloads/{GEYSER_LIKE_DOWNLOAD_KEY}"
    return JarArtifact(
        label=project_label,
        version=f"{version}-{build}",
        download_url=download_url,
        sha256=sha256,
        filename=filename,
    )


# --- ViaVersion (Hangar) artifact metadata -----------------------------------


def fetch_viaversion_artifact(version_name: str | None) -> JarArtifact:
    """Fetch metadata for ViaVersion via the Hangar API.

    Hangar's ``/latest?channel=Release`` endpoint returns a plain version name (e.g. ``"5.9.1"``); per-version
    metadata comes from ``/versions/{name}``. When ``version_name`` is ``None``, the latest Release-channel
    version is used.

    Args:
        version_name: Pin to this Hangar version name, or ``None`` for latest.

    Returns:
        A populated ``JarArtifact`` for the PAPER platform download.

    Raises:
        DownloadError: On API failure or unexpected shape.
    """
    project_url = f"{HANGAR_API_BASE}/ViaVersion/ViaVersion"
    if version_name is None:
        version_name = http_get_text(f"{project_url}/latest?channel=Release").strip()
        if not version_name:
            raise DownloadError("Hangar /latest?channel=Release returned an empty body")
    payload = http_get_json(f"{project_url}/versions/{version_name}")
    return _viaversion_artifact_from_payload(payload)


def _viaversion_artifact_from_payload(payload: object) -> JarArtifact:
    """Convert a Hangar single-version payload into a ``JarArtifact``.

    Args:
        payload: Decoded JSON from ``/api/v1/projects/ViaVersion/ViaVersion/versions/{name}``.

    Returns:
        A populated ``JarArtifact``.

    Raises:
        DownloadError: When the payload shape is unexpected.
    """
    if not isinstance(payload, dict):
        raise DownloadError("Hangar version response was not an object")
    name = payload.get("name")
    downloads = payload.get("downloads")
    if not isinstance(name, str) or not isinstance(downloads, dict):
        raise DownloadError("Hangar version response: missing name / downloads")
    entry = downloads.get(HANGAR_DOWNLOAD_PLATFORM)
    if not isinstance(entry, dict):
        raise DownloadError(f"Hangar version {name!r}: downloads.{HANGAR_DOWNLOAD_PLATFORM!r} missing")
    file_info = entry.get("fileInfo")
    if not isinstance(file_info, dict):
        raise DownloadError(f"Hangar version {name!r}: PAPER entry missing fileInfo")
    sha256 = file_info.get("sha256Hash")
    filename = file_info.get("name")
    download_url = entry.get("downloadUrl")
    if not isinstance(sha256, str) or not isinstance(filename, str) or not isinstance(download_url, str):
        raise DownloadError(f"Hangar version {name!r}: missing sha256 / name / downloadUrl")
    return JarArtifact(
        label="viaversion",
        version=name,
        download_url=download_url,
        sha256=sha256,
        filename="ViaVersion.jar",
    )
