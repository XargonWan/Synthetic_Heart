"""Tests for the Rift Vessel session manager cooldown query (no real DB)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from core.vessel_session_manager import VesselSessionManager


class _FakeCursor:
    """Minimal async cursor capturing the last executed query and params."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))

    async def fetchall(self) -> list[dict[str, Any]]:
        return []


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    async def __aenter__(self) -> "_FakeConn":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def cursor(self, *_args: Any, **_kwargs: Any) -> _FakeCursor:
        return self._cursor


@pytest.mark.asyncio
async def test_close_expired_sessions_uses_timestamp_cutoff(monkeypatch: Any) -> None:
    """The cooldown query must not use INTERVAL SQL and must pass a datetime."""
    cursor = _FakeCursor()

    def _fake_conn_ctx() -> _FakeConn:
        return _FakeConn(cursor)

    monkeypatch.setattr("core.vessel_session_manager.get_conn_ctx", _fake_conn_ctx)

    manager = VesselSessionManager()
    ended = await manager.close_expired_sessions(3600)

    assert ended == 0
    assert cursor.executed, "expected a SELECT to be executed"
    query, params = cursor.executed[0]
    # Backend-agnostic: no MariaDB-only INTERVAL literal.
    assert "INTERVAL" not in query.upper()
    assert "last_event_at < %s" in query
    # The single bound parameter is a computed datetime cutoff.
    assert isinstance(params, tuple)
    assert len(params) == 1
    assert isinstance(params[0], datetime)


class _FakeConnCommit(_FakeConn):
    """Fake connection that also supports ``commit`` (write path)."""

    def __init__(self, cursor: _FakeCursor) -> None:
        super().__init__(cursor)
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_suspend_session_keeps_active_and_skips_diary(monkeypatch: Any) -> None:
    """Suspend must keep the row ``active`` (for reattach) and never flush a diary."""
    cursor = _FakeCursor()
    conn = _FakeConnCommit(cursor)

    def _fake_conn_ctx() -> _FakeConnCommit:
        return conn

    monkeypatch.setattr("core.vessel_session_manager.get_conn_ctx", _fake_conn_ctx)

    manager = VesselSessionManager()
    # Simulate an in-memory tracked session so we can assert it is cleared.
    manager._current_session_id = "sess-1"
    manager._active_session_ids.add("sess-1")

    flushed: list[Any] = []

    async def _spy_flush(*args: Any, **kwargs: Any) -> None:
        flushed.append((args, kwargs))

    monkeypatch.setattr(manager, "_flush_to_diary", _spy_flush)

    await manager.suspend_session("sess-1")

    # A single UPDATE that refreshes last_event_at while preserving status.
    assert cursor.executed, "expected an UPDATE to be executed"
    query, params = cursor.executed[0]
    up = query.upper()
    assert "UPDATE VESSEL_SESSIONS" in up
    assert "LAST_EVENT_AT = CURRENT_TIMESTAMP" in up
    assert "STATUS = 'ENDED'" not in up
    assert "STATUS = 'ACTIVE'" in up  # WHERE guard keeps it reattachable
    assert params == ("sess-1",)
    assert conn.committed is True
    # No diary flush on a restart suspension.
    assert flushed == []
    # In-memory bookkeeping for the destroyed connector is dropped.
    assert manager._current_session_id is None
    assert "sess-1" not in manager._active_session_ids
