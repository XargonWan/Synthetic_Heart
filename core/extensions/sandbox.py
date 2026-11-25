"""How an extension process is isolated from the Core (platform milestone M2).

The process boundary is the security boundary (Python cannot enforce one inside a
process). What this module adds around it:

* a **clean environment**: nothing is inherited, so none of the Core's secrets
  (database password, API keys, tokens from ``.env``) reaches the process;
* a **dedicated numeric user** per extension when the Core runs with enough
  privilege (root, as in the container): the extension then cannot read the Core's
  files or another extension's data;
* its own **data directory**, empty ``stdin``, ``umask 077``, its own session and
  process group (so the whole tree can be stopped), and lowered resource limits.

When the Core does **not** run as root (a developer on the host) it cannot change
user. The extension still gets the clean environment and its own directory but it
runs as the same user as the Core and **can read ``.env``**. That is reported as a
warning at start-up; it is a development convenience, not a boundary.

Network egress is not restricted in this milestone.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

#: Directory of the SDK package: handed to the process by explicit path.
SDK_DIR = Path(__file__).resolve().parents[2] / "synth_sdk"

# Environment variables an extension process receives besides its own.
_BASE_PATH = "/usr/local/bin:/usr/bin:/bin"
_WINDOWS_PASSTHROUGH = ("SYSTEMROOT", "COMSPEC")


def is_root() -> bool:
    """True on POSIX when the Core can change the user of a child process."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


@dataclass(frozen=True)
class ResourceLimits:
    """Resource limits applied inside the extension process (``None``: leave)."""

    nofile: int | None = 256
    nproc: int | None = 128
    fsize: int | None = 256 * 1024 * 1024
    core: int | None = 0
    address_space: int | None = None
    cpu_seconds: int | None = None

    def to_json(self) -> str:
        values: dict[str, int] = {}
        for key, value in (
            ("nofile", self.nofile),
            ("nproc", self.nproc),
            ("fsize", self.fsize),
            ("core", self.core),
            ("as", self.address_space),
            ("cpu", self.cpu_seconds),
        ):
            if value is not None:
                values[key] = value
        return json.dumps(values, sort_keys=True)


@dataclass(frozen=True)
class SandboxPolicy:
    """How extension processes are isolated."""

    #: ``None``: change user when the Core is root. ``True``: require it (refuse
    #: to start otherwise). ``False``: never change user.
    drop_privileges: bool | None = None
    uid_base: int = 200_000
    uid_count: int = 10_000
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def will_drop_privileges(self) -> bool:
        if self.drop_privileges is None:
            return is_root()
        return self.drop_privileges


class SandboxError(RuntimeError):
    """The sandbox cannot be set up as the policy requires."""


@dataclass(frozen=True)
class RuntimeLayout:
    """Where extension code and data live.

    ``root/installed/<publisher>/<name>`` is the code (read-only for the
    extension), ``root/data/<publisher>/<name>`` is the only place it may write.
    """

    root: Path

    def install_dir(self, extension_id: str) -> Path:
        return self.root / "installed" / extension_id

    def data_dir(self, extension_id: str) -> Path:
        return self.root / "data" / extension_id

    @property
    def uid_file(self) -> Path:
        return self.root / "uids.json"


class UidAllocator:
    """Gives each extension its own numeric uid, persisted across restarts.

    Ids are allocated sequentially and never reused for another extension, so two
    extensions never share a user (a hash would collide).
    """

    def __init__(self, path: Path, base: int, count: int) -> None:
        self._path = path
        self._base = base
        self._count = count

    def _load(self) -> dict[str, int]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, int)
        }

    def allocate(self, extension_id: str) -> int:
        table = self._load()
        if extension_id in table:
            return table[extension_id]
        used = set(table.values())
        for uid in range(self._base, self._base + self._count):
            if uid not in used:
                table[extension_id] = uid
                self._save(table)
                return uid
        raise SandboxError("no free user ids left for extensions")

    def _save(self, table: dict[str, int]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".uids-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(table, handle, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def effective_limits(policy: SandboxPolicy, *, dedicated_user: bool) -> ResourceLimits:
    """The limits to apply. ``nproc`` counts **every** process of the user, so it
    is only meaningful (and only safe) when the extension has its own user."""
    if dedicated_user:
        return policy.limits
    return replace(policy.limits, nproc=None)


def build_environment(
    extension_id: str, data_dir: Path, entry: str, limits: ResourceLimits
) -> dict[str, str]:
    """The **whole** environment of an extension process. Nothing is inherited."""
    env = {
        "PATH": _BASE_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(data_dir),
        "TMPDIR": str(data_dir / "tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "SYNTH_EXTENSION_ID": extension_id,
        "SYNTH_EXTENSION_DATA": str(data_dir),
        "SYNTH_EXTENSION_ENTRY": entry,
        "SYNTH_EXT_LIMITS": limits.to_json(),
    }
    if sys.platform == "win32":  # pragma: no cover - secondary platform
        for name in _WINDOWS_PASSTHROUGH:
            if name in os.environ:
                env[name] = os.environ[name]
    return env


def _readable_by_others(path: Path, *, directory: bool) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    need = stat.S_IXOTH if directory else stat.S_IROTH
    return bool(mode & need)


def sandbox_problems(
    policy: SandboxPolicy, *, interpreter: Path, sdk_dir: Path, install_dir: Path
) -> list[str]:
    """Why the sandbox cannot start an extension as configured (empty: fine).

    When the process will run as another user it must be able to read the
    interpreter, the SDK and the extension's own code.
    """
    problems: list[str] = []
    if policy.drop_privileges and not is_root():
        problems.append(
            "the policy requires a dedicated user but the Core is not root, "
            "so it cannot change the user of a child process"
        )
        return problems
    if not policy.will_drop_privileges():
        return problems
    real_interpreter = Path(os.path.realpath(interpreter))
    for label, path in (("interpreter", real_interpreter), ("SDK", sdk_dir)):
        for parent in (*reversed(path.parents),):
            if not _readable_by_others(parent, directory=True):
                problems.append(
                    f"{label}: directory {parent} is not accessible to other users"
                )
                break
        if not _readable_by_others(path, directory=path.is_dir()):
            problems.append(f"{label}: {path} is not readable by other users")
    if not _readable_by_others(install_dir, directory=True):
        problems.append(f"extension code {install_dir} is not readable by other users")
    return problems


def secure_runtime_tree(layout: RuntimeLayout) -> list[str]:
    """Restore safe permissions on the runtime directory. Returns what it changed.

    Run before any extension is launched. Something else may have loosened the
    tree: the container start-up script does ``chmod a+rwx -R /config`` and the
    runtime root normally lives under ``/config``. Left alone, that would let an
    extension rewrite ``grants.json`` (and grant itself anything) or edit another
    extension's code.

    * the root, ``installed/`` and ``data/`` are ``0755``;
    * extension code under ``installed/`` loses its group/other write bits;
    * ``grants.json`` and ``uids.json`` are ``0600`` (only the Core reads them);
    * each extension's own data directory is made ``0700`` when it is launched.
    """
    changed: list[str] = []

    def chmod(path: Path, mode: int) -> None:
        try:
            if stat.S_IMODE(path.lstat().st_mode) != mode:
                os.chmod(path, mode)
                changed.append(f"{path} -> {mode:04o}")
        except OSError:
            return

    layout.root.mkdir(parents=True, exist_ok=True)
    for directory in (layout.root, layout.root / "installed", layout.root / "data"):
        directory.mkdir(parents=True, exist_ok=True)
        chmod(directory, 0o755)
    for name in ("grants.json", "uids.json"):
        path = layout.root / name
        if path.is_file():
            chmod(path, 0o600)
            if is_root():
                os.chown(path, 0, 0)
    for path in sorted((layout.root / "installed").rglob("*")):
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            chmod(path, mode & ~(stat.S_IWGRP | stat.S_IWOTH))
    return changed


def prepare_data_dir(data_dir: Path, uid: int | None) -> None:
    """Create the extension's private data and temporary directories (mode 0700)."""
    for path in (data_dir, data_dir / "tmp"):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        if uid is not None:
            os.chown(path, uid, uid)


def popen_privilege_kwargs(uid: int | None) -> dict[str, Any]:
    """``subprocess`` arguments that drop the child to ``uid`` (empty on ``None``)."""
    if uid is None:
        return {}
    return {"user": uid, "group": uid, "extra_groups": []}
