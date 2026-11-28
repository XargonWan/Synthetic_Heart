"""A Node.js runtime for the Minecraft Vessel, fetched when it is first needed.

The Docker image carries no Node.js: only the Minecraft Vessel's Mineflayer bridge uses it,
and a runtime nobody asked for is 200 MB in every installation. When the Vessel is switched
on and ``node`` is not on the PATH, this module downloads the pinned official build into the
data volume (``<data>/node/``, so it survives container recreation), checks its SHA-256
against the value pinned here, and unpacks it. Nothing for the operator to do.

The pin (version and one hash per architecture) is part of this file on purpose: the
download is only accepted if it is exactly the build reviewed here. Linux only (the image's
platform); elsewhere the operator installs Node themselves, as before.

When the Vessel becomes an extension, this moves with it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

NODE_VERSION = "v22.23.5"
#: SHA-256 of ``node-<version>-linux-<arch>.tar.xz`` (from nodejs.org's SHASUMS256.txt).
NODE_SHA256 = {
    "x64": "cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8",
    "arm64": "2babb9eb5264860d540849f4b064a6fbdebcdc323657d9d382ce23d313fd5e09",
}
DOWNLOAD_BASE = "https://nodejs.org/dist"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
_DONE = ".synth-complete"


def _arch() -> str | None:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return None


def install_root() -> Path:
    from core.app_paths import data_root

    return data_root() / "node"


def _installed_dir() -> Path | None:
    arch = _arch()
    if arch is None:
        return None
    return install_root() / f"node-{NODE_VERSION}-linux-{arch}"


def find_node() -> tuple[str | None, str | None]:
    """``(node, npm)``: from the PATH, else from the runtime fetched earlier."""
    node, npm = shutil.which("node"), shutil.which("npm")
    if node and npm:
        return node, npm
    directory = _installed_dir()
    if directory is not None and (directory / _DONE).is_file():
        return str(directory / "bin" / "node"), str(directory / "bin" / "npm")
    return None, None


def environment_for(node: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """``base`` (default: this process's environment) with node's folder first on the
    PATH: ``npm`` is a script that starts ``node`` by name."""
    env = dict(os.environ if base is None else base)
    folder = str(Path(node).parent)
    env["PATH"] = folder + os.pathsep + env.get("PATH", "")
    return env


def _download(url: str, target: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, open(target, "wb") as out:  # noqa: S310 - https, pinned hash
        size = 0
        while chunk := response.read(1 << 20):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise RuntimeError("the Node.js download is larger than expected")
            out.write(chunk)


def _fetch_and_unpack() -> Path:
    arch = _arch()
    directory = _installed_dir()
    if arch is None or directory is None:
        raise RuntimeError(f"no Node.js build for this CPU ({platform.machine()})")
    root = install_root()
    root.mkdir(parents=True, exist_ok=True)
    name = f"node-{NODE_VERSION}-linux-{arch}"
    url = f"{DOWNLOAD_BASE}/{NODE_VERSION}/{name}.tar.xz"
    with tempfile.TemporaryDirectory(dir=root, prefix=".fetch-") as scratch:
        archive = Path(scratch) / "node.tar.xz"
        _download(url, archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != NODE_SHA256[arch]:
            raise RuntimeError(
                f"the Node.js download does not match the pinned SHA-256 ({digest[:12]}...)"
            )
        with tarfile.open(archive) as bundle:
            bundle.extractall(scratch, filter="data")
        shutil.rmtree(directory, ignore_errors=True)
        shutil.move(str(Path(scratch) / name), str(directory))
    (directory / _DONE).write_text(NODE_VERSION)
    return directory


async def ensure_node() -> tuple[str | None, str | None, str]:
    """``(node, npm, detail)``: what is there, fetching it first if it is not.

    ``detail`` says why there is none (empty when there is)."""
    node, npm = find_node()
    if node and npm:
        return node, npm, ""
    if not sys.platform.startswith("linux"):
        return (
            None,
            None,
            "node/npm not found in PATH (install Node.js on this platform)",
        )
    try:
        await asyncio.to_thread(_fetch_and_unpack)
    except Exception as exc:  # noqa: BLE001 - reported to the operator, never raised
        return (
            None,
            None,
            f"could not fetch Node.js {NODE_VERSION}: {type(exc).__name__}: {exc}",
        )
    node, npm = find_node()
    return node, npm, "" if node and npm else "Node.js was fetched but is not usable"
