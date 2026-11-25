"""Which capabilities the user has granted to which extension (milestone M2).

An extension *declares* the capabilities it wants in its manifest. A declaration
grants nothing: the gateway lets a call through only when the capability is
declared **and** granted, and it asks the store on every call, so revoking a
grant takes effect immediately.

The user interface for granting (the extension store, milestone M6) is not built
yet. Until then grants come from a store the host fills in: :class:`MemoryGrantStore`
in tests, :class:`FileGrantStore` for a persisted JSON file.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol


class GrantStore(Protocol):
    def granted(self, extension_id: str) -> frozenset[str]:
        """Capabilities currently granted to ``extension_id`` (empty if none)."""
        ...


class MemoryGrantStore:
    def __init__(self, grants: dict[str, Iterable[str]] | None = None) -> None:
        self._grants: dict[str, set[str]] = {
            k: set(v) for k, v in (grants or {}).items()
        }

    def granted(self, extension_id: str) -> frozenset[str]:
        return frozenset(self._grants.get(extension_id, ()))

    def grant(self, extension_id: str, *capabilities: str) -> None:
        self._grants.setdefault(extension_id, set()).update(capabilities)

    def revoke(self, extension_id: str, *capabilities: str) -> None:
        if capabilities:
            self._grants.get(extension_id, set()).difference_update(capabilities)
        else:
            self._grants.pop(extension_id, None)


class FileGrantStore:
    """Grants persisted as JSON ``{"publisher/name": ["capability", ...]}``.

    The file is re-read when it changes, so an edit (or a revoke from another
    process) is seen on the next call. A missing, unreadable or malformed file
    means **no grants**: failing closed.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cache: dict[str, frozenset[str]] = {}
        self._stamp: tuple[int, int] | None = None

    def _load(self) -> None:
        try:
            stat = self._path.stat()
        except OSError:
            self._cache, self._stamp = {}, None
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._cache, self._stamp = {}, stamp
            return
        cache: dict[str, frozenset[str]] = {}
        if isinstance(raw, dict):
            for ext_id, caps in raw.items():
                if isinstance(ext_id, str) and isinstance(caps, list):
                    cache[ext_id] = frozenset(c for c in caps if isinstance(c, str))
        self._cache, self._stamp = cache, stamp

    def granted(self, extension_id: str) -> frozenset[str]:
        self._load()
        return self._cache.get(extension_id, frozenset())

    def grant(self, extension_id: str, *capabilities: str) -> None:
        """Add capabilities and persist."""
        self._load()
        table = {k: set(v) for k, v in self._cache.items()}
        table.setdefault(extension_id, set()).update(capabilities)
        self.save(table)

    def revoke_all(self, extension_id: str) -> None:
        """Forget everything granted to ``extension_id`` and persist."""
        self._load()
        table = {k: set(v) for k, v in self._cache.items() if k != extension_id}
        self.save(table)

    def save(self, grants: Mapping[str, Iterable[str]]) -> None:
        """Atomically replace the file with ``grants`` (mode 0600)."""
        data = {k: sorted(set(v)) for k, v in sorted(grants.items())}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".grants-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
