"""Reading and checking extension archives (platform milestone M1).

Decision: **one file is one plugin**. An extension archive is a zip file with
**uncompressed (stored) entries**, containing ``synth-extension.toml`` at its
root. This module only *reads and checks* an archive. It never extracts one:
installation is the job of the installer (milestone M6).

The checks here protect the host from a hostile archive that is merely opened to
read its manifest:

* the manifest is read with a size limit (no decompression bombs);
* member names that are absolute, contain ``..``, backslashes or a drive letter
  are rejected (path traversal on a later extraction);
* symbolic links are rejected.

Like :mod:`core.extensions.manifest` this module imports nothing from ``core``.
"""

from __future__ import annotations

import hashlib
import stat
import zipfile
from pathlib import Path, PurePosixPath

from core.extensions.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestError,
    parse_manifest_text,
)

#: A manifest larger than this is refused (it is a small text file).
MAX_MANIFEST_BYTES = 256 * 1024
_CHUNK = 1024 * 1024


def _unsafe_name(name: str) -> str | None:
    """Return why ``name`` is unsafe as an archive member name, or ``None``."""
    if not name:
        return "empty member name"
    if "\\" in name:
        return "contains a backslash"
    if name.startswith("/"):
        return "is an absolute path"
    if len(name) > 1 and name[1] == ":":
        return "starts with a drive letter"
    if "\x00" in name:
        return "contains a NUL byte"
    parts = PurePosixPath(name).parts
    if ".." in parts:
        return "contains '..'"
    return None


def check_archive(path: Path, *, require_stored: bool = True) -> list[str]:
    """Return the structural problems of the archive at ``path`` (empty if fine).

    ``require_stored`` enforces the decision that extension archives are
    uncompressed. It is about simple, fast hashing and signing, not about safety.
    """
    problems: list[str] = []
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"not a readable zip archive: {exc}"]
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if MANIFEST_FILENAME not in names:
            problems.append(f"{MANIFEST_FILENAME} is missing at the archive root")
        seen: set[str] = set()
        for info in infos:
            name = info.filename
            reason = _unsafe_name(name)
            if reason:
                problems.append(f"unsafe member name {name!r}: {reason}")
            if name in seen:
                problems.append(f"duplicate member {name!r}")
            seen.add(name)
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                problems.append(f"member {name!r} is a symbolic link")
            if require_stored and not info.is_dir():
                if info.compress_type != zipfile.ZIP_STORED:
                    problems.append(
                        f"member {name!r} is compressed; extension archives must be uncompressed"
                    )
            if (
                info.filename == MANIFEST_FILENAME
                and info.file_size > MAX_MANIFEST_BYTES
            ):
                problems.append(
                    f"{MANIFEST_FILENAME} is larger than {MAX_MANIFEST_BYTES} bytes"
                )
    return problems


def read_archive_manifest(path: Path, *, require_stored: bool = True) -> Manifest:
    """Validate the archive structure and return its manifest.

    Raises :class:`ManifestError` listing every structural and manifest problem.
    """
    source = str(path)
    problems = check_archive(path, require_stored=require_stored)
    if problems:
        raise ManifestError(source, problems)
    with zipfile.ZipFile(path) as archive:
        with archive.open(MANIFEST_FILENAME) as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError(
            source, [f"{MANIFEST_FILENAME} is larger than {MAX_MANIFEST_BYTES} bytes"]
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError(
            source, [f"{MANIFEST_FILENAME} is not UTF-8: {exc}"]
        ) from exc
    return parse_manifest_text(text, source=f"{source}!{MANIFEST_FILENAME}")


def archive_sha256(path: Path) -> str:
    """Return the SHA-256 of the archive file: its identity in a repository index."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
