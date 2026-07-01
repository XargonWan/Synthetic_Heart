import asyncio
from datetime import UTC, datetime
from collections import deque

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
    monkeypatch.setattr(
        "core.live_session_manager.LiveSessionManager.get_instance",
        lambda: dummy_mgr,
    )

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
        timestamp=datetime.now(UTC),
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
    original_create_task = asyncio.create_task
    monkeypatch.setattr(
        chat_history_cache.asyncio,
        "create_task",
        lambda coro: original_create_task(coro),
    )

    result = await chat_history_cache.save_chat_message(
        interface_path="discord_live_123",
        message_text="ignore me",
    )
    await asyncio.sleep(0)
    assert result is True
    assert calls == []


@pytest.mark.asyncio
async def test_save_chat_message_forwards_self_reply_to_live(monkeypatch):
    """Bot text replies on non-live interfaces should still enrich live context."""

    calls = []

    class DummyMgr:
        def get_active_sessions(self):
            return [7]

        async def send_context_update(self, gid, text):
            calls.append((gid, text))

    dummy_mgr = DummyMgr()
    monkeypatch.setattr(
        "core.live_session_manager.LiveSessionManager.get_instance",
        lambda: dummy_mgr,
    )

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
    original_create_task = asyncio.create_task
    monkeypatch.setattr(
        chat_history_cache.asyncio,
        "create_task",
        lambda coro: original_create_task(coro),
    )

    result = await chat_history_cache.save_chat_message(
        interface_path="telegram_bot/123",
        message_text="bot reply",
        sender_name="self",
        sender_id="self",
        timestamp=datetime.now(UTC),
    )
    await asyncio.sleep(0.05)

    assert result is True
    assert len(calls) == 1
    assert calls[0][0] == 7
    assert "assistant reply template synced from telegram_bot/123" in calls[0][1]
    assert "primary loose template" in calls[0][1]
    assert "same opening idea" in calls[0][1]
    assert calls[0][1].endswith("bot reply")


@pytest.mark.asyncio
async def test_save_chat_message_uses_parametrized_dedup_cutoff(monkeypatch):
    executed: list[tuple[str, tuple[object, ...] | None]] = []

    class DummyCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def execute(self, q, params: tuple[object, ...] | None = None):
            executed.append((q, params))

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

    result = await chat_history_cache.save_chat_message(
        interface_path="telegram_bot/123",
        message_text="hello again",
        sender_name="Alice",
        sender_id="alice123",
        timestamp=datetime.now(UTC),
    )

    assert result is True
    dedup_query, dedup_params = executed[0]
    assert "DATE_SUB" not in dedup_query
    assert "UTC_TIMESTAMP()" not in dedup_query
    assert "timestamp > %s" in dedup_query
    assert dedup_params is not None


@pytest.mark.asyncio
async def test_save_chat_message_never_evicts_old_rows(monkeypatch):
    """chat_history_cache is a permanent log, not a rolling window -- a
    write must never delete older rows for the same interface_path. This
    used to trim each chat down to CONTEXT_VERBOSITY rows on every message,
    which silently destroyed cross-chat history (e.g. a busy group chat's
    log would get evicted to a handful of rows within seconds, leaving
    nothing for a later group<->DM context merge to draw on)."""
    executed: list[str] = []

    class DummyCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def execute(self, q, params=None):
            executed.append(q)

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

    result = await chat_history_cache.save_chat_message(
        interface_path="telegram_bot/-999",
        message_text="keep me forever",
        sender_name="Alice",
        sender_id="alice123",
        timestamp=datetime.now(UTC),
    )

    assert result is True
    assert not any("DELETE" in q.upper() for q in executed)


@pytest.mark.asyncio
async def test_load_chat_history_returns_latest_rows_in_chronological_order(
    monkeypatch,
):
    now = datetime.now(UTC)
    rows = [
        (
            "user",
            "user-1",
            f"message-{index}",
            now.replace(microsecond=index),
            "synth_webui/webui_default",
            None,
        )
        for index in range(12)
    ]
    executed: list[tuple[str, tuple[object, ...] | None]] = []

    class DummyCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def execute(self, query, params=None):
            executed.append((query, params))

        async def fetchall(self):
            # Simulate the DB returning the last 10 messages reordered ASC.
            return rows[2:]

    class DummyConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        def cursor(self):
            return DummyCursor()

    monkeypatch.setattr(chat_history_cache, "get_conn_ctx", lambda: DummyConn())
    monkeypatch.setattr(chat_history_cache, "_get_history_limit", lambda default=10: 10)

    history = await chat_history_cache.load_chat_history("synth_webui/webui_default")

    assert isinstance(history, deque)
    assert [message["text"] for message in history] == [
        f"message-{index}" for index in range(2, 12)
    ]
    query, params = executed[0]
    assert "ORDER BY timestamp DESC, id DESC" in query
    assert "ORDER BY timestamp ASC, id ASC" in query
    assert params == ("synth_webui/webui_default", 10)


@pytest.mark.asyncio
async def test_load_chat_history_uses_explicit_limit(monkeypatch):
    executed: list[tuple[str, tuple[object, ...] | None]] = []

    class DummyCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def execute(self, query, params=None):
            executed.append((query, params))

        async def fetchall(self):
            return []

    class DummyConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        def cursor(self):
            return DummyCursor()

    monkeypatch.setattr(chat_history_cache, "get_conn_ctx", lambda: DummyConn())

    history = await chat_history_cache.load_chat_history(
        "synth_webui/webui_default",
        limit=37,
    )

    assert isinstance(history, deque)
    _, params = executed[0]
    assert params == ("synth_webui/webui_default", 37)
