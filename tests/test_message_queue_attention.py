import pytest
from types import SimpleNamespace
from collections import deque

# Tests for centralized wake/sleep (attention) handling in core.message_queue

import core.message_queue as mq


class _FakeQueue:
    def __init__(self):
        self.items = deque()

    def empty(self):
        return not self.items

    def get_nowait(self):
        return self.items.popleft()

    async def get(self):
        return self.items.popleft()

    async def put(self, item):
        self.items.append(item)


@pytest.fixture(autouse=True)
def stub_queue_dependencies(monkeypatch):
    class _FakePlugin:
        def get_rate_limit(self):
            return 10**9, 1, 1.0

    async def _not_blocked(user_id):
        return False

    async def _track_chat(chat_id, meta):
        return None

    fake_queue = _FakeQueue()
    monkeypatch.setattr("core.message_queue.is_user_blocked", _not_blocked)
    monkeypatch.setattr("core.plugin_instance.get_plugin", lambda: _FakePlugin())
    monkeypatch.setattr("core.message_queue._get_queue", lambda: fake_queue)
    monkeypatch.setattr("core.rate_limit.is_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr("core.message_queue.recent_chats.track_chat", _track_chat)
    return fake_queue


def _drain_queue():
    queue = mq._get_queue()
    while not queue.empty():
        queue.get_nowait()
    return queue


@pytest.mark.asyncio
async def test_awake_state_suppresses_non_directed_message(monkeypatch):
    # Simulate is_message_for_bot returning False (not directed)
    async def fake_is_message_for_bot(message, bot, **kwargs):
        return False, "not_directed"

    monkeypatch.setattr(
        "core.message_queue.is_message_for_bot", fake_is_message_for_bot
    )
    # Simulate get_attention returning True (awake)
    monkeypatch.setattr(
        "core.chat_attention.get_attention", lambda scope, default=True: True
    )

    queue = _drain_queue()

    fake_message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(
            id=123, type="group", title=None, username=None, first_name=None
        ),
        text="hello",
        chat_id=123,
    )

    await mq.enqueue(
        None, fake_message, interface_id="discord_bot", skip_mention_check=False
    )

    # Should NOT have put item into _queue because chat is awake but no explicit trigger
    queue = mq._get_queue()
    assert queue.empty()


@pytest.mark.asyncio
async def test_awake_state_allows_explicit_trigger(monkeypatch):
    # Simulate is_message_for_bot returning False (not directed)
    async def fake_is_message_for_bot(message, bot, **kwargs):
        return False, "not_directed"

    monkeypatch.setattr(
        "core.message_queue.is_message_for_bot", fake_is_message_for_bot
    )
    # Simulate get_attention returning True (awake)
    monkeypatch.setattr(
        "core.chat_attention.get_attention", lambda scope, default=True: True
    )

    queue = _drain_queue()

    fake_message = SimpleNamespace(
        from_user=SimpleNamespace(id=99),
        chat=SimpleNamespace(
            id=321, type="group", title=None, username=None, first_name=None
        ),
        text="hello @synth",
        chat_id=321,
    )
    # Mark as explicit trigger (interface should set this)
    fake_message.is_explicit_trigger = True

    await mq.enqueue(
        None, fake_message, interface_id="discord_bot", skip_mention_check=False
    )

    # Should have put item into _queue because explicit trigger overrides awake default
    queue = mq._get_queue()
    assert not queue.empty()
    item = await queue.get()
    assert item[2]["message"] == fake_message


@pytest.mark.asyncio
async def test_awake_state_processes_directed_message(monkeypatch):
    # Simulate is_message_for_bot returning True (directed)
    async def fake_is_message_for_bot(message, bot, **kwargs):
        return True, "directed"

    monkeypatch.setattr(
        "core.message_queue.is_message_for_bot", fake_is_message_for_bot
    )
    # Simulate get_attention returning True (awake)
    monkeypatch.setattr(
        "core.chat_attention.get_attention", lambda scope, default=True: True
    )

    queue = _drain_queue()

    fake_message = SimpleNamespace(
        from_user=SimpleNamespace(id=4),
        chat=SimpleNamespace(
            id=222, type="group", title=None, username=None, first_name=None
        ),
        text="this one is for the bot",
        chat_id=222,
    )

    await mq.enqueue(
        None, fake_message, interface_id="discord_bot", skip_mention_check=False
    )

    # Since is_message_for_bot returned True and chat is awake, the message should be processed
    queue = mq._get_queue()
    assert not queue.empty()
    item = await queue.get()
    assert item[2]["message"] == fake_message


@pytest.mark.asyncio
async def test_asleep_state_suppresses_non_explicit_trigger(monkeypatch):
    # Simulate is_message_for_bot returning True (directed)
    async def fake_is_message_for_bot(message, bot, **kwargs):
        return True, "directed"

    monkeypatch.setattr(
        "core.message_queue.is_message_for_bot", fake_is_message_for_bot
    )
    # Simulate get_attention returning False (asleep)
    monkeypatch.setattr(
        "core.chat_attention.get_attention", lambda scope, default=True: False
    )

    queue = _drain_queue()

    fake_message = SimpleNamespace(
        from_user=SimpleNamespace(id=2),
        chat=SimpleNamespace(
            id=456, type="group", title=None, username=None, first_name=None
        ),
        text="hi",
        chat_id=456,
    )

    await mq.enqueue(
        None, fake_message, interface_id="discord_bot", skip_mention_check=False
    )

    # Should be suppressed (queue remains empty)
    queue = mq._get_queue()
    assert queue.empty()


@pytest.mark.asyncio
async def test_asleep_state_allows_explicit_trigger(monkeypatch):
    # Simulate is_message_for_bot returning False (not directed)
    async def fake_is_message_for_bot(message, bot, **kwargs):
        return False, "not_directed"

    monkeypatch.setattr(
        "core.message_queue.is_message_for_bot", fake_is_message_for_bot
    )
    # Simulate get_attention returning False (asleep)
    monkeypatch.setattr(
        "core.chat_attention.get_attention", lambda scope, default=True: False
    )

    queue = _drain_queue()

    fake_message = SimpleNamespace(
        from_user=SimpleNamespace(id=3),
        chat=SimpleNamespace(
            id=789, type="group", title=None, username=None, first_name=None
        ),
        text="hello @synth",
        chat_id=789,
    )
    # Mark as explicit trigger (interface should set this)
    fake_message.is_explicit_trigger = True

    await mq.enqueue(
        None, fake_message, interface_id="discord_bot", skip_mention_check=False
    )

    # Should have put item into _queue because explicit trigger overrides sleep
    queue = mq._get_queue()
    assert not queue.empty()
    item = await queue.get()
    assert item[2]["message"] == fake_message


@pytest.mark.asyncio
async def test_enqueue_defaults_history_scope_local(monkeypatch):
    """Enqueue without explicit history_scope should default to 'local' for interfaces."""
    queue = _drain_queue()

    fake_message = SimpleNamespace(
        from_user=SimpleNamespace(id=5),
        chat=SimpleNamespace(
            id=999, type="private", title=None, username=None, first_name=None
        ),
        text="hi",
        chat_id=999,
    )

    await mq.enqueue(
        None, fake_message, interface_id="telegram", skip_mention_check=True
    )

    # Item should be enqueued with history_scope defaulted to 'local'
    queue = mq._get_queue()
    assert not queue.empty()
    prio, cnt, item = await queue.get()
    assert item.get("history_scope") == "local"
