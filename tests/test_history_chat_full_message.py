import pytest
from types import SimpleNamespace
from datetime import datetime

from core.webui import SynthWebUIInterface


@pytest.mark.asyncio
async def test_history_chat_returns_full_messages(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)

    long_message = "LONGMSG-" + ("A" * 2000) + "-END"

    class DummyCursor:
        async def execute(self, *args, **kwargs):
            pass

        async def fetchall(self):
            # Return one row with long message and a datetime timestamp
            return [("synth_webui/1", "alice", long_message, datetime.utcnow())]

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
    assert len(o.get("messages", [])) == 1
    assert o["messages"][0]["message_text"] == long_message
