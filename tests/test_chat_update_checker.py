import pytest
import asyncio

from core import chat_update_checker as cuc
from core import recent_chats


@pytest.mark.asyncio
async def test_detects_new_messages(monkeypatch):
    checker = cuc.get_chat_update_checker()

    # Simulate DB responses
    async def fake_execute(query, params=()):
        if "MAX(UNIX_TIMESTAMP(timestamp))" in query or "MAX(last_active)" in query:
            return [(1000.0,)]
        elif (
            "WHERE UNIX_TIMESTAMP(timestamp) > %s" in query
            or "WHERE last_active >" in query
        ):
            return [
                ("telegram_bot/chat_a", "Jay", "u1", 950.0),
                ("telegram_bot/chat_b", "Kay", "u2", 999.0),
            ]
        return []

    monkeypatch.setattr("core.chat_update_checker.execute_query", fake_execute)

    # Set last_known to an earlier time to simulate previous check
    checker._last_known_ts = 900.0

    res = await checker.check_for_updates()
    assert isinstance(res, dict)
    assert res["updated"] is True
    assert isinstance(res["new_messages"], list)
    assert len(res["new_messages"]) == 2


@pytest.mark.asyncio
async def test_no_updates(monkeypatch):
    checker = cuc.get_chat_update_checker()

    async def fake_execute(query, params=()):
        if "MAX(UNIX_TIMESTAMP(timestamp))" in query or "MAX(last_active)" in query:
            return [(1000.0,)]
        return []

    monkeypatch.setattr("core.chat_update_checker.execute_query", fake_execute)

    checker._last_known_ts = 2000.0

    res = await checker.check_for_updates()
    assert res["updated"] is False
    assert res["new_messages"] == []


@pytest.mark.asyncio
async def test_db_unavailable_fallback(monkeypatch):
    checker = cuc.get_chat_update_checker()

    async def raise_exc(query, params=()):
        raise Exception("DB down")

    monkeypatch.setattr("core.chat_update_checker.execute_query", raise_exc)

    # start from empty state
    checker._last_count = 0

    async def fake_get_last_active_chats():
        return [1, 2]

    monkeypatch.setattr(
        recent_chats, "get_last_active_chats", fake_get_last_active_chats
    )

    res = await checker.check_for_updates()
    assert res["updated"] is True


@pytest.mark.asyncio
async def test_start_creates_task():
    checker = cuc.get_chat_update_checker()
    checker.start()
    # start() should create an asyncio.Task in running loop
    assert checker._task is not None
    assert isinstance(checker._task, asyncio.Task)
    # stop it to avoid background running tasks during tests
    checker.stop()


@pytest.mark.asyncio
async def test_peek_does_not_consume(monkeypatch):
    """A non-consuming peek (consume=False) must not modify _last_known_ts but
    must still report updates when max_ts is newer than the current last_known."""
    checker = cuc.get_chat_update_checker()

    async def fake_execute(query, params=()):
        # MAX query
        if "MAX(UNIX_TIMESTAMP(timestamp))" in query or "MAX(last_active)" in query:
            return [(1000.0,)]
        # rows since last_known
        if "WHERE UNIX_TIMESTAMP(timestamp) > %s" in query:
            return [("telegram_bot/-1", "Jay", "31321637", 950.0)]
        return []

    monkeypatch.setattr("core.chat_update_checker.execute_query", fake_execute)

    # Initialize last_known_ts to earlier time
    checker._last_known_ts = 900.0

    res = await checker.check_for_updates(consume=False)
    assert res["updated"] is True
    # Ensure internal last_known_ts was NOT updated by peek
    assert checker._last_known_ts == 900.0
