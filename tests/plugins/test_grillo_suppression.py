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
async def test_cooldown_blocks_grillo(monkeypatch):
    # Setup
    handler = DummyHandler()
    from core.core_initializer import INTERFACE_REGISTRY

    INTERFACE_REGISTRY["telegram_bot"] = handler

    # Mock last message authored by synth 1 hour ago
    async def fake_get_last_message(path):
        return {
            "sender_id": "self",
            "sender_name": "synth",
            "text": "Previous message",
            "timestamp": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        }

    monkeypatch.setattr(
        "core.chat_history_cache.get_last_message", fake_get_last_message
    )

    # Prevent DB calls for recording suppressed events
    class DummyGrillo:
        @classmethod
        async def set_activity_response_text(
            cls, activity_log_id, response_text, append=True
        ):
            pass

        @classmethod
        async def record_suppressed_event(cls, activity_log_id=None, reason=""):
            pass

    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin", DummyGrillo)

    plugin = MessagePlugin()
    action = {
        "type": "message_telegram_bot",
        "payload": {"text": "Hello", "interface_path": "telegram_bot/-100123/2"},
    }

    await plugin._handle_message_action(
        action,
        {"grillo_beat": True, "activity_log_id": 1},
        bot=None,
        original_message=None,
    )

    assert len(handler.sent) == 0


@pytest.mark.asyncio
async def test_duplicate_similarity_blocks(monkeypatch):
    handler = DummyHandler()
    from core.core_initializer import INTERFACE_REGISTRY

    INTERFACE_REGISTRY["telegram_bot"] = handler

    # Mock recent chat history to include a similar message
    async def fake_load_chat_history(path):
        return [
            {
                "sender_name": "Alice",
                "sender_id": "100",
                "text": "Ciao Mario, come è andato il viaggio?",
                "timestamp": datetime.utcnow().isoformat(),
            }
        ]

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history", fake_load_chat_history
    )

    class DummyGrillo:
        @classmethod
        async def set_activity_response_text(
            cls, activity_log_id, response_text, append=True
        ):
            pass

        @classmethod
        async def record_suppressed_event(cls, activity_log_id=None, reason=""):
            pass

    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin", DummyGrillo)

    # Lower threshold for test
    monkeypatch.setattr(
        "core.config_manager.config_registry.get_value",
        lambda k, d, **kwargs: 0.6 if k == "GRILLO_DUP_SIMILARITY_THRESHOLD" else d,
    )

    plugin = MessagePlugin()
    action = {
        "type": "message_telegram_bot",
        "payload": {
            "text": "Ciao Mario, hai novità sul viaggio?",
            "interface_path": "telegram_bot/-100123/2",
        },
    }

    await plugin._handle_message_action(
        action,
        {"grillo_beat": True, "activity_log_id": 2},
        bot=None,
        original_message=None,
    )

    assert len(handler.sent) == 0


@pytest.mark.asyncio
async def test_observer_no_longer_suppresses_snippets_by_last_speaker(monkeypatch):
    """Snippet collection no longer drops a chat because the synth spoke last.

    That rule (a 12 h self-window) matched every conversation a responsive synth
    takes part in, so the observer had no live context at all. The human's lines
    are surfaced again; the synth's own lines are still never surfaced. Outreach
    suppression now lives in the live-conversation guard (see
    tests/test_grillo_observer.py), not in snippet collection.
    """
    obs = GrilloChatObserverPlugin()
    now = datetime.now(timezone.utc)

    async def fake_recent_paths(limit):
        return [{"interface_path": "telegram_bot/-100123/2"}]

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr(
        "core.interface_path_utils.is_vessel_interface_path", lambda p: False
    )

    async def fake_load_chat_history(path):
        return [
            {
                "sender_name": "Alice",
                "sender_id": "100",
                "text": "are you around?",
                "timestamp": (now - timedelta(hours=3)).isoformat(),
            },
            {
                "sender_name": "self",
                "text": "bot said something",
                "timestamp": (now - timedelta(hours=2)).isoformat(),
            },
        ]

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history", fake_load_chat_history
    )

    snippets = await obs._collect_recent_snippets(3)

    assert len(snippets) == 1
    assert "are you around?" in snippets[0]
    assert "bot said something" not in snippets[0]


def test_observer_prompt_avoids_duplicates():
    obs = GrilloChatObserverPlugin()
    prompt = obs._build_observer_prompt(
        ["(chat:telegram_bot/-100123/2 | sender:alice | 2026-02-10T00:00:00Z) Hello"]
    )
    assert "Do NOT propose messages that are conceptually duplicate" in prompt
