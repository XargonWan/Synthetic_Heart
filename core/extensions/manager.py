"""The extension manager: knows which extensions exist and which can start (M1).

Milestone M1 scope: **discovery and resolution only**. The manager reads
manifests (from extension directories and archives), keeps them,
and asks the resolver which can start and in what order. It does **not** launch
processes or touch the running system; that is the gateway and launcher of
milestone M2. Nothing in the existing start-up path calls it yet.

A bad extension never stops the others: every problem found while loading is
recorded as a :class:`LoadIssue` and the scan continues.

Not thread-safe: use it from the event loop (or guard it) like the registries.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.extensions.archive import read_archive_manifest
from core.extensions.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestError,
    load_manifest,
)
from core.extensions.resolver import (
    CORE_API_VERSION,
    Issue,
    Resolution,
    check_install,
    resolve,
)

#: Archive suffixes the scanner treats as extension archives. The final file
#: extension of an extension package is still to be decided, so this is a
#: parameter of :meth:`ExtensionManager.scan_directory`, not a commitment.
DEFAULT_ARCHIVE_SUFFIXES: tuple[str, ...] = (".zip",)


@dataclass(frozen=True)
class LoadIssue:
    """Something that went wrong while reading one extension source."""

    source: str
    message: str


class ExtensionManager:
    """Registry of extension manifests with discovery and resolution."""

    def __init__(self) -> None:
        self._manifests: dict[str, Manifest] = {}
        self._sources: dict[str, str] = {}
        self._load_issues: list[LoadIssue] = []

    # ------------------------------------------------------------------ state

    @property
    def manifests(self) -> Mapping[str, Manifest]:
        """A copy of the registered manifests by extension id."""
        return dict(self._manifests)

    @property
    def load_issues(self) -> tuple[LoadIssue, ...]:
        """Problems found while reading sources, in the order they happened."""
        return tuple(self._load_issues)

    def source_of(self, extension_id: str) -> str | None:
        """Where the manifest of ``extension_id`` was read from."""
        return self._sources.get(extension_id)

    # ----------------------------------------------------------- registration

    def add_manifest(self, manifest: Manifest, source: str) -> bool:
        """Register a manifest. A duplicate id is refused and recorded.

        The first registration wins, so the result does not depend on a later
        source silently replacing an extension that is already known.
        """
        existing = self._sources.get(manifest.id)
        if manifest.id in self._manifests:
            self._load_issues.append(
                LoadIssue(
                    source,
                    f"extension id {manifest.id!r} is already registered from {existing}",
                )
            )
            return False
        self._manifests[manifest.id] = manifest
        self._sources[manifest.id] = source
        return True

    def forget(self, extension_id: str) -> None:
        """Drop an extension that was uninstalled or is about to be re-read."""
        self._manifests.pop(extension_id, None)
        self._sources.pop(extension_id, None)

    def add_directory(self, path: Path) -> Manifest | None:
        """Register an extension from a directory containing a manifest."""
        try:
            manifest = load_manifest(path / MANIFEST_FILENAME)
        except ManifestError as exc:
            self._load_issues.extend(LoadIssue(str(path), e) for e in exc.errors)
            return None
        return manifest if self.add_manifest(manifest, str(path)) else None

    def add_archive(
        self, path: Path, *, require_stored: bool = True
    ) -> Manifest | None:
        """Register an extension from a single-file archive."""
        try:
            manifest = read_archive_manifest(path, require_stored=require_stored)
        except ManifestError as exc:
            self._load_issues.extend(LoadIssue(str(path), e) for e in exc.errors)
            return None
        return manifest if self.add_manifest(manifest, str(path)) else None

    def scan_directory(
        self,
        root: Path,
        *,
        archive_suffixes: tuple[str, ...] = DEFAULT_ARCHIVE_SUFFIXES,
    ) -> list[Manifest]:
        """Register every extension found directly under ``root``.

        A child directory with a manifest is an extension in development layout;
        a child file with one of ``archive_suffixes`` is a packed extension.
        Returns the manifests registered by this call.
        """
        added: list[Manifest] = []
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            self._load_issues.append(
                LoadIssue(str(root), f"cannot read directory: {exc}")
            )
            return added
        for child in children:
            if child.name.startswith((".", "_")):
                continue
            manifest: Manifest | None = None
            if child.is_dir() and (child / MANIFEST_FILENAME).is_file():
                manifest = self.add_directory(child)
            elif child.is_file() and child.suffix.lower() in archive_suffixes:
                manifest = self.add_archive(child)
            if manifest is not None:
                added.append(manifest)
        return added

    # ------------------------------------------------------------- resolution

    def resolve(self, *, core_api_version: str = CORE_API_VERSION) -> Resolution:
        """Which registered extensions can start, in what order, and why not."""
        return resolve(self._manifests, core_api_version=core_api_version)

    def check_install(
        self, candidate: Manifest, *, core_api_version: str = CORE_API_VERSION
    ) -> list[Issue]:
        """Problems installing ``candidate`` would cause; empty means safe."""
        return check_install(
            candidate, self._manifests, core_api_version=core_api_version
        )
