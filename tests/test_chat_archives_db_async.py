import asyncio
from datetime import datetime
import pytest

from core import chat_archives_db

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc, tb):
        return False
    async def execute(self, *args, **kwargs):
        return None
    async def fetchall(self):
        return self._rows

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
    def cursor(self, *args, **kwargs):
        return _FakeCursor(self._rows)
    def close(self):
        pass

class _FakeCtx:
    def __init__(self, conn):
        self._conn = conn
    async def __aenter__(self):
        return self._conn
    async def __aexit__(self, exc_type, exc, tb):
        return False

@pytest.mark.asyncio
async def test_list_archives_handles_tuple_row_with_missing_fields(monkeypatch):
    # Simulate a tuple row missing the message_count column (older schema)
    now = datetime.utcnow()
    rows = [ ("id1", "sess1", "Chat", now) ]

    async def fake_get_conn_ctx():
        return _FakeCtx(_FakeConn(rows))

    monkeypatch.setattr(chat_archives_db, 'get_conn_ctx', fake_get_conn_ctx)

    out = await chat_archives_db.list_archives()
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]['id'] == 'id1'
    assert out[0]['session_id'] == 'sess1'
    assert out[0]['message_count'] == 0

@pytest.mark.asyncio
async def test_list_archives_handles_dict_row(monkeypatch):
    now = datetime.utcnow()
    rows = [ {'id': 'id2', 'session_id': 'sess2', 'name':'Chat', 'created_at': now, 'message_count': 5} ]

    async def fake_get_conn_ctx():
        return _FakeCtx(_FakeConn(rows))

    monkeypatch.setattr(chat_archives_db, 'get_conn_ctx', fake_get_conn_ctx)

    out = await chat_archives_db.list_archives()
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]['id'] == 'id2'
    assert out[0]['message_count'] == 5
