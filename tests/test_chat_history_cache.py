import asyncio
from datetime import datetime

import pytest

from core import chat_history_cache


@pytest.mark.asyncio
async def test_save_chat_message_triggers_live_context(monkeypatch):
    """Saving a non-live message should notify active live sessions."""

    # prepare dummy manager that records calls
    calls = []

    class DummyMgr:
        def get_active_sessions(self):
            return [111, 222]

        async def send_context_update(self, gid, text):
            calls.append((gid, text))

    dummy_mgr = DummyMgr()
    import core.live_session_manager as lsm_mod

    monkeypatch.setattr(
        "core.live_session_manager.LiveSessionManager.get_instance",
        lambda: dummy_mgr,
    )
    # also ensure the internal singleton reference points to our dummy
    lsm_mod.LiveSessionManager._instance = dummy_mgr

    # patch get_conn_ctx so no real database is touched
    class DummyCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def execute(self, q, params=None):
            pass

        async def fetchone(self):
            return None

    class DummyConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        def cursor(self):
            return DummyCursor()

    monkeypatch.setattr(chat_history_cache, "get_conn_ctx", lambda: DummyConn())

    # call the function under test
    result = await chat_history_cache.save_chat_message(
        interface_path="telegram_bot/123/456",
        message_text="hello world",
        sender_name="Alice",
        sender_id="alice123",
        timestamp=datetime.utcnow(),
    )

    assert result is True
    # allow the immediate task to execute
    await asyncio.sleep(0)

    assert (111, "[context update from telegram_bot/123/456] hello world") in calls
    assert (222, "[context update from telegram_bot/123/456] hello world") in calls


@pytest.mark.asyncio
async def test_save_chat_message_skips_live_path(monkeypatch):
    """Messages from discord_live_* should not trigger context updates."""
    calls = []

    class DummyMgr:
        def get_active_sessions(self):
            return [1]

        async def send_context_update(self, gid, text):
            calls.append((gid, text))

    monkeypatch.setattr(
        "core.live_session_manager.LiveSessionManager.get_instance",
        lambda: DummyMgr(),
    )

    # stub database context to avoid real DB operations
    class DummyCursor2:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def execute(self, q, params=None):
            pass

        async def fetchone(self):
            return None

    class DummyConn2:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        def cursor(self):
            return DummyCursor2()

    monkeypatch.setattr(chat_history_cache, "get_conn_ctx", lambda: DummyConn2())
    monkeypatch.setattr(
        chat_history_cache.asyncio,
        "create_task",
        lambda coro: asyncio.create_task(coro),
    )

    result = await chat_history_cache.save_chat_message(
        interface_path="discord_live_123",
        message_text="ignore me",
    )
    await asyncio.sleep(0)
    assert result is True
    assert calls == []
