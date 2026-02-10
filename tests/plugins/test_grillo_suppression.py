import asyncio
from datetime import datetime, timedelta, timezone
import pytest

from plugins.message_plugin import MessagePlugin
from plugins.grillo.grillo_chat_observer import GrilloChatObserverPlugin


class DummyHandler:
    def __init__(self):
        self.sent = []

    async def send_message(self, payload, original_message=None):
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_cooldown_blocks_grillo(monkeypatch, caplog):
    # Setup
    handler = DummyHandler()
    from core.core_initializer import INTERFACE_REGISTRY
    INTERFACE_REGISTRY['telegram_bot'] = handler

    # Mock last message authored by synth 1 hour ago
    async def fake_get_last_message(path):
        return {'sender_id': 'self', 'sender_name': 'synth', 'text': 'Previous message', 'timestamp': (datetime.utcnow() - timedelta(hours=1)).isoformat()}

    monkeypatch.setattr('core.chat_history_cache.get_last_message', fake_get_last_message)

    # Prevent DB calls for recording suppressed events
    class DummyGrillo:
        @classmethod
        async def set_activity_response_text(cls, activity_log_id, response_text, append=True):
            pass

        @classmethod
        async def record_suppressed_event(cls, activity_log_id=None, reason=''):
            pass

    monkeypatch.setattr('plugins.grillo.grillo_impl.GrilloPlugin', DummyGrillo)

    plugin = MessagePlugin()
    action = {"type": "message_telegram_bot", "payload": {"text": "Hello", "interface_path": "telegram_bot/-100123/2"}}

    await plugin._handle_message_action(action, {'grillo_beat': True, 'activity_log_id': 1}, bot=None, original_message=None)

    assert len(handler.sent) == 0
    assert any('Skipping Grillo outbound message' in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_duplicate_similarity_blocks(monkeypatch, caplog):
    handler = DummyHandler()
    from core.core_initializer import INTERFACE_REGISTRY
    INTERFACE_REGISTRY['telegram_bot'] = handler

    # Mock recent chat history to include a similar message
    async def fake_load_chat_history(path):
        return [
            {'sender_name': 'Alice', 'sender_id': '100', 'text': 'Ciao Mario, come è andato il viaggio?', 'timestamp': datetime.utcnow().isoformat()}
        ]

    monkeypatch.setattr('core.chat_history_cache.load_chat_history', fake_load_chat_history)

    class DummyGrillo:
        @classmethod
        async def set_activity_response_text(cls, activity_log_id, response_text, append=True):
            pass

        @classmethod
        async def record_suppressed_event(cls, activity_log_id=None, reason=''):
            pass

    monkeypatch.setattr('plugins.grillo.grillo_impl.GrilloPlugin', DummyGrillo)

    # Lower threshold for test
    monkeypatch.setattr('core.config_manager.config_registry.get_value', lambda k, d, **kwargs: 0.6 if k == 'GRILLO_DUP_SIMILARITY_THRESHOLD' else d)

    plugin = MessagePlugin()
    action = {"type": "message_telegram_bot", "payload": {"text": "Ciao Mario, hai novità sul viaggio?", "interface_path": "telegram_bot/-100123/2"}}

    await plugin._handle_message_action(action, {'grillo_beat': True, 'activity_log_id': 2}, bot=None, original_message=None)

    assert len(handler.sent) == 0
    assert any('Suppressing Grillo message' in r.message or 'Suppressing Grillo message' in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_observer_respects_cooldown(monkeypatch):
    obs = GrilloChatObserverPlugin()

    # Mock recent chats listing
    async def fake_get_last_active_chats_verbose(n):
        return [(123, 'Test Chat')]

    monkeypatch.setattr('core.recent_chats.get_last_active_chats_verbose', fake_get_last_active_chats_verbose)
    monkeypatch.setattr('core.recent_chats.get_chat_path', lambda cid: 'telegram_bot/-100123/2')

    async def fake_get_last_message(path):
        return {'sender_id': 'self', 'sender_name': 'synth', 'text': 'bot said something', 'timestamp': (datetime.utcnow() - timedelta(hours=2)).isoformat()}

    monkeypatch.setattr('core.chat_history_cache.get_last_message', fake_get_last_message)

    snippets = await obs._collect_recent_snippets(3)
    # Since cooldown default is 24h and last synth message was 2h ago, we should skip and get empty
    assert snippets == []


def test_observer_prompt_avoids_duplicates():
    obs = GrilloChatObserverPlugin()
    prompt = obs._build_observer_prompt(["(chat:telegram_bot/-100123/2 | sender:alice | 2026-02-10T00:00:00Z) Hello"])
    assert "Do NOT propose messages that are conceptually duplicate" in prompt
