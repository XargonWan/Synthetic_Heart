import asyncio
from types import SimpleNamespace

import pytest

from plugins.memory_search import MemorySearchPlugin


class DummyCursor:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def execute(self, q, params):
        # ignore
        pass

    async def fetchall(self):
        return self.rows


class DummyConn:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    def cursor(self):
        return DummyCursor(self.rows)


@pytest.mark.asyncio
async def test_live_search_returns_quickly_and_sends(monkeypatch):
    # prepare fake DB results
    now = "2026-01-01T00:00:00"
    rows = [("memories", 1, now, "remembered content")]

    # patch both the core.db helper and the copy imported by the plugin
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: DummyConn(rows))
    import plugins.memory_search as _ms

    monkeypatch.setattr(_ms, "get_conn_ctx", lambda: DummyConn(rows))

    sent = []

    class FakeMgr:
        def is_session_active(self, gid):
            return True

        async def send_text(self, gid, text):
            sent.append((gid, text))

    monkeypatch.setattr(
        "core.live_session_manager.LiveSessionManager.get_instance",
        lambda: FakeMgr(),
    )

    plugin = MemorySearchPlugin()
    action = {"payload": {"mode": "tags", "tags": ["foo"]}}
    ctx = {"interface": "discord"}
    orig_msg = SimpleNamespace(interface_path="discord_live_123", chat_id=None)

    res = await plugin.execute_action(action, ctx, None, orig_msg)
    assert res.get("processed") is True
    assert res.get("results") == []
    assert res.get("async") is True

    # give background task a tick to run
    await asyncio.sleep(0)
    assert sent == [(123, "[2026-01-01T00:00:00] remembered content")]
