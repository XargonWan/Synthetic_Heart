"""The history backfill reads the column the table has (``created_at``, not ``timestamp``)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

import core.interface_paths as interface_paths


class _Cursor:
    rowcount = 3

    def __init__(self, sqls: list[str]) -> None:
        self.sqls = sqls

    async def execute(self, sql: str, params: Any = None) -> None:
        self.sqls.append(" ".join(sql.split()))

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _Conn:
    def __init__(self, sqls: list[str]) -> None:
        self.sqls = sqls

    def cursor(self, *args: Any) -> _Cursor:
        return _Cursor(self.sqls)

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "db_type, epoch",
    [
        ("postgres", "EXTRACT(EPOCH FROM MAX(created_at))"),
        ("mysql", "UNIX_TIMESTAMP(MAX(created_at))"),
    ],
)
async def test_the_backfill_uses_created_at(
    monkeypatch, db_type: str, epoch: str
) -> None:
    import core.db as core_db

    sqls: list[str] = []

    @asynccontextmanager
    async def ctx():
        yield _Conn(sqls)

    monkeypatch.setattr(interface_paths, "get_conn_ctx", ctx)
    monkeypatch.setattr(core_db, "_get_db_type", lambda: db_type)
    assert await interface_paths.backfill_interface_paths_from_history() == 3
    assert epoch in sqls[0] and "timestamp" not in sqls[0].replace("UNIX_TIMESTAMP", "")
