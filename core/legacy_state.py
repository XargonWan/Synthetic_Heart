"""Adopt state that older Docker-era builds wrote to the wrong place.

For most of its life SyntH only ran in a container, where the application lives
at ``/app`` and persistent state at ``/config``. On Windows those are *not*
absolute paths: ``Path("/config")`` means ``\\config``, i.e. the root of whatever
drive the process happened to start on. So a native run created ``D:\\config``
and ``D:\\app`` and quietly kept real state there (the encrypted-endpoint secret,
uploaded attachments, generated TLS material, migration bookkeeping).

Newer code resolves those paths properly (see :mod:`core.app_paths`), but an
existing install must not look like it lost its data. This module finds that
leftover state once, copies anything that is missing into the resolved data
root, records what it did, and never touches the originals.

Rules:

* **Copy, never move or delete.** The user decides what happens to the old tree.
* **Never overwrite.** A file that already exists at the destination wins.
* **Bounded.** Copying is capped by count and by total size, so a first launch
  cannot hang on gigabytes of old attachments. Whatever is skipped is reported
  with the exact path so it can be moved by hand.
* **Once.** A marker file records the migration, so later launches skip the scan.
* **Fail-safe.** Any error is reported and swallowed; startup is never blocked.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import string
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.app_paths import data_root, in_container

#: Directory names the container paths degraded into on Windows.
LEGACY_DIR_NAMES: tuple[str, ...] = ("config", "app")

#: Items worth adopting, in priority order (the secret first: it decrypts the
#: stored endpoint keys, so losing it is the one genuinely painful outcome).
ADOPTABLE_FILES: tuple[str, ...] = (".synth_secret", "db-cutover-state.json")
ADOPTABLE_DIRS: tuple[str, ...] = ("attachments", "uploads", "ssl", "storage")

#: Contents that mark a directory as SyntH state rather than an unrelated folder
#: that happens to be called ``app`` or ``config``.
STATE_MARKERS: tuple[str, ...] = (
    ".synth_secret",
    "db-cutover-state.json",
    *ADOPTABLE_DIRS,
)

MARKER_NAME = "legacy-state-migration.json"

#: Copy budget: past either limit the remaining items are reported, not copied.
MAX_FILES = 2000
MAX_BYTES = 512 * 1024 * 1024


@dataclass
class LegacySource:
    """A leftover directory and what it holds."""

    path: Path
    files: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)

    @property
    def is_state(self) -> bool:
        return bool(self.files or self.dirs)

    def as_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "files": self.files, "dirs": self.dirs}


@dataclass
class MigrationReport:
    """What the migration found and did.

    In a dry run ``copied`` lists the files a real run *would* adopt, so the CLI
    can report the same set of paths either way.
    """

    data_root: Path
    sources: list[LegacySource] = field(default_factory=list)
    copied: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    skipped_budget: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    already_done: bool = False
    dry_run: bool = False

    @property
    def adopted_anything(self) -> bool:
        return bool(self.copied)

    def as_dict(self) -> dict[str, object]:
        return {
            "data_root": str(self.data_root),
            "already_done": self.already_done,
            "dry_run": self.dry_run,
            "sources": [source.as_dict() for source in self.sources],
            "copied": self.copied,
            "skipped_existing": self.skipped_existing,
            "skipped_budget": self.skipped_budget,
            "errors": self.errors,
        }


def container_path_leftovers(explicit: list[str] | None = None) -> list[Path]:
    """Return leftover ``\\config`` / ``\\app`` directories.

    On Windows the container paths degraded to the root of the current drive, so
    every mounted drive is checked. ``explicit`` paths are checked as well, which
    is what the tests use and what ``--source`` exposes on the CLI.
    """
    candidates: list[Path] = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            try:
                if not drive.exists():
                    continue
            except OSError:
                continue
            for name in LEGACY_DIR_NAMES:
                candidates.append(drive / name)
    else:
        # A container-era install on Linux genuinely used /config and /app, so
        # those are only leftovers if the app has since moved elsewhere.
        for name in LEGACY_DIR_NAMES:
            candidates.append(Path("/") / name)

    for raw in explicit or []:
        try:
            candidates.append(Path(raw).expanduser())
        except Exception:
            continue

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def inspect_legacy_dir(path: Path) -> LegacySource | None:
    """Return :class:`LegacySource` when *path* holds SyntH state, else ``None``.

    A directory is only accepted when it contains something recognisable, so an
    unrelated ``C:\\app`` folder is left alone.
    """
    try:
        if not path.is_dir():
            return None
        names = {entry.name for entry in path.iterdir()}
    except OSError:
        return None
    if not names:
        return None
    if not (names & set(STATE_MARKERS)):
        return None
    source = LegacySource(path=path)
    source.files = [name for name in ADOPTABLE_FILES if name in names]
    source.dirs = [name for name in ADOPTABLE_DIRS if name in names]
    return source if source.is_state else None


def _is_own_data_root(path: Path, destination: Path) -> bool:
    """True when the candidate *is* the resolved data root (nothing to adopt)."""
    try:
        return path.resolve() == destination.resolve()
    except Exception:
        return str(path) == str(destination)


def _copy_file(
    source: Path, target: Path, budget: dict[str, int], *, dry_run: bool = False
) -> str:
    """Copy one file if it is missing and within budget. Returns a status word.

    With ``dry_run`` the decision is made but nothing is written, so a dry run
    reports exactly what a real run would do.
    """
    if target.exists():
        return "existing"
    try:
        size = source.stat().st_size
    except OSError:
        size = 0
    if budget["files"] <= 0 or size > budget["bytes"]:
        return "budget"
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    budget["files"] -= 1
    budget["bytes"] -= size
    return "copied"


def migrate_legacy_state(
    *,
    destination: Path | None = None,
    explicit_sources: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> MigrationReport:
    """Adopt leftover container-path state into the resolved data root.

    ``destination`` defaults to :func:`core.app_paths.data_root`. Passing
    ``explicit_sources`` replaces the drive scan entirely (an empty list means
    "scan nothing"), which is what the tests and ``--source`` rely on.

    Once the marker exists the adoption has happened and is not repeated, which
    is what makes this safe to call on every start. A dry run is exempt: it only
    answers "what is there, and what would be adopted", and must stay answerable
    after the real run has already happened (``force`` overrides it for real runs).
    """
    target_root = destination or data_root()
    report = MigrationReport(data_root=target_root, dry_run=dry_run)

    marker = target_root / MARKER_NAME
    if marker.exists() and not force and not dry_run:
        report.already_done = True
        return report

    if in_container():
        # Inside the container the container paths are correct; there is nothing
        # to migrate and touching them would be destructive.
        report.errors.append("running inside the container: migration not applicable")
        return report

    if explicit_sources is not None:
        candidates = [Path(raw).expanduser() for raw in explicit_sources]
    else:
        candidates = container_path_leftovers()

    for candidate in candidates:
        if _is_own_data_root(candidate, target_root):
            continue
        source = inspect_legacy_dir(candidate)
        if source is not None:
            report.sources.append(source)

    if not report.sources:
        if not dry_run and not marker.exists():
            _write_marker(report)
        return report

    budget = {"files": MAX_FILES, "bytes": MAX_BYTES}
    for source in report.sources:
        for name in source.files:
            origin = source.path / name
            try:
                status = _copy_file(origin, target_root / name, budget, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                report.errors.append(f"{origin}: {exc}")
                continue
            _record(report, status, origin, target_root / name)
        for name in source.dirs:
            origin_dir = source.path / name
            for origin in sorted(origin_dir.rglob("*")):
                if not origin.is_file():
                    continue
                relative = origin.relative_to(source.path)
                try:
                    status = _copy_file(
                        origin, target_root / relative, budget, dry_run=dry_run
                    )
                except Exception as exc:  # noqa: BLE001 - reported, never fatal
                    report.errors.append(f"{origin}: {exc}")
                    continue
                _record(report, status, origin, target_root / relative)

    if not dry_run:
        _write_marker(report)
    return report


def _record(report: MigrationReport, status: str, origin: Path, target: Path) -> None:
    if status == "copied":
        report.copied.append(str(target))
    elif status == "existing":
        report.skipped_existing.append(str(target))
    else:
        report.skipped_budget.append(str(origin))


def _write_marker(report: MigrationReport) -> None:
    """Record the migration so later launches skip the scan."""
    payload = {
        "version": 1,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sources": [source.as_dict() for source in report.sources],
        "copied": len(report.copied),
        "skipped_existing": len(report.skipped_existing),
        "skipped_budget": report.skipped_budget,
        "errors": report.errors,
    }
    try:
        report.data_root.mkdir(parents=True, exist_ok=True)
        (report.data_root / MARKER_NAME).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:
        # A marker that cannot be written only means the scan runs again next
        # launch; that is preferable to failing startup.
        pass


def run_startup_migration() -> MigrationReport | None:
    """Run the migration for the application, swallowing every failure.

    Called once from ``main.py`` before the modules that read the secret file are
    imported, so an adopted secret is picked up on the same launch.
    """
    try:
        report = migrate_legacy_state()
    except Exception:
        return None
    try:
        if report.adopted_anything:
            print(
                f"[legacy-state] adopted {len(report.copied)} file(s) from an older "
                f"install into {report.data_root}",
                flush=True,
            )
        if report.skipped_budget:
            print(
                f"[legacy-state] {len(report.skipped_budget)} file(s) were left in place "
                f"(copy budget reached); move them manually from "
                f"{', '.join(str(s.path) for s in report.sources)}",
                flush=True,
            )
        for source in report.sources:
            if source.path.exists():
                print(
                    f"[legacy-state] the old location {source.path} was left untouched "
                    "and can be deleted once you are satisfied everything works",
                    flush=True,
                )
    except Exception:
        pass
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Adopt state written to the container paths by older builds."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report only, copy nothing"
    )
    parser.add_argument(
        "--force", action="store_true", help="ignore a previous migration"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help="only inspect this directory (repeatable); default scans the drives",
    )
    args = parser.parse_args(argv)

    report = migrate_legacy_state(
        explicit_sources=args.source, dry_run=args.dry_run, force=args.force
    )

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
        return 0

    if report.already_done:
        print("Legacy state migration already completed; nothing to do.")
        return 0
    if not report.sources:
        print("No leftover container-path state found.")
        return 0

    verb = "would adopt" if args.dry_run else "adopted"
    print(f"{verb} {len(report.copied)} file(s) into {report.data_root}")
    for source in report.sources:
        print(f"  from {source.path}")
    if report.skipped_existing:
        print(
            f"  {len(report.skipped_existing)} file(s) already present, left as they are"
        )
    if report.skipped_budget:
        print(f"  {len(report.skipped_budget)} file(s) left behind (copy budget)")
    for error in report.errors:
        print(f"  warning: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
