import pytest
import asyncio

from types import SimpleNamespace

import plugins.grillo.grillo_chat_observer as gco
from core import message_queue


@pytest.mark.asyncio
async def test_observer_builds_prompt_and_collects(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()

    # Mock collect_recent_snippets to return predictable data
    async def fake_collect(limit):
        return ["(chat:telegram_bot/1) Hello world", "(chat:telegram_bot/2) Another message"]

    plugin._collect_recent_snippets = fake_collect

    # Mock create_activity_log
    class FakeGrillo:
        @staticmethod
        async def create_activity_log(beat_type, prompt_text=None):
            return 12345

    monkeypatch.setattr('plugins.grillo.grillo_impl.GrilloPlugin', FakeGrillo)

    # Spy on message_queue.enqueue_low_priority
    called = {}

    async def fake_enqueue(bot, message, context_memory=None, interface_id=None, original_message=None):
        called['ctx'] = context_memory
        called['text'] = getattr(message, 'text', None)

    monkeypatch.setattr(message_queue, 'enqueue_low_priority', fake_enqueue)

    await plugin._run_observer()

    assert 'ctx' in called and called['ctx'].get('beat_type') == 'observer'
    assert '(chat:telegram_bot/1)' in called['text']


@pytest.mark.asyncio
async def test_observer_propose_only_flag_in_prompt(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()
    plugin.propose_only = True

    # minimal snippet
    async def fake_collect(limit):
        return ["test"]
    plugin._collect_recent_snippets = fake_collect

    class FakeGrillo:
        @staticmethod
        async def create_activity_log(beat_type, prompt_text=None):
            return None

    monkeypatch.setattr('plugins.grillo.grillo_impl.GrilloPlugin', FakeGrillo)

    captured = {}

    async def fake_enqueue(bot, message, context_memory=None, interface_id=None, original_message=None):
        captured['text'] = getattr(message, 'text', None)

    monkeypatch.setattr(message_queue, 'enqueue_low_priority', fake_enqueue)

    await plugin._run_observer()

    assert 'proposal-only' in captured['text'].lower() or 'proposal' in captured['text'].lower()
