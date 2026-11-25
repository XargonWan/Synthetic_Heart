"""Extension storage in the Core database (platform milestone M2).

Implements :class:`core.extensions.ports.StoragePort` on top of the Core's DB
connection. The extension never sees the database: it calls ``storage.*`` on the
gateway, which calls this class with the extension id it established itself.

Design points:

* **One table, namespaced by ``extension_id``**; every statement filters on it, so
  there is no way to address another extension's rows.
* The primary key is ``(extension_id, key_hash)`` where ``key_hash`` is the
  SHA-256 of the key. That makes key comparison case-sensitive and exact on every
  backend (MariaDB's default collation is case-insensitive), and keeps the index
  small.
* **Every value is a bound parameter.** No extension-controlled text is ever
  concatenated into SQL. Prefix listing does not use ``LIKE``: it is done in
  Python on the extension's own (bounded) set of keys.
* Quotas (value size, key count, total bytes) are enforced here with the same
  limits as the in-memory reference implementation.
* Portable SQL only (``SELECT`` then ``UPDATE`` or ``INSERT``, no upsert syntax),
  so it does not depend on the Core's per-table conflict-key translation. A lock
  serialises writes within the process.

.. note::

   This class is tested against SQLite through a small connection fake
   (``tests/test_extensions_db_storage.py``). It has **not** been run against
   MariaDB or PostgreSQL yet.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Any

from core.extensions.ports import QuotaExceeded, StorageLimits

TABLE = "extension_storage"

CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    extension_id VARCHAR(100) NOT NULL,
    key_hash CHAR(64) NOT NULL,
    storage_key VARCHAR(256) NOT NULL,
    value LONGTEXT NOT NULL,
    size_bytes INT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (extension_id, key_hash)
)
"""


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class SqlStorage:
    """``StoragePort`` backed by the Core database.

    ``connection`` is a zero-argument callable returning an async context manager
    that yields a DB connection (``core.db.get_conn_ctx``).
    """

    def __init__(
        self,
        connection: Callable[[], Any] | None = None,
        limits: StorageLimits | None = None,
    ) -> None:
        if connection is None:
            from core.db import get_conn_ctx

            connection = get_conn_ctx
        self._connection = connection
        self.limits = limits or StorageLimits()
        self._lock = asyncio.Lock()
        self._ready = False

    async def _ensure_table(self, cur: Any) -> None:
        if not self._ready:
            await cur.execute(CREATE_TABLE)
            self._ready = True

    async def get(self, extension_id: str, key: str) -> str | None:
        async with self._connection() as conn, conn.cursor() as cur:
            await self._ensure_table(cur)
            await cur.execute(
                f"SELECT value FROM {TABLE} WHERE extension_id = %s AND key_hash = %s",
                (extension_id, key_hash(key)),
            )
            row = await cur.fetchone()
        return None if row is None else str(row[0])

    async def set(self, extension_id: str, key: str, value_json: str) -> None:
        size = len(value_json.encode("utf-8"))
        if size > self.limits.max_value_bytes:
            raise QuotaExceeded("value is too large")
        digest = key_hash(key)
        async with self._lock, self._connection() as conn, conn.cursor() as cur:
            await self._ensure_table(cur)
            await cur.execute(
                f"SELECT size_bytes FROM {TABLE} WHERE extension_id = %s AND key_hash = %s",
                (extension_id, digest),
            )
            existing = await cur.fetchone()
            await cur.execute(
                f"SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM {TABLE} "
                "WHERE extension_id = %s",
                (extension_id,),
            )
            count, used = await cur.fetchone()
            count, used = int(count), int(used)
            if existing is None:
                if count >= self.limits.max_keys:
                    raise QuotaExceeded("too many keys")
            else:
                used -= int(existing[0])
            if used + size > self.limits.max_total_bytes:
                raise QuotaExceeded("storage quota exceeded")
            if existing is None:
                await cur.execute(
                    f"INSERT INTO {TABLE} "
                    "(extension_id, key_hash, storage_key, value, size_bytes) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (extension_id, digest, key, value_json, size),
                )
            else:
                await cur.execute(
                    f"UPDATE {TABLE} SET storage_key = %s, value = %s, size_bytes = %s, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE extension_id = %s AND key_hash = %s",
                    (key, value_json, size, extension_id, digest),
                )

    async def delete(self, extension_id: str, key: str) -> bool:
        async with self._lock, self._connection() as conn, conn.cursor() as cur:
            await self._ensure_table(cur)
            digest = key_hash(key)
            await cur.execute(
                f"SELECT 1 FROM {TABLE} WHERE extension_id = %s AND key_hash = %s",
                (extension_id, digest),
            )
            if await cur.fetchone() is None:
                return False
            await cur.execute(
                f"DELETE FROM {TABLE} WHERE extension_id = %s AND key_hash = %s",
                (extension_id, digest),
            )
            return True

    async def keys(self, extension_id: str, prefix: str, limit: int) -> list[str]:
        """Keys starting with ``prefix``, sorted.

        The prefix is matched in Python, not with ``LIKE``: it is exact and
        case-sensitive on every backend, and an extension has at most
        ``max_keys`` rows, so reading its keys is cheap and bounded.
        """
        async with self._connection() as conn, conn.cursor() as cur:
            await self._ensure_table(cur)
            await cur.execute(
                f"SELECT storage_key FROM {TABLE} WHERE extension_id = %s",
                (extension_id,),
            )
            rows = await cur.fetchall()
        return sorted(k for (k,) in rows if str(k).startswith(prefix))[:limit]

    async def purge(self, extension_id: str) -> int:
        """Delete every key of an extension (used when it is uninstalled)."""
        async with self._lock, self._connection() as conn, conn.cursor() as cur:
            await self._ensure_table(cur)
            await cur.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE extension_id = %s", (extension_id,)
            )
            (count,) = await cur.fetchone()
            await cur.execute(
                f"DELETE FROM {TABLE} WHERE extension_id = %s", (extension_id,)
            )
            return int(count)
