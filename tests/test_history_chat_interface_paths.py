import pytest
from types import SimpleNamespace
from datetime import datetime

from core.webui import SynthWebUIInterface


@pytest.mark.asyncio
async def test_history_chat_returns_interface_paths(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)

    # Prepare dummy DB cursor that returns different results depending on query
    class DummyCursor:
        def __init__(self):
            self._last_query = ""

        async def execute(self, query, *args, **kwargs):
            self._last_query = query

        async def fetchall(self):
            # If the query requests distinct interface_path, return single-column rows
            if "SELECT DISTINCT interface_path" in self._last_query:
                return [("synth_webui/1",), ("telegram_bot/123",)]
            # Else it's the messages query
            return [("synth_webui/1", "alice", "Hello there", datetime.utcnow())]

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
        # Return an object usable with `async with` directly
        return DummyConn()

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx)

    fake_request = SimpleNamespace(
        query_params={
            "page": "1",
            "per_page": "10",
            "interface_path": "",
            "search": "",
            "sort": "desc",
        }
    )

    resp = await webui.history_chat(fake_request)
    assert resp.status_code == 200

    body = resp.body.decode("utf-8") if hasattr(resp, "body") else None
    assert body is not None

    import json as _json

    o = _json.loads(body)

    assert o.get("success") is True
    assert "interface_paths" in o
    assert isinstance(o["interface_paths"], list)
    assert "synth_webui/1" in o["interface_paths"]
    assert "telegram_bot/123" in o["interface_paths"]
