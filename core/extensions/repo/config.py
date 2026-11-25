"""Which repositories are configured, and what is installed from them.

Two small JSON files under the runtime root, written atomically and readable only by the
Core's user. Neither holds a secret an extension could use: a repository token (for a
private repository) lives in ``repositories.json`` with mode 0600 and is read only by
the transport.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.extensions.manifest import EXTENSION_ID_RE

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class ConfigError(ValueError):
    """A repository or state change that is not acceptable."""


@dataclass
class Repository:
    name: str
    location: str
    kind: str = (
        "local"  # local | http | git | github (release assets; ``branch`` is the tag)
    )
    priority: int = 100  # lower wins when two repositories offer the same version
    enabled: bool = True
    #: The publisher's Ed25519 public key (base64), pinned when the repository is added.
    public_key: str = ""
    #: Refuse an index that is not signed with ``public_key``.
    require_signature: bool = True
    #: Installing from a repository that is not trusted needs an explicit confirmation.
    trusted: bool = False
    branch: str = ""
    token: str = ""


class RepoStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[Repository]:
        raw = _read_json(self.path)
        found: list[Repository] = []
        for item in raw.get("repositories", []) if isinstance(raw, dict) else []:
            if isinstance(item, dict):
                try:
                    found.append(Repository(**item))
                except TypeError:
                    continue
        return sorted(found, key=lambda r: (r.priority, r.name))

    def save(self, repos: list[Repository]) -> None:
        _write_json(self.path, {"repositories": [asdict(r) for r in repos]})

    def add(self, repo: Repository) -> None:
        if not _NAME.match(repo.name):
            raise ConfigError(
                "a repository name is lowercase letters, digits, '_', '-' or '.'"
            )
        if repo.kind not in ("local", "http", "git", "github"):
            raise ConfigError("kind must be local, http, git or github")
        repos = self.load()
        if any(r.name == repo.name for r in repos):
            raise ConfigError(f"a repository named {repo.name!r} already exists")
        repos.append(repo)
        self.save(repos)

    def remove(self, name: str) -> bool:
        repos = self.load()
        kept = [r for r in repos if r.name != name]
        if len(kept) == len(repos):
            return False
        self.save(kept)
        return True

    def get(self, name: str) -> Repository | None:
        return next((r for r in self.load() if r.name == name), None)

    def update(self, name: str, **changes: Any) -> Repository:
        repos = self.load()
        for repo in repos:
            if repo.name == name:
                for key, value in changes.items():
                    if not hasattr(repo, key):
                        raise ConfigError(f"unknown repository setting {key!r}")
                    setattr(repo, key, value)
                self.save(repos)
                return repo
        raise ConfigError(f"no repository named {name!r}")


@dataclass
class InstalledRecord:
    version: str
    sha256: str
    repo: str = ""
    installed_at: float = 0.0
    enabled: bool = True
    default_install: bool = False
    #: The version kept for rollback, if any.
    previous_version: str = ""
    previous_sha256: str = ""


#: The official extension repository, pinned: its address and its publisher's key.
OFFICIAL_NAME = "official"
OFFICIAL_REPO = "XargonWan/synthetic-heart-official-plugins"
#: The rolling release the catalog and the packages are published to: ``latest`` for the
#: ``main`` branch, ``latest-develop`` for ``develop`` (until main is released, develop).
OFFICIAL_CHANNEL = "latest-develop"
OFFICIAL_KEY = "cRgFJxeqnbfGN+CPSeXhINTQr7z7SxNCdYtzrNObXGY="


def official_repository() -> Repository:
    """The official repository as a trusted, signed Git repository."""
    return Repository(
        name=OFFICIAL_NAME,
        location=OFFICIAL_REPO,
        kind="github",
        branch=OFFICIAL_CHANNEL,
        public_key=OFFICIAL_KEY,
        require_signature=True,
        trusted=True,
    )


@dataclass
class State:
    extensions: dict[str, InstalledRecord] = field(default_factory=dict)
    #: Whether the first-start defaults were handled, and which ones were uninstalled
    #: on purpose (they are not put back).
    defaults_done: bool = False
    declined: list[str] = field(default_factory=list)
    #: Whether the settings of the old in-tree plugins were carried over
    #: (``core.extensions.legacy``); it runs once.
    legacy_done: bool = False
    #: Whether the official repository was added (once: removing it sticks).
    official_seeded: bool = False


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> State:
        raw = _read_json(self.path)
        state = State()
        if not isinstance(raw, dict):
            return state
        for ext_id, record in (raw.get("extensions") or {}).items():
            if isinstance(record, dict) and EXTENSION_ID_RE.match(str(ext_id)):
                try:
                    state.extensions[ext_id] = InstalledRecord(**record)
                except TypeError:
                    continue
        state.defaults_done = bool(raw.get("defaults_done"))
        state.legacy_done = bool(raw.get("legacy_done"))
        state.official_seeded = bool(raw.get("official_seeded"))
        state.declined = [d for d in raw.get("declined", []) if isinstance(d, str)]
        return state

    def save(self, state: State) -> None:
        _write_json(
            self.path,
            {
                "extensions": {
                    k: asdict(v) for k, v in sorted(state.extensions.items())
                },
                "defaults_done": state.defaults_done,
                "legacy_done": state.legacy_done,
                "official_seeded": state.official_seeded,
                "declined": sorted(set(state.declined)),
            },
        )

    def is_enabled(self, extension_id: str) -> bool:
        """Extensions not recorded here (a development folder) are on."""
        record = self.load().extensions.get(extension_id)
        return True if record is None else record.enabled


def now() -> float:
    return time.time()
