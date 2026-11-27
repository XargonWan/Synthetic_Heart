"""Skins shipped as extensions: put their files where the Skins tab looks.

A skin extension (``provides.skins``, no ``entry``) is files only. While it is installed
and on, its folder is copied into the skins directory under the skin's name, with a marker
(``.synth-extension``) saying whose it is; when it is removed or turned off, exactly that
folder goes away. A folder without the marker (Rei, Zero, a skin the user made or
uploaded) is never touched: if a skin extension wants a name such a folder has, it is
skipped with a note.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from core.extensions.manifest import Manifest

MARKER = ".synth-extension"

#: Skins that come with SyntH itself: they are not extensions and cannot be removed.
BUNDLED_SKINS = ("Rei", "Zero")


def default_skins_dir() -> Path:
    """``<app root>/skins`` (what the WebUI serves at ``/skins``)."""
    return Path(__file__).resolve().parents[2] / "skins"


def read_marker(folder: Path) -> dict[str, str] | None:
    try:
        data = json.loads((folder / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("id"), str):
        return {k: str(v) for k, v in data.items()}
    return None


def _copy(source: Path, destination: Path, marker: dict[str, str]) -> None:
    """Copy ``source`` to ``destination`` through a staging folder, then swap it in."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".skin-", dir=destination.parent))
    try:
        target = staging / "skin"
        shutil.copytree(source, target, symlinks=False)
        (target / MARKER).write_text(json.dumps(marker), encoding="utf-8")
        shutil.rmtree(destination, ignore_errors=True)
        os.replace(target, destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def sync_skins(
    skins_dir: Path, installed: dict[str, tuple[Manifest, Path, bool]]
) -> list[str]:
    """Make the skins directory match the installed skin extensions. Returns notes.

    ``installed`` maps an extension id to ``(manifest, install dir, enabled)``. Never
    raises: a skin that cannot be placed is a note, not a failure of the install.
    """
    notes: list[str] = []
    wanted: dict[str, tuple[str, str, Path]] = {}
    for ext_id, (manifest, base, enabled) in installed.items():
        if not enabled or manifest.kind != "skins":
            continue
        for skin in manifest.provides.skins:
            source = (base / skin.path).resolve()
            if not source.is_dir() or base.resolve() not in source.parents:
                notes.append(f"{ext_id}: skin folder {skin.path!r} is missing")
                continue
            wanted[skin.name] = (ext_id, manifest.version, source)
    if not skins_dir.is_dir():
        try:
            skins_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return [f"skins directory unavailable: {exc}"]
    # Folders we put there that are no longer wanted.
    for folder in sorted(skins_dir.iterdir()):
        marker = read_marker(folder) if folder.is_dir() else None
        if marker is not None and folder.name not in wanted:
            shutil.rmtree(folder, ignore_errors=True)
    for name, (ext_id, version, source) in sorted(wanted.items()):
        destination = skins_dir / name
        marker = read_marker(destination) if destination.exists() else None
        if destination.exists() and marker is None:
            notes.append(
                f"{ext_id}: a skin named {name!r} already exists and is not an extension's"
            )
            continue
        if marker == {"id": ext_id, "version": version}:
            continue
        try:
            _copy(source, destination, {"id": ext_id, "version": version})
        except OSError as exc:
            notes.append(f"{ext_id}: skin {name!r} could not be placed: {exc}")
    return notes
