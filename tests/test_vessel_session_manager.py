"""Tests for the Rift Vessel session manager cooldown query (no real DB)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from core.vessel_session_manager import VesselSessionManager


class _FakeCursor:
    """Minimal async cursor capturing the last executed query and params."""

    def __init__(self, fetchone_row: dict[str, Any] | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._fetchone_row = fetchone_row

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))

    async def fetchall(self) -> list[dict[str, Any]]:
        return []

    async def fetchone(self) -> dict[str, Any] | None:
        return self._fetchone_row


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
    """Suspend must keep the row ``active`` (for reattach) and never compact."""
    cursor = _FakeCursor()
    conn = _FakeConnCommit(cursor)

    def _fake_conn_ctx() -> _FakeConnCommit:
        return conn

    monkeypatch.setattr("core.vessel_session_manager.get_conn_ctx", _fake_conn_ctx)

    manager = VesselSessionManager()
    # Simulate an in-memory tracked session so we can assert it is cleared.
    manager._current_session_id = "sess-1"
    manager._active_session_ids.add("sess-1")

    launched: list[Any] = []

    def _spy_launch(*args: Any, **kwargs: Any) -> None:
        launched.append((args, kwargs))

    monkeypatch.setattr(manager, "_launch_compaction", _spy_launch)

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
    # No compaction on a restart suspension.
    assert launched == []
    # In-memory bookkeeping for the destroyed connector is dropped.
    assert manager._current_session_id is None
    assert "sess-1" not in manager._active_session_ids


@pytest.mark.asyncio
async def test_end_session_schedules_compaction_not_diary(monkeypatch: Any) -> None:
    """end_session must compact into vessel_diary, never the real ai_diary."""
    row = {
        "environment": "minecraft",
        "interface_path": "vessel/minecraft",
        "status": "active",
        "experience_buffer": '[{"summary": "mined a block"}]',
    }
    cursor = _FakeCursor(fetchone_row=row)
    conn = _FakeConnCommit(cursor)

    def _fake_conn_ctx() -> _FakeConnCommit:
        return conn

    monkeypatch.setattr("core.vessel_session_manager.get_conn_ctx", _fake_conn_ctx)
    monkeypatch.setattr(
        "core.vessel_session_manager.message_queue",
        None,
        raising=False,
    )

    manager = VesselSessionManager()

    launched: list[Any] = []

    def _spy_launch(**kwargs: Any) -> None:
        launched.append(kwargs)

    monkeypatch.setattr(manager, "_launch_compaction", _spy_launch)

    result = await manager.end_session("sess-9", reason="logout")

    # No diary link is returned anymore.
    assert result is None
    # Compaction was scheduled with the buffered experience.
    assert len(launched) == 1
    assert launched[0]["session_id"] == "sess-9"
    assert launched[0]["environment"] == "minecraft"
    assert launched[0]["reason"] == "logout"
    assert launched[0]["buffer"] == [{"summary": "mined a block"}]
    # The UPDATE nulls the diary link (no ai_diary write).
    update_query = next((q for q, _ in cursor.executed if "UPDATE" in q.upper()), "")
    assert "DIARY_ENTRY_ID = NULL" in update_query.upper()
