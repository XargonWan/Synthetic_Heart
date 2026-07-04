import json
from datetime import datetime, timezone

import pytest

import core.db as db_module
from core.webui import SynthWebUIInterface


class _FakeRequest:
    def __init__(self) -> None:
        self.query_params = {}


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, list[object] | None]] = []

    async def execute(self, query: str, params=None) -> None:
        if params is None:
            stored_params = None
        else:
            stored_params = list(params)
        self.executed.append((query, stored_params))

    async def fetchone(self):
        return (1,)

    async def fetchall(self):
        return [
            (
                7,
                "daily entry",
                "daily thought",
                datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
                "joy",
            )
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeConnCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.mark.asyncio
async def test_history_diary_skips_group_concat_session_setting_on_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor()
    conn = _FakeConn(cursor)

    monkeypatch.setattr(db_module, "_get_db_type", lambda: "postgres")
    monkeypatch.setattr(db_module, "get_conn_ctx", lambda: _FakeConnCtx(conn))

    webui = object.__new__(SynthWebUIInterface)

    response = await SynthWebUIInterface.history_diary(webui, _FakeRequest())
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["entries"][0]["id"] == 7
    assert payload["entries"][0]["content"] == "daily entry"
    assert all("group_concat_max_len" not in query for query, _ in cursor.executed)
