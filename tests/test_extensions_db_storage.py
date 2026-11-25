"""Storage conformance tests: every ``StoragePort`` must behave the same (M2).

The in-memory port is the reference. ``SqlStorage`` (the Core-database adapter)
is run through the same tests using SQLite behind a small fake of the DB
connection the Core hands out. **This does not exercise MariaDB or PostgreSQL.**
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from core.extensions.db_storage import TABLE, SqlStorage, key_hash
from core.extensions.memory_ports import MemoryStorage
from core.extensions.ports import QuotaExceeded, StorageLimits, StoragePort

LIMITS = StorageLimits(max_keys=5, max_total_bytes=200, max_value_bytes=100)


class FakeCursor:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._cursor: sqlite3.Cursor | None = None
        self.statements: list[str] = []

    async def __aenter__(self) -> FakeCursor:
        self._cursor = self._db.cursor()
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._cursor is not None
        self._cursor.close()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        assert self._cursor is not None
        self.statements.append(sql)
        self._cursor.execute(sql.replace("%s", "?"), params)

    async def fetchone(self) -> Any:
        assert self._cursor is not None
        return self._cursor.fetchone()

    async def fetchall(self) -> list[Any]:
        assert self._cursor is not None
        return self._cursor.fetchall()


class FakeConnection:
    def __init__(self, db: sqlite3.Connection, log: list[str]) -> None:
        self._db = db
        self._log = log

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self._db)
        self._log_cursor = cursor
        return cursor


@pytest.fixture
def sqlite_db() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        yield db
    finally:
        db.close()


def make_sql_storage(db: sqlite3.Connection, log: list[str]) -> SqlStorage:
    @asynccontextmanager
    async def connection() -> AsyncIterator[FakeConnection]:
        conn = FakeConnection(db, log)
        yield conn

    return SqlStorage(connection, LIMITS)


@pytest.fixture(params=["memory", "sql"])
def storage(
    request: pytest.FixtureRequest, sqlite_db: sqlite3.Connection
) -> StoragePort:
    if request.param == "memory":
        return MemoryStorage(LIMITS)
    return make_sql_storage(sqlite_db, [])


@pytest.fixture
def sql_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every SQL statement the fake cursor is asked to run."""
    seen: list[str] = []
    original = FakeCursor.execute

    async def spy(self: FakeCursor, sql: str, params: tuple[Any, ...] = ()) -> None:
        seen.append(sql)
        await original(self, sql, params)

    monkeypatch.setattr(FakeCursor, "execute", spy)
    return seen


class TestConformance:
    @pytest.mark.asyncio
    async def test_missing_key(self, storage: StoragePort) -> None:
        assert await storage.get("dev/a", "nope") is None
        assert await storage.delete("dev/a", "nope") is False

    @pytest.mark.asyncio
    async def test_set_get_overwrite_delete(self, storage: StoragePort) -> None:
        await storage.set("dev/a", "k", json.dumps({"n": 1}))
        assert json.loads(await storage.get("dev/a", "k") or "") == {"n": 1}
        await storage.set("dev/a", "k", json.dumps({"n": 2}))
        assert json.loads(await storage.get("dev/a", "k") or "") == {"n": 2}
        assert await storage.delete("dev/a", "k") is True
        assert await storage.get("dev/a", "k") is None

    @pytest.mark.asyncio
    async def test_extensions_are_isolated(self, storage: StoragePort) -> None:
        await storage.set("dev/a", "k", '"A"')
        assert await storage.get("dev/b", "k") is None
        await storage.set("dev/b", "k", '"B"')
        assert await storage.get("dev/a", "k") == '"A"'
        assert await storage.keys("dev/b", "", 10) == ["k"]
        assert await storage.delete("dev/b", "k") is True
        assert await storage.get("dev/a", "k") == '"A"'

    @pytest.mark.asyncio
    async def test_keys_are_case_sensitive(self, storage: StoragePort) -> None:
        await storage.set("dev/a", "Key", '"upper"')
        await storage.set("dev/a", "key", '"lower"')
        assert await storage.get("dev/a", "Key") == '"upper"'
        assert await storage.get("dev/a", "key") == '"lower"'
        assert await storage.keys("dev/a", "K", 10) == ["Key"]

    @pytest.mark.asyncio
    async def test_prefix_listing_treats_wildcards_literally(
        self, storage: StoragePort
    ) -> None:
        for key in ("a_b", "axb", "a%b", "a1b", "b_b"):
            await storage.set("dev/a", key, "1")
        assert await storage.keys("dev/a", "a_", 10) == ["a_b"]
        assert await storage.keys("dev/a", "a%", 10) == ["a%b"]
        assert await storage.keys("dev/a", "", 10) == [
            "a%b",
            "a1b",
            "a_b",
            "axb",
            "b_b",
        ]
        assert await storage.keys("dev/a", "a", 2) == ["a%b", "a1b"]

    @pytest.mark.asyncio
    async def test_value_size_limit(self, storage: StoragePort) -> None:
        await storage.set("dev/a", "ok", '"' + "x" * 90 + '"')
        with pytest.raises(QuotaExceeded):
            await storage.set("dev/a", "big", '"' + "x" * 200 + '"')

    @pytest.mark.asyncio
    async def test_key_count_limit(self, storage: StoragePort) -> None:
        for i in range(5):
            await storage.set("dev/a", f"k{i}", "1")
        with pytest.raises(QuotaExceeded):
            await storage.set("dev/a", "k5", "1")
        await storage.set("dev/a", "k0", "2")  # replacing is not a new key
        await storage.set("dev/b", "k0", "1")  # another extension has its own count

    @pytest.mark.asyncio
    async def test_total_size_limit_counts_only_the_difference_on_replace(
        self, storage: StoragePort
    ) -> None:
        await storage.set("dev/a", "a", "x" * 90)
        await storage.set("dev/a", "b", "x" * 90)
        with pytest.raises(QuotaExceeded):
            await storage.set("dev/a", "c", "x" * 90)
        await storage.set("dev/a", "a", "x" * 100)  # grows by 10: still within 200
        with pytest.raises(QuotaExceeded):
            await storage.set("dev/a", "a", "x" * 100 + "y")  # value limit
        await storage.delete("dev/a", "b")
        await storage.set("dev/a", "c", "x" * 90)

    @pytest.mark.asyncio
    async def test_unicode_round_trip_and_byte_accounting(
        self, storage: StoragePort
    ) -> None:
        text = json.dumps("é" * 40, ensure_ascii=False)  # 2 bytes per character
        await storage.set("dev/a", "u", text)
        assert await storage.get("dev/a", "u") == text
        with pytest.raises(QuotaExceeded):
            await storage.set("dev/a", "v", json.dumps("é" * 60, ensure_ascii=False))


class TestSqlSpecifics:
    @pytest.mark.asyncio
    async def test_hostile_text_is_data_not_sql(
        self, sqlite_db: sqlite3.Connection
    ) -> None:
        storage = make_sql_storage(sqlite_db, [])
        evil_id = "x'; DROP TABLE extension_storage; --"
        evil_key = "k'); DELETE FROM extension_storage; --"
        await storage.set(evil_id, evil_key, '"payload"')
        await storage.set("dev/safe", "k", '"safe"')
        assert await storage.get(evil_id, evil_key) == '"payload"'
        assert await storage.get("dev/safe", "k") == '"safe"'
        count = sqlite_db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        assert count == 2

    @pytest.mark.asyncio
    async def test_no_extension_text_is_ever_in_the_sql_string(
        self, sqlite_db: sqlite3.Connection, sql_log: list[str]
    ) -> None:
        storage = make_sql_storage(sqlite_db, [])
        await storage.set("dev/needle", "needle-key", '"needle-value"')
        await storage.get("dev/needle", "needle-key")
        await storage.keys("dev/needle", "needle", 5)
        await storage.delete("dev/needle", "needle-key")
        assert sql_log
        assert not any("needle" in sql for sql in sql_log)
        for sql in sql_log:
            scoped = "extension_id = %s" in sql or "(extension_id," in sql
            assert scoped or "CREATE TABLE" in sql, sql  # every statement is namespaced

    @pytest.mark.asyncio
    async def test_the_table_is_created_once_and_lazily(
        self, sqlite_db: sqlite3.Connection, sql_log: list[str]
    ) -> None:
        storage = make_sql_storage(sqlite_db, [])
        assert sql_log == []  # constructing does nothing
        await storage.set("dev/a", "k", "1")
        await storage.get("dev/a", "k")
        assert sum("CREATE TABLE" in s for s in sql_log) == 1

    @pytest.mark.asyncio
    async def test_purge_removes_only_that_extension(
        self, sqlite_db: sqlite3.Connection
    ) -> None:
        storage = make_sql_storage(sqlite_db, [])
        for i in range(3):
            await storage.set("dev/gone", f"k{i}", "1")
        await storage.set("dev/stays", "k", "1")
        assert await storage.purge("dev/gone") == 3
        assert await storage.keys("dev/gone", "", 10) == []
        assert await storage.get("dev/stays", "k") == "1"
        assert await storage.purge("dev/gone") == 0

    def test_key_hash_is_stable_and_case_sensitive(self) -> None:
        assert key_hash("a") == key_hash("a")
        assert key_hash("a") != key_hash("A")
        assert len(key_hash("a")) == 64
