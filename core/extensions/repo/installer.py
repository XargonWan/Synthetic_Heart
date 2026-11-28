"""Installing, updating and removing extensions from repositories.

Everything goes through the same path whether a repository is a local directory, a Git
checkout or a web server: read the index, check its signature, pick a version, download
the archive, check its SHA-256 and its structure, check the manifest says what the index
said, check the install would not break anything that works now, extract into a staging
directory, then swap it into place. A failure at any step leaves the installed version
exactly as it was.

Nothing here grants a capability: installing an extension never lets it call the Core.
The operator grants what the manifest declares (``grant``), deliberately.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from core.extensions.archive import archive_sha256, check_archive, read_archive_manifest
from core.extensions.grants import FileGrantStore
from core.extensions.manifest import EXTENSION_ID_RE, Manifest, ManifestError
from core.extensions.repo.config import (
    OFFICIAL_NAME,
    InstalledRecord,
    RepoStore,
    Repository,
    StateStore,
    now,
)
from core.extensions.repo.index import (
    INDEX_NAME,
    MAX_INDEX_BYTES,
    SIGNATURE_NAME,
    IndexError_,
    PackageEntry,
    RepoIndex,
    key_fingerprint,
    parse_index,
    verify_index,
)
from core.extensions.repo.transports import (
    GithubReleaseTransport,
    GitTransport,
    HttpTransport,
    LocalTransport,
    Transport,
    TransportError,
    fetch_url,
)
from core.extensions.repo.github_access import github_token
from core.extensions.resolver import CORE_API_VERSION, check_install

#: Extraction limits: a hostile archive must not fill the disk or the inode table.
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_DEPENDENCY_DEPTH = 8


class InstallError(RuntimeError):
    """An install, update or removal cannot be done. The message says why."""


class UntrustedRepository(InstallError):
    """The repository is not trusted and the operator has not confirmed."""


@dataclass
class Candidate:
    entry: PackageEntry
    repo: Repository
    index: RepoIndex
    signed: bool


@dataclass
class InstallResult:
    installed: list[str] = field(
        default_factory=list
    )  # "id version", dependencies first
    warnings: list[str] = field(default_factory=list)


@dataclass
class Row:
    """One extension as the operator sees it."""

    id: str
    name: str
    installed: str = ""
    available: str = ""
    repo: str = ""
    enabled: bool = False
    update: bool = False
    experimental: bool = False
    default_install: bool = False
    description: str = ""
    authors: list[str] = field(default_factory=list)
    #: ``"skins"`` (files for the Skins tab) or ``"plugin"``.
    kind: str = "plugin"


def host_platform() -> str:
    import platform
    import sys

    return f"{sys.platform}-{platform.machine().lower()}-py{sys.version_info.major}{sys.version_info.minor}"


def _version(text: str) -> Version | None:
    try:
        return Version(text)
    except InvalidVersion:
        return None


class Installer:
    def __init__(
        self,
        root: Path,
        *,
        core_api_version: str = CORE_API_VERSION,
        transport_for: Callable[[Repository], Transport] | None = None,
        skins_dir: Path | None = None,
    ) -> None:
        self.root = Path(root)
        #: Where the skins of installed skin extensions are put (``None``: nowhere).
        self.skins_dir = skins_dir
        self.repos = RepoStore(self.root / "repositories.json")
        self.state = StateStore(self.root / "installed.json")
        self.grants = FileGrantStore(self.root / "grants.json")
        self.core_api_version = core_api_version
        self._transport_for = transport_for or self._default_transport
        self.cache = self.root / "cache"

    # -------------------------------------------------------------- layout

    def install_dir(self, extension_id: str) -> Path:
        return self.root / "installed" / extension_id

    def previous_dir(self, extension_id: str) -> Path:
        return self.root / "previous" / extension_id

    def data_dir(self, extension_id: str) -> Path:
        return self.root / "data" / extension_id

    # ---------------------------------------------------------- repositories

    def _default_transport(self, repo: Repository) -> Transport:
        if repo.kind == "http":
            return HttpTransport(repo.location, token=repo.token or None)
        if repo.kind == "git":
            return GitTransport(repo.location, self.cache / "git", branch=repo.branch)
        if repo.kind == "github":
            return GithubReleaseTransport(
                repo.location.removeprefix("github:"),
                repo.branch or "latest",
                token=repo.token or github_token() or None,
            )
        return LocalTransport(Path(repo.location))

    def load_index(
        self, repo: Repository, *, refresh: bool = False
    ) -> tuple[RepoIndex, bool]:
        """The verified index of ``repo`` and whether it was signed with the pinned key.

        Raises :class:`InstallError` for an unreachable repository, a malformed index, a
        signature that does not match, or a required signature that is missing.
        """
        transport = self._transport_for(repo)
        try:
            if refresh:
                transport.refresh()
            raw = transport.read(INDEX_NAME, MAX_INDEX_BYTES)
        except TransportError as exc:
            raise InstallError(f"repository {repo.name!r}: {exc}") from exc
        signed = False
        if repo.public_key:
            try:
                signature = transport.read(SIGNATURE_NAME, 64 * 1024)
            except TransportError:
                signature = b""
            if signature:
                problem = verify_index(raw, signature, repo.public_key)
                if problem:
                    raise InstallError(f"repository {repo.name!r}: {problem}")
                signed = True
        if repo.require_signature and not signed:
            raise InstallError(
                f"repository {repo.name!r} requires a signature made with its pinned key "
                "and the index has none"
            )
        try:
            return parse_index(raw), signed
        except IndexError_ as exc:
            raise InstallError(f"repository {repo.name!r}: {exc}") from exc

    def _repositories(self, only: str | None = None) -> list[Repository]:
        repos = [r for r in self.repos.load() if r.enabled]
        if only is not None:
            repos = [r for r in repos if r.name == only]
            if not repos:
                raise InstallError(f"no enabled repository named {only!r}")
        return repos

    def available(
        self, *, only: str | None = None, refresh: bool = False
    ) -> tuple[list[Candidate], list[str]]:
        """Every offered version of every extension, and the repositories that failed."""
        found: list[Candidate] = []
        problems: list[str] = []
        for repo in self._repositories(only):
            try:
                index, signed = self.load_index(repo, refresh=refresh)
            except InstallError as exc:
                problems.append(str(exc))
                continue
            found.extend(
                Candidate(entry, repo, index, signed) for entry in index.packages
            )
        return found, problems

    # ------------------------------------------------------------ selecting

    def _acceptable(self, entry: PackageEntry) -> str | None:
        """Why ``entry`` cannot run here, or ``None``."""
        if entry.platform not in ("any", host_platform()):
            return f"built for {entry.platform}, this machine is {host_platform()}"
        if entry.core_api and Version(self.core_api_version) not in SpecifierSet(
            entry.core_api
        ):
            return (
                f"needs Core API {entry.core_api}, this Core is {self.core_api_version}"
            )
        return None

    def choose(
        self,
        extension_id: str,
        *,
        version: str | None = None,
        repo: str | None = None,
        candidates: list[Candidate] | None = None,
    ) -> Candidate:
        """The best installable version, or an :class:`InstallError` saying why not."""
        if not EXTENSION_ID_RE.match(extension_id):
            raise InstallError(
                f"{extension_id!r} is not an extension id (publisher/name)"
            )
        if candidates is None:
            candidates, problems = self.available(only=repo)
        else:
            problems = []
        offered = [c for c in candidates if c.entry.id == extension_id]
        if not offered:
            hint = f" ({'; '.join(problems)})" if problems else ""
            raise InstallError(f"{extension_id} is not offered by any repository{hint}")
        if version is not None:
            wanted = _version(version)
            offered = [
                c
                for c in offered
                if wanted is not None and c.entry.parsed_version == wanted
            ]
            if not offered:
                raise InstallError(
                    f"{extension_id} {version} is not published (only the current versions are kept)"
                )
        usable = [c for c in offered if self._acceptable(c.entry) is None]
        if not usable:
            reasons = sorted({self._acceptable(c.entry) or "" for c in offered})
            raise InstallError(
                f"no version of {extension_id} can run here: {'; '.join(reasons)}"
            )
        best = max(c.entry.parsed_version for c in usable)
        top = [c for c in usable if c.entry.parsed_version == best]
        distinct = {c.entry.sha256 for c in top}
        if len(distinct) > 1 and repo is None:
            names = ", ".join(sorted(c.repo.name for c in top))
            raise InstallError(
                f"{extension_id} {best} differs between repositories ({names}); choose one with --repo"
            )
        return min(top, key=lambda c: c.repo.priority)

    # -------------------------------------------------------------- download

    @staticmethod
    def _kept_locally(candidate: Candidate) -> bool:
        """A local repository that holds the file itself needs no download."""
        repo = candidate.repo
        return (
            repo.kind == "local"
            and (Path(repo.location) / candidate.entry.file).is_file()
        )

    def download(self, candidate: Candidate) -> tuple[Path, Manifest]:
        """Fetch the archive and verify it. Returns its path and its manifest."""
        entry = candidate.entry
        target = self.cache / "downloads" / f"{entry.sha256}.synthx"
        if not (target.is_file() and archive_sha256(target) == entry.sha256):
            try:
                if entry.url and not self._kept_locally(candidate):
                    fetch_url(entry.url, target, entry.size)
                else:
                    self._transport_for(candidate.repo).fetch(
                        entry.file, target, entry.size
                    )
            except TransportError as exc:
                raise InstallError(f"{entry.id}: {exc}") from exc
        if (
            target.stat().st_size != entry.size
            or archive_sha256(target) != entry.sha256
        ):
            target.unlink(missing_ok=True)
            raise InstallError(
                f"{entry.id} {entry.version}: the file does not match the index (size or SHA-256)"
            )
        problems = check_archive(target)
        if problems:
            raise InstallError(
                f"{entry.id}: unsafe or malformed archive: {'; '.join(problems)}"
            )
        try:
            manifest = read_archive_manifest(target)
        except ManifestError as exc:
            raise InstallError(f"{entry.id}: {exc}") from exc
        if manifest.id != entry.id or manifest.version != entry.version:
            raise InstallError(
                f"the archive holds {manifest.id} {manifest.version}, the index said {entry.id} {entry.version}"
            )
        return target, manifest

    # ---------------------------------------------------------------- plan

    def sync_skins(self) -> list[str]:
        """Put the skins of the installed, enabled skin extensions in the skins directory
        (and take away those of extensions that are gone). Returns notes; never raises."""
        if self.skins_dir is None:
            return []
        from core.extensions.skins import sync_skins

        try:
            state = self.state.load()
            installed = {
                ext_id: (
                    manifest,
                    self.install_dir(ext_id),
                    state.extensions[ext_id].enabled,
                )
                for ext_id, manifest in self._installed_manifests().items()
            }
            return sync_skins(self.skins_dir, installed)
        except Exception as exc:  # noqa: BLE001 - skins never break an install
            return [f"skins could not be synchronised: {type(exc).__name__}: {exc}"]

    def _installed_manifests(self) -> dict[str, Manifest]:
        from core.extensions.manifest import load_manifest

        found: dict[str, Manifest] = {}
        for ext_id in self.state.load().extensions:
            path = self.install_dir(ext_id) / "synth-extension.toml"
            try:
                found[ext_id] = load_manifest(path)
            except (ManifestError, OSError):
                continue
        return found

    def plan(
        self,
        extension_id: str,
        *,
        version: str | None = None,
        repo: str | None = None,
        allow_untrusted: bool = False,
    ) -> list[tuple[Candidate, Path, Manifest]]:
        """What installing ``extension_id`` takes, dependencies first, all downloaded and
        verified. Nothing is changed."""
        candidates, problems = self.available(only=repo)
        installed = self._installed_manifests()
        ordered: list[tuple[Candidate, Path, Manifest]] = []
        chosen: dict[str, Manifest] = {}

        def visit(ext_id: str, wanted: str | None, depth: int) -> None:
            if ext_id in chosen:
                return
            if depth > MAX_DEPENDENCY_DEPTH:
                raise InstallError(
                    f"dependencies of {extension_id} are nested too deeply"
                )
            candidate = self.choose(
                ext_id,
                version=wanted,
                repo=repo if ext_id == extension_id else None,
                candidates=candidates,
            )
            if not candidate.repo.trusted and not allow_untrusted:
                raise UntrustedRepository(
                    f"{ext_id} comes from repository {candidate.repo.name!r}, which is not trusted; "
                    "confirm to install from it"
                )
            path, manifest = self.download(candidate)
            chosen[ext_id] = manifest
            for need in manifest.requires.extensions:
                have = installed.get(need.id) or chosen.get(need.id)
                if have is not None and self._satisfies(have, need.version):
                    continue
                if need.optional:
                    continue
                visit(need.id, None, depth + 1)
            ordered.append((candidate, path, manifest))

        if problems and not any(c.entry.id == extension_id for c in candidates):
            raise InstallError("; ".join(problems))
        visit(extension_id, version, 0)
        # Would this break anything that works now?
        merged = dict(installed)
        for _, _, manifest in ordered:
            merged[manifest.id] = manifest
        for candidate, _, manifest in ordered:
            before = {k: v for k, v in merged.items() if k != manifest.id}
            issues = check_install(
                manifest, before, core_api_version=self.core_api_version
            )
            blocking = [i.message for i in issues if i.severity == "error"]
            if blocking:
                raise InstallError(
                    f"{manifest.id} {manifest.version} cannot be installed: {'; '.join(blocking)}"
                )
        return ordered

    @staticmethod
    def _satisfies(manifest: Manifest, constraint: str) -> bool:
        if not constraint:
            return True
        try:
            return Version(manifest.version) in SpecifierSet(constraint)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------- install

    def install(
        self,
        extension_id: str,
        *,
        version: str | None = None,
        repo: str | None = None,
        allow_untrusted: bool = False,
    ) -> InstallResult:
        plan = self.plan(
            extension_id, version=version, repo=repo, allow_untrusted=allow_untrusted
        )
        result = InstallResult()
        for candidate, path, manifest in plan:
            record = self.state.load().extensions.get(manifest.id)
            if record is not None and record.sha256 == candidate.entry.sha256:
                continue  # already this exact package
            self._place(candidate, path, manifest)
            result.installed.append(f"{manifest.id} {manifest.version}")
            if manifest.experimental or candidate.entry.experimental:
                result.warnings.append(
                    f"{manifest.id} is experimental: it may change or break"
                )
            if not candidate.signed:
                result.warnings.append(
                    f"{manifest.id}: repository {candidate.repo.name!r} is not signed"
                )
            if (
                manifest.python_requirements
                and not (self.install_dir(manifest.id) / "lib").is_dir()
            ):
                result.warnings.append(
                    f"{manifest.id} declares Python requirements but the package carries no lib/: it may not start"
                )
        return result

    def _extract(self, archive: Path, destination: Path) -> None:
        total = 0
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if len(infos) > MAX_MEMBERS:
                raise InstallError("the archive has too many members")
            base = destination.resolve()
            for info in infos:
                target = (destination / info.filename).resolve()
                if target != base and base not in target.parents:
                    raise InstallError(
                        f"member {info.filename!r} leads outside the install directory"
                    )
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += info.file_size
                if total > MAX_EXTRACTED_BYTES:
                    raise InstallError(
                        "the archive is larger than the extraction limit"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out)
                mode = (info.external_attr >> 16) & 0o777
                os.chmod(target, 0o755 if mode & stat.S_IXUSR else 0o644)
        for directory in [
            destination,
            *[p for p in destination.rglob("*") if p.is_dir()],
        ]:
            os.chmod(directory, 0o755)

    def _place(self, candidate: Candidate, archive: Path, manifest: Manifest) -> None:
        """Extract into staging and swap it in; keep what was there for rollback."""
        target = self.install_dir(manifest.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{manifest.id.replace('/', '_')}-", dir=target.parent
            )
        )
        try:
            self._extract(archive, staging)
            state = self.state.load()
            old = state.extensions.get(manifest.id)
            kept = self.previous_dir(manifest.id)
            if target.exists():
                shutil.rmtree(kept, ignore_errors=True)
                kept.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, kept)
            try:
                os.replace(staging, target)
            except OSError:
                if kept.exists() and not target.exists():
                    os.replace(kept, target)  # put the old one back
                raise
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        record = InstalledRecord(
            version=manifest.version,
            sha256=candidate.entry.sha256,
            repo=candidate.repo.name,
            installed_at=now(),
            enabled=old.enabled if old else True,
            default_install=candidate.entry.default_install,
            previous_version=old.version if old else "",
            previous_sha256=old.sha256 if old else "",
        )
        state.extensions[manifest.id] = record
        self.state.save(state)
        self.sync_skins()

    # -------------------------------------------------------------- removal

    def remove(self, extension_id: str, *, purge: bool = False) -> None:
        """Uninstall: delete the files (and the data with ``purge``). Off is not this."""
        state = self.state.load()
        record = state.extensions.get(extension_id)
        if record is None:
            raise InstallError(f"{extension_id} is not installed")
        needed_by = [
            other
            for other, manifest in self._installed_manifests().items()
            if other != extension_id
            and any(
                n.id == extension_id and not n.optional
                for n in manifest.requires.extensions
            )
        ]
        if needed_by:
            raise InstallError(
                f"{extension_id} is needed by {', '.join(sorted(needed_by))}"
            )
        shutil.rmtree(self.install_dir(extension_id), ignore_errors=True)
        shutil.rmtree(self.previous_dir(extension_id), ignore_errors=True)
        if purge:
            shutil.rmtree(self.data_dir(extension_id), ignore_errors=True)
        self.grants.revoke_all(extension_id)
        del state.extensions[extension_id]
        if record.default_install and extension_id not in state.declined:
            state.declined.append(
                extension_id
            )  # a default someone removed stays removed
        self.state.save(state)
        self.sync_skins()

    # ------------------------------------------------------ on/off, updates

    def set_enabled(self, extension_id: str, enabled: bool) -> None:
        state = self.state.load()
        record = state.extensions.get(extension_id)
        if record is None:
            raise InstallError(f"{extension_id} is not installed")
        record.enabled = enabled
        self.state.save(state)
        self.sync_skins()

    def updates(self, *, refresh: bool = False) -> list[tuple[str, str, str]]:
        """``(id, installed, available)`` for every extension with a newer version."""
        candidates, _ = self.available(refresh=refresh)
        out: list[tuple[str, str, str]] = []
        for ext_id, record in sorted(self.state.load().extensions.items()):
            have = _version(record.version)
            try:
                best = self.choose(ext_id, candidates=candidates)
            except InstallError:
                continue
            if have is not None and best.entry.parsed_version > have:
                out.append((ext_id, record.version, best.entry.version))
        return out

    def update(
        self, extension_id: str, *, allow_untrusted: bool = False
    ) -> InstallResult:
        record = self.state.load().extensions.get(extension_id)
        if record is None:
            raise InstallError(f"{extension_id} is not installed")
        return self.install(
            extension_id, repo=record.repo or None, allow_untrusted=allow_untrusted
        )

    def rollback(self, extension_id: str) -> str:
        """Go back to the version kept from before the last update. Returns its version."""
        state = self.state.load()
        record = state.extensions.get(extension_id)
        kept = self.previous_dir(extension_id)
        if record is None or not record.previous_version or not kept.is_dir():
            raise InstallError(
                f"there is no previous version of {extension_id} to go back to"
            )
        target = self.install_dir(extension_id)
        swap = target.with_name(target.name + ".swap")
        shutil.rmtree(swap, ignore_errors=True)
        os.replace(target, swap)
        try:
            os.replace(kept, target)
        except OSError:
            os.replace(swap, target)
            raise
        os.replace(swap, kept)
        record.version, record.previous_version = (
            record.previous_version,
            record.version,
        )
        record.sha256, record.previous_sha256 = record.previous_sha256, record.sha256
        self.state.save(state)
        self.sync_skins()
        return record.version

    # ---------------------------------------------------------- permissions

    def requested_capabilities(self, extension_id: str) -> tuple[str, ...]:
        manifest = self._installed_manifests().get(extension_id)
        if manifest is None:
            raise InstallError(f"{extension_id} is not installed")
        return manifest.permissions.capabilities

    def grant(
        self, extension_id: str, capabilities: list[str] | None = None
    ) -> list[str]:
        """Grant what the manifest declares (or the named subset). Never more."""
        declared = set(self.requested_capabilities(extension_id))
        wanted = declared if capabilities is None else set(capabilities)
        extra = wanted - declared
        if extra:
            raise InstallError(
                f"{extension_id} did not declare: {', '.join(sorted(extra))}"
            )
        self.grants.grant(extension_id, *sorted(wanted))
        return sorted(wanted)

    # -------------------------------------------------------------- listing

    def rows(self, *, refresh: bool = False) -> tuple[list[Row], list[str]]:
        candidates, problems = self.available(refresh=refresh)
        state = self.state.load()
        rows: dict[str, Row] = {}
        for candidate in candidates:
            entry = candidate.entry
            if self._acceptable(entry) is not None:
                continue
            row = rows.setdefault(
                entry.id,
                Row(entry.id, entry.name or entry.id, description=entry.description),
            )
            have = _version(row.available)
            if have is None or entry.parsed_version > have:
                row.available, row.repo = entry.version, candidate.repo.name
                row.experimental, row.default_install = (
                    entry.experimental,
                    entry.default_install,
                )
                row.name = entry.name or entry.id
                row.description = entry.description
                row.authors = list(entry.authors)
                row.kind = "skins" if entry.kind == "skins" else "plugin"
        installed_manifests = self._installed_manifests()
        for ext_id, record in state.extensions.items():
            row = rows.setdefault(ext_id, Row(ext_id, ext_id))
            row.installed, row.enabled = record.version, record.enabled
            manifest = installed_manifests.get(ext_id)
            if manifest is not None:
                # What is installed is read from its own manifest, even when no
                # repository offers it any more.
                row.name = row.name if row.name != ext_id else manifest.name
                row.description = row.description or manifest.description
                row.authors = row.authors or list(manifest.authors)
                row.kind = manifest.kind
            have, best = _version(record.version), _version(row.available)
            row.update = bool(have and best and best > have)
        return sorted(rows.values(), key=lambda r: r.id), problems

    # ------------------------------------------------------------- defaults

    def ensure_defaults(self) -> tuple[list[str], list[str]]:
        """Install the ``default_install`` extensions once, at the first start.

        Returns ``(installed, failed)``. One that fails does not stop the others and does
        not mark the step done, so it is tried again at the next start. An extension the
        operator uninstalled is never put back.
        """
        state = self.state.load()
        if state.defaults_done:
            return [], []
        candidates, problems = self.available()
        wanted = sorted({c.entry.id for c in candidates if c.entry.default_install})
        installed: list[str] = []
        # The official repository could not be read (no network at the first start, or no
        # release yet): that is a failure, reported, and the defaults are not done. The
        # next start tries again.
        failed: list[str] = [
            p for p in problems if p.startswith(f"repository {OFFICIAL_NAME!r}")
        ]
        trusted = {
            c.entry.id
            for c in candidates
            if c.entry.default_install and c.repo.name == OFFICIAL_NAME
        }
        for ext_id in wanted:
            if ext_id in state.declined or ext_id in state.extensions:
                continue
            try:
                self.install(ext_id, allow_untrusted=True)
                installed.append(ext_id)
                if ext_id in trusted:
                    # The official repository is SyntH's own: what it ships as a
                    # default is on and granted what it declares, so the first start
                    # works. A default another repository merely *flags* is not.
                    self.grant(ext_id)
            except InstallError as exc:
                failed.append(f"{ext_id}: {exc}")
        if not failed:
            state = self.state.load()
            state.defaults_done = True
            self.state.save(state)
        return installed, failed

    def pinned_fingerprint(self, public_key: str) -> str:
        return key_fingerprint(public_key)
