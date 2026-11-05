"""execute_read_only_query runs inside a database-enforced read-only transaction."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from core import db


class _Cursor:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, query: str, params: Any = None) -> None:
        self.log.append(query)

    async def fetchall(self) -> list[dict[str, int]]:
        return [{"one": 1}]


class _PgConn:
    """asyncpg-style: exposes transaction(readonly=...)."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    def transaction(self, readonly: bool = False) -> Any:
        self.log.append(f"TX readonly={readonly}")
        return contextlib.AsyncExitStack()

    def cursor(self, *_a: Any) -> _Cursor:
        return _Cursor(self.log)


class _MySqlConn:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def cursor(self, *_a: Any) -> _Cursor:
        return _Cursor(self.log)


def _ctx(conn: Any) -> Any:
    @contextlib.asynccontextmanager
    async def _manager() -> Any:
        yield conn

    return _manager


@pytest.mark.asyncio
async def test_postgres_uses_readonly_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    monkeypatch.setattr(db, "get_conn_ctx", _ctx(_PgConn(log)))
    rows = await db.execute_read_only_query("SELECT 1 AS one")
    assert rows == [{"one": 1}]
    assert log == ["TX readonly=True", "SELECT 1 AS one"]


@pytest.mark.asyncio
async def test_mysql_wraps_query_in_read_only_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    monkeypatch.setattr(db, "get_conn_ctx", _ctx(_MySqlConn(log)))
    await db.execute_read_only_query("SELECT 1 AS one")
    assert log == ["START TRANSACTION READ ONLY", "SELECT 1 AS one", "ROLLBACK"]
