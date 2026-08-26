"""The per-message queue context must be a shallow copy of the shared
context-manager dict so per-turn routing flags (agent_needed,
attachment_paths) can never leak across messages/chats."""

import pytest
from types import SimpleNamespace
from collections import deque

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


def _make_fake_message(chat_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(
            id=chat_id, type="private", title=None, username=None, first_name=None
        ),
        text="hello",
        chat_id=chat_id,
    )


@pytest.fixture
def fake_queue(monkeypatch):
    class _FakePlugin:
        def get_rate_limit(self):
            return 10**9, 1, 1.0

    async def _not_blocked(user_id):
        return False

    queue = _FakeQueue()
    monkeypatch.setattr("core.message_queue.is_user_blocked", _not_blocked)
    monkeypatch.setattr("core.plugin_instance.get_plugin", lambda: _FakePlugin())
    monkeypatch.setattr("core.message_queue._get_queue", lambda: queue)
    monkeypatch.setattr("core.rate_limit.is_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr("core.message_queue.get_reaction_emoji", lambda: "")
    monkeypatch.setattr("core.message_queue.ensure_message_user_fields", lambda m: None)
    return queue


@pytest.mark.asyncio
async def test_enqueue_default_context_is_turn_local(monkeypatch, fake_queue):
    """Mutating the defaulted context must not write into the shared dict."""
    shared = {"telegram_bot/1": deque(["older message"])}
    monkeypatch.setattr(mq, "get_context_memory", lambda: shared)

    fake_message = _make_fake_message(123)
    await mq.enqueue(
        None, fake_message, interface_id="telegram", skip_mention_check=True
    )

    prio, cnt, item = fake_queue.get_nowait()
    ctx = item["context"]
    # The consumer gets a turn-local top-level dict...
    assert ctx is not shared
    # ...while the per-interface history deques stay shared (shallow copy).
    assert ctx["telegram_bot/1"] is shared["telegram_bot/1"]
    # Per-turn routing flags written by the pipeline must not leak.
    ctx["agent_needed"] = True
    ctx["attachment_paths"] = ["/tmp/x.png"]
    assert "agent_needed" not in shared
    assert "attachment_paths" not in shared
    assert shared == {"telegram_bot/1": deque(["older message"])}


@pytest.mark.asyncio
async def test_enqueue_low_priority_default_context_is_turn_local(
    monkeypatch, fake_queue
):
    """Same isolation guarantee for the low-priority enqueue wrapper."""
    shared = {"telegram_bot/1": deque(["older message"])}
    monkeypatch.setattr(mq, "get_context_memory", lambda: shared)

    fake_message = _make_fake_message(456)
    await mq.enqueue_low_priority(
        None, fake_message, interface_id="telegram", priority=mq.PRIORITY_LOW
    )

    prio, cnt, item = fake_queue.get_nowait()
    ctx = item["context"]
    assert ctx is not shared
    assert ctx["telegram_bot/1"] is shared["telegram_bot/1"]
    ctx["agent_needed"] = True
    ctx["attachment_paths"] = ["/tmp/x.png"]
    assert "agent_needed" not in shared
    assert "attachment_paths" not in shared
    assert shared == {"telegram_bot/1": deque(["older message"])}
