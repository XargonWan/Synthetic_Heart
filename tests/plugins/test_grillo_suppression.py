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

    snippets, own_lines = await obs._collect_recent_snippets(3)

    assert len(snippets) == 1
    assert "are you around?" in snippets[0]
    assert "bot said something" not in snippets[0]


def test_observer_prompt_avoids_duplicates():
    obs = GrilloChatObserverPlugin()
    prompt = obs._build_observer_prompt(
        ["(chat:telegram_bot/-100123/2 | sender:alice | 2026-02-10T00:00:00Z) Hello"]
    )
    assert "Do NOT propose messages that are conceptually duplicate" in prompt


_OWN_LAST_LINE = (
    "*I take the bottle with both hands like it's a trophy and drink half of it "
    "in one go, which I will regret in about ninety seconds and don't care about "
    "at all right now.*"
)


def _beats_with_own_last_line(monkeypatch, *, extra_rows=None):
    """Point the delivery gates at a chat whose last synth line is known."""

    async def fake_load_chat_history(path):
        return [
            {
                "sender_name": "Scar",
                "sender_id": "5208932647",
                "text": "hands you the bottle, drink up sweetie",
                "timestamp": "2026-09-25T04:29:11+00:00",
            },
            {
                "sender_name": "self",
                "sender_id": "self",
                "text": _OWN_LAST_LINE,
                "timestamp": "2026-09-25T04:29:22+00:00",
            },
        ] + list(extra_rows or [])

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history", fake_load_chat_history
    )


@pytest.mark.asyncio
async def test_observer_repeat_of_own_last_line_is_suppressed(monkeypatch):
    """An outbound beat must never re-deliver the synth's own previous line.

    Live 2026-09-25 04:31: the observer beat re-sent, byte for byte, the reply
    the normal turn had produced two minutes earlier — into the same private
    Telegram DM. Outbound beats are exempt from the last-from-synth gate
    (reaching out is their purpose), and private chats are exempt too, so nothing
    stopped it; the repeat gate is scoped to cover exactly this combination.
    """
    handler = DummyHandler()
    from core.core_initializer import INTERFACE_REGISTRY

    INTERFACE_REGISTRY["telegram_bot"] = handler

    _beats_with_own_last_line(monkeypatch)

    reasons: list[str] = []

    class DummyGrillo:
        @classmethod
        async def set_activity_response_text(
            cls, activity_log_id, response_text, append=True
        ):
            pass

        @classmethod
        async def record_suppressed_event(cls, activity_log_id=None, reason=""):
            reasons.append(reason)

    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin", DummyGrillo)

    plugin = MessagePlugin()
    action = {
        "type": "message_synth_webui",
        "payload": {
            "text": _OWN_LAST_LINE,
            "interface_path": "telegram_bot/5208932647",
        },
    }

    await plugin._handle_message_action(
        action,
        {"grillo_beat": True, "beat_type": "observer", "activity_log_id": 2028},
        bot=None,
        original_message=None,
    )

    assert handler.sent == [], "a repeat of the synth's own last line must not be sent"
    assert any("repeat of own last line" in r for r in reasons), reasons


@pytest.mark.asyncio
async def test_observer_new_outreach_still_goes_out(monkeypatch):
    """Genuinely new outreach into the same private chat is untouched."""
    handler = DummyHandler()
    from core.core_initializer import INTERFACE_REGISTRY

    INTERFACE_REGISTRY["telegram_bot"] = handler

    _beats_with_own_last_line(monkeypatch)

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
        "type": "message_synth_webui",
        "payload": {
            "text": "Sweetheart, the sun is up and I still owe you an answer.",
            "interface_path": "telegram_bot/5208932647",
        },
    }

    await plugin._handle_message_action(
        action,
        {"grillo_beat": True, "beat_type": "observer", "activity_log_id": 2028},
        bot=None,
        original_message=None,
    )

    assert len(handler.sent) == 1
    assert handler.sent[0]["text"].startswith("Sweetheart")


@pytest.mark.asyncio
async def test_repeat_gate_ignores_the_human_words(monkeypatch):
    """A beat echoing what the HUMAN just said is not a repeat.

    The gate compares only against rows the synth itself authored, so legitimate
    outreach that picks up the other person's phrasing is never suppressed.
    """
    handler = DummyHandler()
    from core.core_initializer import INTERFACE_REGISTRY

    INTERFACE_REGISTRY["telegram_bot"] = handler

    human_line = (
        "I put the bottle down on the night stand next to your hand and tell you "
        "to drink some water before you fall asleep on me again"
    )

    _beats_with_own_last_line(
        monkeypatch,
        extra_rows=[
            {
                "sender_name": "Scar",
                "sender_id": "5208932647",
                "text": human_line,
                "timestamp": "2026-09-25T04:30:00+00:00",
            }
        ],
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

    plugin = MessagePlugin()
    action = {
        "type": "message_synth_webui",
        "payload": {
            # Echoes the human's words, says nothing the synth already said.
            "text": (
                "I put the bottle down on the night stand next to your hand and "
                "tell you to drink some water before you fall asleep on me again "
                "- and then I will, I promise."
            ),
            "interface_path": "telegram_bot/5208932647",
        },
    }

    await plugin._handle_message_action(
        action,
        {"grillo_beat": True, "beat_type": "observer", "activity_log_id": 2028},
        bot=None,
        original_message=None,
    )

    assert len(handler.sent) == 1
