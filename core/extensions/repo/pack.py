"""Building packages and repositories (for authors and publishers).

``pack_extension`` turns an extension directory into one uncompressed archive, with
its Python libraries inside (``lib/``, built from the manifest's requirements) so
installing needs no network and no build tools. Entries are sorted and timestamped
with a fixed date: packing the same tree twice gives the same file, hence the same
SHA-256.

``build_index`` scans a repository directory for archives and writes the signed index.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from core.extensions.archive import archive_sha256, read_archive_manifest
from core.extensions.manifest import MANIFEST_FILENAME, Manifest, load_manifest
from core.extensions.repo.index import (
    INDEX_NAME,
    SCHEMA,
    SIGNATURE_NAME,
    parse_index,
    sign_index,
)
from core.extensions.repo.installer import host_platform

ARCHIVE_SUFFIX = ".synthx"
#: Files and folders that never belong in a package.
_SKIP_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "tests",
    ".DS_Store",
}
_SKIP_SUFFIXES = (".pyc", ".pyo", ".swp", ".orig")
#: Compiled libraries make a package specific to one platform.
_NATIVE_SUFFIXES = (".so", ".pyd", ".dylib", ".dll")
_FIXED_DATE = (2020, 1, 1, 0, 0, 0)


class PackError(RuntimeError):
    """A package or index could not be built."""


def _wanted(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in _SKIP_NAMES for part in relative.parts):
        return False
    return not path.name.endswith(_SKIP_SUFFIXES)


def build_libs(manifest: Manifest, target: Path) -> None:
    """Install the manifest's Python requirements into ``target`` (``lib/``)."""
    if not manifest.python_requirements:
        return
    uv = shutil.which("uv")
    if uv is None:
        raise PackError("uv is needed to build an extension's lib/ (it is not on PATH)")
    done = subprocess.run(
        [uv, "pip", "install", "--target", str(target), *manifest.python_requirements],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise PackError(f"building lib/ failed: {done.stderr.strip()[-400:]}")


def pack_extension(
    source: Path, repo_dir: Path, *, bundle_libs: bool = True
) -> tuple[Path, Manifest]:
    """Pack ``source`` into ``repo_dir/packages/<publisher>/<name>-<version>.synthx``."""
    manifest = load_manifest(source / MANIFEST_FILENAME)
    with tempfile.TemporaryDirectory(prefix="synth-pack-") as scratch:
        tree = Path(scratch) / "tree"
        shutil.copytree(
            source,
            tree,
            ignore=lambda directory, names: [
                n for n in names if n in _SKIP_NAMES or n.endswith(_SKIP_SUFFIXES)
            ],
            symlinks=False,
        )
        if bundle_libs:
            shutil.rmtree(tree / "lib", ignore_errors=True)
            build_libs(manifest, tree / "lib")
        publisher, _, name = manifest.id.partition("/")
        out_dir = repo_dir / "packages" / publisher
        out_dir.mkdir(parents=True, exist_ok=True)
        archive = out_dir / f"{name}-{manifest.version}{ARCHIVE_SUFFIX}"
        files = sorted(p for p in tree.rglob("*") if p.is_file() and not p.is_symlink())
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            for path in files:
                relative = path.relative_to(tree).as_posix()
                info = zipfile.ZipInfo(relative, date_time=_FIXED_DATE)
                info.compress_type = zipfile.ZIP_STORED
                executable = path.stat().st_mode & 0o100
                info.external_attr = (0o755 if executable else 0o644) << 16
                bundle.writestr(info, path.read_bytes())
    # The result must pass the checks the installer will apply.
    read_archive_manifest(archive)
    return archive, manifest


def _platform_of(archive: Path) -> str:
    with zipfile.ZipFile(archive) as bundle:
        native = any(n.endswith(_NATIVE_SUFFIXES) for n in bundle.namelist())
    return host_platform() if native else "any"


def build_index(
    repo_dir: Path,
    *,
    name: str,
    private_key: bytes | None = None,
    default_install: tuple[str, ...] = (),
    url_base: str = "",
    url_above: int = 0,
    suffixes: tuple[str, ...] = (ARCHIVE_SUFFIX,),
) -> Path:
    """Write ``synth-repo.json`` (and its signature) for the archives in ``repo_dir``.

    ``url_base`` and ``url_above``: a package larger than ``url_above`` bytes is
    listed with ``url = url_base + <file name>`` (a release asset), because a Git host
    refuses files that big; the package does not have to be committed.
    """
    if url_base and not url_base.endswith("/"):
        url_base += "/"
    packages = []
    archives = sorted(
        {p for suffix in suffixes for p in repo_dir.rglob(f"*{suffix}") if p.is_file()}
    )
    for archive in archives:
        try:
            manifest = read_archive_manifest(archive)
        except Exception as exc:  # noqa: BLE001 - report which file is bad
            raise PackError(f"{archive}: {exc}") from exc
        packages.append(
            {
                "id": manifest.id,
                "version": manifest.version,
                "file": archive.relative_to(repo_dir).as_posix(),
                "size": archive.stat().st_size,
                "sha256": archive_sha256(archive),
                "core_api": manifest.core_api,
                "platform": _platform_of(archive),
                "name": manifest.name,
                "description": manifest.description,
                "experimental": manifest.experimental,
                "default_install": manifest.id in default_install,
                **(
                    {"url": url_base + archive.name}
                    if url_base and url_above and archive.stat().st_size > url_above
                    else {}
                ),
                "capabilities": list(manifest.permissions.capabilities),
                "authors": list(manifest.authors),
                **({"kind": "skins"} if manifest.kind == "skins" else {}),
            }
        )
    unknown = sorted(set(default_install) - {p["id"] for p in packages})
    if unknown:
        raise PackError(
            f"default_install names packages that are not here: {', '.join(unknown)}"
        )
    document = {
        "schema": SCHEMA,
        "name": name,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "packages": packages,
    }
    raw = json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    parse_index(raw)  # never write an index the installer would refuse
    index_path = repo_dir / INDEX_NAME
    index_path.write_bytes(raw)
    signature_path = repo_dir / SIGNATURE_NAME
    if private_key is not None:
        signature_path.write_bytes(sign_index(raw, private_key))
    else:
        signature_path.unlink(missing_ok=True)
    return index_path


#: A release asset is a plain zip named after its extension.
RELEASE_SUFFIX = ".zip"


def release_asset_name(manifest: Manifest) -> str:
    """``official-telegram.zip`` for ``official/telegram``: the version is inside the
    package (its manifest), never in the name, because the release is rolling."""
    publisher, _, name = manifest.id.partition("/")
    return f"{publisher}-{name}{RELEASE_SUFFIX}"


def build_release(
    sources: Sequence[Path],
    out_dir: Path,
    *,
    name: str,
    private_key: bytes | None = None,
    default_install: tuple[str, ...] = (),
    bundle_libs: bool = True,
) -> list[Path]:
    """Pack every source into ``out_dir`` as one ``<publisher>-<name>.zip`` and write the
    signed catalog (``synth-repo.json`` and ``.sig``) beside them.

    That directory is exactly what a rolling GitHub release carries: each package is an
    asset of its own, the catalog is an asset, and the index names every package by its
    asset name, so a repository of kind ``github`` needs nothing else. Returns the
    files to upload.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob(f"*{RELEASE_SUFFIX}"):
        stale.unlink()
    assets: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="synth-release-") as scratch:
        for source in sources:
            archive, manifest = pack_extension(
                source, Path(scratch), bundle_libs=bundle_libs
            )
            target = out_dir / release_asset_name(manifest)
            if target in assets:
                raise PackError(f"two sources are both {manifest.id}")
            shutil.copyfile(archive, target)
            assets.append(target)
    index = build_index(
        out_dir,
        name=name,
        private_key=private_key,
        default_install=default_install,
        suffixes=(RELEASE_SUFFIX,),
    )
    assets.append(index)
    signature = out_dir / SIGNATURE_NAME
    if signature.is_file():
        assets.append(signature)
    return assets
