import json
from datetime import datetime, timezone

import pytest

import core.db as db_module
from core.webui import SynthWebUIInterface


class _FakeRequest:
    def __init__(self, query_params=None, path_params=None) -> None:
        self.query_params = query_params or {}
        self.path_params = path_params or {}


class _FakeCursor:
    def __init__(self, rows=None, rowcount=0) -> None:
        self.executed: list[tuple[str, object]] = []
        self._rows = rows or []
        self.rowcount = rowcount

    async def execute(self, query: str, params=None) -> None:
        self.executed.append((query, list(params) if params is not None else None))

    async def fetchone(self):
        return (len(self._rows),)

    async def fetchall(self):
        return self._rows

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


def _rows():
    return [
        (
            3,
            datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
            "Rekku likes jasmine tea",
            "grillo",
            "compaction",
            '["tea"]',
            "global",
        ),
        (
            4,
            datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
            "100% certain fact",
            "observer",
            "grillo_observer",
            "[]",
            "observer",
        ),
    ]


@pytest.mark.asyncio
async def test_list_memories_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(rows=_rows())
    monkeypatch.setattr(
        db_module, "get_conn_ctx", lambda: _FakeConnCtx(_FakeConn(cursor))
    )

    webui = object.__new__(SynthWebUIInterface)
    response = await SynthWebUIInterface.list_memories(webui, _FakeRequest())
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["total_count"] == 2
    assert payload["entries"][0]["id"] == 3
    assert payload["entries"][0]["content"] == "Rekku likes jasmine tea"
    assert payload["entries"][0]["source"] == "compaction"
    assert all("WHERE" not in query for query, _ in cursor.executed)


@pytest.mark.asyncio
async def test_list_memories_search_escapes_wildcards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor(rows=[])
    monkeypatch.setattr(
        db_module, "get_conn_ctx", lambda: _FakeConnCtx(_FakeConn(cursor))
    )

    webui = object.__new__(SynthWebUIInterface)
    response = await SynthWebUIInterface.list_memories(
        webui, _FakeRequest(query_params={"search": "100%"})
    )
    payload = json.loads(response.body)

    assert payload["success"] is True
    queries = [query for query, _ in cursor.executed]
    assert any("ESCAPE" in query for query in queries)
    params = [params for _, params in cursor.executed]
    assert any(
        params_set == ["%100\\%%"] for params_set in params if params_set is not None
    )


@pytest.mark.asyncio
async def test_delete_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(rowcount=1)
    monkeypatch.setattr(
        db_module, "get_conn_ctx", lambda: _FakeConnCtx(_FakeConn(cursor))
    )

    webui = object.__new__(SynthWebUIInterface)
    response = await SynthWebUIInterface.delete_memory(
        webui, _FakeRequest(path_params={"memory_id": "3"})
    )
    payload = json.loads(response.body)

    assert payload == {"success": True, "deleted_count": 1}
    assert any("DELETE FROM memories" in query for query, _ in cursor.executed)


@pytest.mark.asyncio
async def test_delete_memory_rejects_bad_id() -> None:
    webui = object.__new__(SynthWebUIInterface)
    response = await SynthWebUIInterface.delete_memory(
        webui, _FakeRequest(path_params={"memory_id": "abc"})
    )
    assert response.status_code == 400
