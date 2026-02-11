import pytest
from types import SimpleNamespace

# Tests for centralized wake/sleep (attention) handling in core.message_queue

import core.message_queue as mq


@pytest.mark.asyncio
async def test_awake_state_suppresses_non_directed_message(monkeypatch):
    # Simulate is_message_for_bot returning False (not directed)
    async def fake_is_message_for_bot(message, bot, **kwargs):
        return False, "not_directed"

    monkeypatch.setattr("core.message_queue.is_message_for_bot", fake_is_message_for_bot)
    # Simulate get_attention returning True (awake)
    monkeypatch.setattr("core.message_queue.get_attention", lambda scope, default=True: True)

    # Ensure queue empty
    while not mq._queue.empty():
        mq._queue.get_nowait()

    fake_message = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=SimpleNamespace(id=123, type="group"), text="hello", chat_id=123)

    await mq.enqueue(None, fake_message, interface_id="discord_bot", skip_mention_check=False)

    # Should NOT have put item into _queue because chat is awake but no explicit trigger
    assert mq._queue.empty()


@pytest.mark.asyncio
async def test_awake_state_allows_explicit_trigger(monkeypatch):
    # Simulate is_message_for_bot returning False (not directed)
    async def fake_is_message_for_bot(message, bot, **kwargs):
        return False, "not_directed"

    monkeypatch.setattr("core.message_queue.is_message_for_bot", fake_is_message_for_bot)
    # Simulate get_attention returning True (awake)
    monkeypatch.setattr("core.message_queue.get_attention", lambda scope, default=True: True)

    # Ensure queue empty
    while not mq._queue.empty():
        mq._queue.get_nowait()

    fake_message = SimpleNamespace(from_user=SimpleNamespace(id=99), chat=SimpleNamespace(id=321, type="group"), text="hello @synth", chat_id=321)
    # Mark as explicit trigger (interface should set this)
    fake_message.is_explicit_trigger = True

    await mq.enqueue(None, fake_message, interface_id="discord_bot", skip_mention_check=False)

    # Should have put item into _queue because explicit trigger overrides awake default
    assert not mq._queue.empty()
    item = await mq._queue.get()
    assert item[2]["message"] == fake_message


@pytest.mark.asyncio
async def test_awake_state_processes_directed_message(monkeypatch):
    # Simulate is_message_for_bot returning True (directed)
    async def fake_is_message_for_bot(message, bot, **kwargs):
        return True, "directed"

    monkeypatch.setattr("core.message_queue.is_message_for_bot", fake_is_message_for_bot)
    # Simulate get_attention returning True (awake)
    monkeypatch.setattr("core.message_queue.get_attention", lambda scope, default=True: True)

    # Ensure queue empty
    while not mq._queue.empty():
        mq._queue.get_nowait()

    fake_message = SimpleNamespace(from_user=SimpleNamespace(id=4), chat=SimpleNamespace(id=222, type="group"), text="this one is for the bot", chat_id=222)

    await mq.enqueue(None, fake_message, interface_id="discord_bot", skip_mention_check=False)

    # Since is_message_for_bot returned True and chat is awake, the message should be processed
    assert not mq._queue.empty()
    item = await mq._queue.get()
    assert item[2]["message"] == fake_message


@pytest.mark.asyncio
async def test_asleep_state_suppresses_non_explicit_trigger(monkeypatch):
    # Simulate is_message_for_bot returning True (directed)
    async def fake_is_message_for_bot(message, bot, **kwargs):
        return True, "directed"

    monkeypatch.setattr("core.message_queue.is_message_for_bot", fake_is_message_for_bot)
    # Simulate get_attention returning False (asleep)
    monkeypatch.setattr("core.message_queue.get_attention", lambda scope, default=True: False)

    # Ensure queue empty
    while not mq._queue.empty():
        mq._queue.get_nowait()

    fake_message = SimpleNamespace(from_user=SimpleNamespace(id=2), chat=SimpleNamespace(id=456, type="group"), text="hi", chat_id=456)

    await mq.enqueue(None, fake_message, interface_id="discord_bot", skip_mention_check=False)

    # Should be suppressed (queue remains empty)
    assert mq._queue.empty()


@pytest.mark.asyncio
async def test_asleep_state_allows_explicit_trigger(monkeypatch):
    # Simulate is_message_for_bot returning False (not directed)
    async def fake_is_message_for_bot(message, bot, **kwargs):
        return False, "not_directed"

    monkeypatch.setattr("core.message_queue.is_message_for_bot", fake_is_message_for_bot)
    # Simulate get_attention returning False (asleep)
    monkeypatch.setattr("core.message_queue.get_attention", lambda scope, default=True: False)

    # Ensure queue empty
    while not mq._queue.empty():
        mq._queue.get_nowait()

    fake_message = SimpleNamespace(from_user=SimpleNamespace(id=3), chat=SimpleNamespace(id=789, type="group"), text="hello @synth", chat_id=789)
    # Mark as explicit trigger (interface should set this)
    fake_message.is_explicit_trigger = True

    await mq.enqueue(None, fake_message, interface_id="discord_bot", skip_mention_check=False)

    # Should have put item into _queue because explicit trigger overrides sleep
    assert not mq._queue.empty()
    item = await mq._queue.get()
    assert item[2]["message"] == fake_message
