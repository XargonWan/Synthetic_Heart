import pytest
from types import SimpleNamespace
from datetime import datetime

from core.webui import SynthWebUIInterface


@pytest.mark.asyncio
async def test_history_grillo_returns_beat_types(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)

    class DummyCursor:
        def __init__(self):
            self._last_query = ''

        async def execute(self, query, *args, **kwargs):
            self._last_query = query

        async def fetchall(self):
            if 'SELECT DISTINCT beat_type' in self._last_query:
                return [("dream",), ("curiosity",)]
            # grillo rows: id, beat_type, prompt_text, response_text, diary_entry_id, executed_at, diary_content
            return [(1, "dream", "prompt", "response", None, datetime.utcnow(), None)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return DummyCursor()

    def mock_get_conn_ctx():
        return DummyConn()

    import core.db as cdb
    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx)

    fake_request = SimpleNamespace(query_params={
        'page': '1',
        'per_page': '10',
        'search': '',
        'beat_type': '',
        'sort': 'desc'
    })

    resp = await webui.history_grillo(fake_request)
    assert resp.status_code == 200

    body = resp.body.decode('utf-8') if hasattr(resp, 'body') else None
    assert body is not None

    import json as _json
    o = _json.loads(body)

    assert o.get('success') is True
    assert isinstance(o.get('beat_types'), list)
    assert 'dream' in o['beat_types']
    assert 'curiosity' in o['beat_types']
