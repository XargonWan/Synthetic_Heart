"""Tests for core/growth_state.py — the rolling self-growth history store.

The DB connection is mocked so these run without a live database. They verify
the save→mark-current→prune sequence, the empty-content short-circuit, the
current-state read, and the revert-inserts-a-new-row behaviour.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

import core.growth_state as gs
from core.growth_state import (
    MAX_GROWTH_HISTORY,
    get_current_growth,
    revert_to_state,
    save_growth_state,
)


class _FakeCursor:
    """Records executed SQL and returns queued fetch results."""

    def __init__(self, fetchone_result: Any = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._fetchone_result = fetchone_result
        self.lastrowid = 42

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    async def fetchone(self) -> Any:
        return self._fetchone_result

    async def fetchall(self) -> Any:
        return []


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.commit = AsyncMock()

    def cursor(self, *args: Any, **kwargs: Any) -> _FakeCursor:
        return self._cursor


def _patch_conn(monkeypatch: pytest.MonkeyPatch, cursor: _FakeCursor) -> None:
    conn = _FakeConn(cursor)

    @asynccontextmanager
    async def fake_ctx():  # noqa: ANN202
        yield conn

    monkeypatch.setattr(gs, "get_conn_ctx", fake_ctx)
    monkeypatch.setattr(gs, "ensure_growth_table", AsyncMock())


def test_max_growth_history_is_ten() -> None:
    assert MAX_GROWTH_HISTORY == 10


@pytest.mark.asyncio
async def test_save_growth_state_empty_content_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Should short-circuit before touching the DB.
    ensure_mock = AsyncMock()
    monkeypatch.setattr(gs, "ensure_growth_table", ensure_mock)
    assert await save_growth_state("   ") is None
    ensure_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_growth_state_demotes_inserts_and_prunes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gs, "_get_db_type", lambda: "postgres")
    cursor = _FakeCursor(fetchone_result=(7,))
    _patch_conn(monkeypatch, cursor)

    new_id = await save_growth_state("I grew", created_by="grillo_growth")

    assert new_id == 7
    joined = " ".join(sql for sql, _ in cursor.executed).lower()
    # Demote previous current, insert new current, prune history.
    assert "set is_current = false" in joined
    assert "insert into growth_states" in joined
    assert "delete from growth_states" in joined
    # Prune uses the history cap.
    prune = [p for sql, p in cursor.executed if sql.lower().startswith("delete")]
    assert prune and prune[0] == (MAX_GROWTH_HISTORY,)


@pytest.mark.asyncio
async def test_get_current_growth_returns_stripped_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gs, "_get_db_type", lambda: "postgres")
    cursor = _FakeCursor(fetchone_result={"content": "  hello growth  "})
    _patch_conn(monkeypatch, cursor)

    assert await get_current_growth() == "hello growth"


@pytest.mark.asyncio
async def test_get_current_growth_none_when_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gs, "_get_db_type", lambda: "postgres")
    cursor = _FakeCursor(fetchone_result=None)
    _patch_conn(monkeypatch, cursor)

    assert await get_current_growth() is None


@pytest.mark.asyncio
async def test_revert_inserts_new_row_from_historical_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gs, "_get_db_type", lambda: "postgres")
    cursor = _FakeCursor(fetchone_result={"content": "past self"})
    _patch_conn(monkeypatch, cursor)

    save_mock = AsyncMock(return_value=55)
    monkeypatch.setattr(gs, "save_growth_state", save_mock)

    new_id = await revert_to_state(3)

    assert new_id == 55
    save_mock.assert_awaited_once_with("past self", created_by="user", source="revert")


@pytest.mark.asyncio
async def test_revert_missing_state_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gs, "_get_db_type", lambda: "postgres")
    cursor = _FakeCursor(fetchone_result=None)
    _patch_conn(monkeypatch, cursor)

    save_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(gs, "save_growth_state", save_mock)

    assert await revert_to_state(999) is None
    save_mock.assert_not_awaited()
