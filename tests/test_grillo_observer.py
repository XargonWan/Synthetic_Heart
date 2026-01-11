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
    # snippets are now attached to context and included in the text
    assert '(chat:telegram_bot/1)' in called['text']
    assert called['ctx'].get('grillo_snippets') == ["(chat:telegram_bot/1) Hello world", "(chat:telegram_bot/2) Another message"]


@pytest.mark.asyncio
async def test_collect_recent_snippets_includes_sender_and_timestamp(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()

    async def mock_get_last_active_chats_verbose(n):
        return [(1, 'Chat A')]

    async def mock_load_chat_history(interface_path):
        from collections import deque
        return deque([
            {"text": "Hello", "sender_name": "Rekku", "timestamp": "2026-01-11T03:51:00Z"},
            {"text": "User message", "sender_name": "Jay", "timestamp": "2026-01-11T03:52:00Z"}
        ])

    import core.recent_chats as recent_chats
    monkeypatch.setattr(recent_chats, 'get_last_active_chats_verbose', mock_get_last_active_chats_verbose)
    import core.chat_history_cache as chat_history_cache
    monkeypatch.setattr(chat_history_cache, 'load_chat_history', mock_load_chat_history)

    snippets = await plugin._collect_recent_snippets(2)
    assert isinstance(snippets, list)
    assert len(snippets) >= 1
    # Ensure sender and timestamp metadata are included
    assert 'sender:' in snippets[0]
    assert '2026' in snippets[0]


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
    # check deduplication instruction is present
    assert 'check the chat snippets' in captured['text'].lower() or 'avoid producing duplicate' in captured['text'].lower() or 'do not repeat' in captured['text'].lower()
