"""Moving files across the extension boundary (platform milestone M3b).

An extension runs in another process, usually as another user, so a file cannot
simply be "passed": the other side may not even be able to open it. Files cross
only as **copies made by the Core**, and the Core never trusts a path it was given:

* **Inbound** (a user sent a photo, a voice note, a document): the extension saves
  the file under *its own data directory* and names it in ``interfaces.inbound``.
  The Core checks that the path really is inside that directory (no ``..``, no
  symlink that leads out, a regular file, the right owner), copies it into a
  directory the Core owns, and only then lets the message chain see the *copy*.
  The extension therefore cannot make the Core read ``.env`` or any other file by
  naming it, and its files are not needed again once the call returns.
* **Outbound** (the model attaches a file to a reply): the path comes from the
  model, so it is first checked by the Core's own sandbox rule
  (``core.outbound_file_utils.resolve_safe_outbound_path``: allowed roots, no
  credential files). The Core then copies the file into the extension's data
  directory (readable by that extension only), hands over the copy, and **deletes
  it as soon as the extension's delivery call returns**.

Limits apply to every transfer: a size per file, a number of files and a total per
message, and a quota for the inbound copies an extension may keep.

The file system calls are blocking and may copy tens of megabytes, so every public
method runs them in a worker thread. This module imports nothing from the Core: the
sandbox rule and the MIME helpers are passed in.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import secrets
import shutil
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.extensions.gateway import extension_slug
from core.extensions.sandbox import prepare_data_dir

#: ``(kind, mime)`` for a path; kind is image/video/audio/document.
Classifier = Callable[[Path], tuple[str, str]]
#: The Core's outbound sandbox rule: ``(path, None)`` or ``(None, reason)``.
OutboundResolver = Callable[[str], tuple[Path | None, str | None]]

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
_CHUNK = 1024 * 1024
_O_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class MediaError(Exception):
    """An attachment was refused. ``reason`` is safe to show to the extension."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class MediaLimits:
    max_file_bytes: int = 25 * 1024 * 1024
    max_files: int = 8
    max_total_bytes: int = 50 * 1024 * 1024
    #: What one extension may keep of inbound copies at any time.
    inbound_quota_bytes: int = 2 * 1024 * 1024 * 1024
    max_name_length: int = 100
    #: Inbound copies older than this are removed by :meth:`MediaBridge.prune`.
    inbound_ttl_seconds: float = 14 * 86400
    #: Staged outbound copies left behind (crash) are removed after this.
    outbox_ttl_seconds: float = 3600.0


def safe_name(raw: object, *, limit: int = 100, default: str = "attachment") -> str:
    """A file name that is safe to create: letters, digits, ``._-``; never a path."""
    text = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _SAFE_NAME.sub("_", text).strip("._")
    if not cleaned:
        return default
    if len(cleaned) > limit:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            cleaned = stem[: limit - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:limit]
    return cleaned


def _inside(root: Path, path: Path) -> bool:
    return root == path or root in path.parents


def open_inside(root: Path, raw: str, *, owner_uid: int | None) -> tuple[int, int]:
    """Open ``raw`` for reading **only if** it is a regular file inside ``root``.

    Returns ``(file descriptor, size)``; the caller closes the descriptor. Raises
    :class:`MediaError` otherwise. Never follows a symlink on the last component,
    resolves the path before the check, and re-checks the file that was actually
    opened (Linux: through ``/proc/self/fd``), so a path swapped for a symlink
    between the check and the open cannot lead out of ``root``.
    """
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise MediaError("invalid path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise MediaError("the path must be absolute")
    real_root = Path(os.path.realpath(root))
    real = Path(os.path.realpath(candidate))
    if not _inside(real_root, real) or real == real_root:
        raise MediaError("the file is outside the extension's data directory")
    try:
        fd = os.open(real, _O_FLAGS)
    except FileNotFoundError:
        raise MediaError("the file does not exist") from None
    except OSError:
        raise MediaError("the file cannot be opened") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise MediaError("not a regular file")
        if owner_uid is not None and info.st_uid != owner_uid:
            raise MediaError("the file is not owned by the extension")
        if sys.platform.startswith("linux"):
            try:
                actual = Path(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                actual = real
            if not _inside(real_root, Path(os.path.realpath(actual))):
                raise MediaError("the file is outside the extension's data directory")
        return fd, info.st_size
    except BaseException:
        os.close(fd)
        raise


@dataclass
class StagedMedia:
    """Outbound files copied for one delivery. Call :meth:`cleanup` when done."""

    directory: Path | None
    items: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    def cleanup(self) -> None:
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None


class MediaBridge:
    """Copies files across the extension boundary, with every check in the Core."""

    def __init__(
        self,
        *,
        data_dir: Callable[[str], Path],
        uid: Callable[[str], int | None],
        inbound_root: Path,
        resolve_outbound: OutboundResolver,
        classify: Classifier | None = None,
        limits: MediaLimits | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._data_dir = data_dir
        self._uid = uid
        self._inbound_root = inbound_root
        self._resolve_outbound = resolve_outbound
        self._classify = classify or _default_classify
        self.limits = limits or MediaLimits()
        self._clock = clock
        self._used: dict[str, int] = {}

    # ------------------------------------------------------------- inbound

    def _inbound_dir(self, extension_id: str) -> Path:
        return self._inbound_root / extension_slug(extension_id)

    def _used_bytes(self, extension_id: str) -> int:
        if extension_id not in self._used:
            total = 0
            for path in self._inbound_dir(extension_id).rglob("*"):
                try:
                    if path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
                except OSError:
                    continue
            self._used[extension_id] = total
        return self._used[extension_id]

    def _import_one(
        self, extension_id: str, item: Mapping[str, Any], budget: int
    ) -> dict[str, Any]:
        data_dir = self._data_dir(extension_id)
        fd, size = open_inside(
            data_dir, item.get("path", ""), owner_uid=self._uid(extension_id)
        )
        limits = self.limits
        try:
            if size > limits.max_file_bytes:
                raise MediaError(
                    f"the file is larger than {limits.max_file_bytes} bytes"
                )
            if size > budget:
                raise MediaError("the message's attachments are too large in total")
            if self._used_bytes(extension_id) + size > limits.inbound_quota_bytes:
                raise MediaError("the extension's attachment quota is used up")
            name = safe_name(
                item.get("filename") or Path(str(item.get("path", ""))).name,
                limit=limits.max_name_length,
            )
            day = time.strftime("%Y%m%d", time.gmtime(self._clock()))
            directory = self._inbound_dir(extension_id) / day
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            dest = (
                directory / f"{int(self._clock() * 1000)}_{secrets.token_hex(4)}_{name}"
            )
            copied = 0
            with (
                os.fdopen(fd, "rb", closefd=False) as source,
                open(dest, "xb") as target,
            ):
                os.chmod(dest, 0o600)
                while True:
                    chunk = source.read(_CHUNK)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if (
                        copied > limits.max_file_bytes
                    ):  # size on disk can change under us
                        target.close()
                        dest.unlink(missing_ok=True)
                        raise MediaError(
                            f"the file is larger than {limits.max_file_bytes} bytes"
                        )
                    target.write(chunk)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        self._used[extension_id] = self._used_bytes(extension_id) + copied
        declared = str(item.get("mime_type") or "").lower()
        mime = (
            declared
            if _MIME_RE.match(declared)
            else (mimetypes.guess_type(name)[0] or "application/octet-stream")
        )
        return {"path": str(dest), "mime_type": mime, "filename": name, "size": copied}

    def _import_all(
        self, extension_id: str, items: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        if len(items) > self.limits.max_files:
            raise MediaError(f"at most {self.limits.max_files} attachments per message")
        imported: list[dict[str, Any]] = []
        budget = self.limits.max_total_bytes
        try:
            for item in items:
                if not isinstance(item, Mapping):
                    raise MediaError("each attachment must be an object")
                result = self._import_one(extension_id, item, budget)
                budget -= result["size"]
                imported.append(result)
        except BaseException:
            for done in imported:  # all or nothing
                Path(done["path"]).unlink(missing_ok=True)
                self._used[extension_id] = max(
                    0, self._used.get(extension_id, 0) - done["size"]
                )
            raise
        return imported

    async def import_inbound(
        self, extension_id: str, items: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate and copy inbound attachments. All of them, or none.

        Returns ``{"path", "mime_type", "filename", "size"}`` per file, with ``path``
        pointing at the Core's own copy. Raises :class:`MediaError`.
        """
        return await asyncio.to_thread(self._import_all, extension_id, list(items))

    # ------------------------------------------------------------ outbound

    def _stage(
        self, extension_id: str, raw_paths: Sequence[str], trusted: bool = False
    ) -> StagedMedia:
        limits = self.limits
        staged = StagedMedia(directory=None)
        sources: list[tuple[Path, str]] = []
        for raw in list(raw_paths)[: max(limits.max_files, 0)]:
            if trusted:
                # A file the Core itself made (a synthesised voice, a call tone): it
                # is not model-chosen, so the model's sandbox rule does not apply.
                candidate = Path(str(raw))
                resolved, reason = (
                    (candidate, None)
                    if candidate.is_file() and not candidate.is_symlink()
                    else (None, "not a regular file")
                )
            else:
                resolved, reason = self._resolve_outbound(str(raw))
            if resolved is None:
                staged.rejected.append((str(raw)[:200], reason or "refused"))
                continue
            sources.append((resolved, str(raw)))
        for raw in list(raw_paths)[max(limits.max_files, 0) :]:
            staged.rejected.append((str(raw)[:200], "too many attachments"))
        if not sources:
            return staged
        data_dir = self._data_dir(extension_id)
        uid = self._uid(extension_id)
        prepare_data_dir(data_dir, uid)
        outbox = data_dir / "outbox"
        outbox.mkdir(exist_ok=True)
        directory = outbox / secrets.token_hex(8)
        directory.mkdir(mode=0o700)
        staged.directory = directory
        total = 0
        try:
            if uid is not None:
                os.chown(outbox, uid, uid)
                os.chown(directory, uid, uid)
            for index, (source, raw) in enumerate(sources):
                size = source.stat().st_size
                if size > limits.max_file_bytes:
                    staged.rejected.append((raw[:200], "file too large"))
                    continue
                if total + size > limits.max_total_bytes:
                    staged.rejected.append(
                        (raw[:200], "attachments too large in total")
                    )
                    continue
                name = safe_name(source.name, limit=limits.max_name_length)
                dest = directory / f"{index}_{name}"
                shutil.copyfile(source, dest)
                os.chmod(dest, 0o600)
                if uid is not None:
                    os.chown(dest, uid, uid)
                total += size
                kind, mime = self._classify(source)
                staged.items.append(
                    {
                        "path": str(dest),
                        "name": name,
                        "mime_type": mime,
                        "kind": kind,
                        "size": size,
                    }
                )
        except BaseException:
            staged.cleanup()
            raise
        return staged

    async def stage_outbound(
        self, extension_id: str, raw_paths: Sequence[str]
    ) -> StagedMedia:
        """Copy files the model wants to send into the extension's data directory.

        Paths the Core's sandbox refuses are returned in ``rejected`` with the
        reason; the rest are in ``items``. The caller must call
        :meth:`StagedMedia.cleanup` when the delivery has returned.
        """
        return await asyncio.to_thread(self._stage, extension_id, list(raw_paths))

    async def stage_core_files(
        self, extension_id: str, raw_paths: Sequence[str]
    ) -> StagedMedia:
        """Like :meth:`stage_outbound`, for files the **Core** produced.

        The caller passes only paths the Core made itself (never a path the model or an
        extension named), so the outbound sandbox rule is not applied; size limits
        and the private staging directory are.
        """
        return await asyncio.to_thread(self._stage, extension_id, list(raw_paths), True)

    # ---------------------------------------------------------------- upkeep

    def _prune(self, extension_ids: Sequence[str]) -> dict[str, int]:
        now = self._clock()
        removed = {"inbound": 0, "outbox": 0}
        for path in self._inbound_root.rglob("*"):
            try:
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and now - path.stat().st_mtime > self.limits.inbound_ttl_seconds
                ):
                    path.unlink()
                    removed["inbound"] += 1
            except OSError:
                continue
        for extension_id in extension_ids:
            outbox = self._data_dir(extension_id) / "outbox"
            if not outbox.is_dir():
                continue
            for child in outbox.iterdir():
                try:
                    if now - child.stat().st_mtime > self.limits.outbox_ttl_seconds:
                        shutil.rmtree(child, ignore_errors=True)
                        removed["outbox"] += 1
                except OSError:
                    continue
        self._used.clear()
        return removed

    async def prune(self, extension_ids: Sequence[str] = ()) -> dict[str, int]:
        """Remove expired inbound copies and staged files a crash left behind."""
        return await asyncio.to_thread(self._prune, list(extension_ids))


def _default_classify(path: Path) -> tuple[str, str]:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    for prefix, kind in (("image/", "image"), ("video/", "video"), ("audio/", "audio")):
        if mime.startswith(prefix):
            return kind, mime
    return "document", mime
