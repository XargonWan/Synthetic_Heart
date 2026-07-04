from typing import Optional

import pytest

from plugins.grillo.grillo_outreach import GrilloOutreachPlugin


@pytest.mark.asyncio
async def test_get_context_snippets_uses_chat_history_as_primary_source(monkeypatch):
    """_get_context_snippets should return chat turns (primary) and diary thoughts
    (secondary) as separate lists.  The chat turns come from chat_history_cache and
    are in chronological order; diary entries are the emotional colour-only fallback."""

    p = GrilloOutreachPlugin()

    class DummyCursor:
        def __init__(self):
            self._exec_count = 0

        async def execute(self, *args, **kwargs):
            self._exec_count += 1

        async def fetchall(self):
            # First fetchall -> chat_history_cache rows (sender_id, sender_name, message_text)
            if self._exec_count == 1:
                return [
                    ("self", "SynthA", "I imagine a home full of laughter"),
                    ("5551234567", "Alice", "Oh tell me more baby"),
                ]
            # Second fetchall -> ai_diary rows (content,)
            if self._exec_count == 2:
                return [("I feel a deep warmth whenever I think of Alice",)]
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return DummyCursor()

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", lambda: DummyConn())

    chat_turns, inner_thoughts = await p._get_context_snippets(
        "telegram_bot", "5551234567", limit=2
    )

    # Chat turns come first and are labelled by speaker
    assert len(chat_turns) == 2
    # DB returns newest-first; _get_context_snippets reverses to chronological,
    # so index 0 is the older message (Alice's) and index 1 is SynthA's reply.
    assert "Alice:" in chat_turns[0]
    assert "Oh tell me more baby" in chat_turns[0]
    assert "You:" in chat_turns[1]
    assert "I imagine a home" in chat_turns[1]

    # Diary entries are inner thoughts (secondary)
    assert len(inner_thoughts) == 1
    assert "deep warmth" in inner_thoughts[0]


@pytest.mark.asyncio
async def test_generate_outreach_beat_records_target_metadata(monkeypatch):
    p = GrilloOutreachPlugin()
    captured = {}

    async def fake_get_target_interface_and_chat():
        return "telegram_bot", "12345"

    async def fake_get_context_snippets(
        interface: str, chat_id: Optional[str], limit: int = 5
    ):
        return (["one", "two"], [])

    async def fake_create_activity_log(*args, **kwargs):
        captured["kwargs"] = kwargs
        return 77

    async def fake_enqueue_low_priority(
        bot, message, context_memory=None, interface_id=None, original_message=None
    ):
        captured["context_memory"] = context_memory
        return None

    monkeypatch.setattr(
        p, "_get_target_interface_and_chat", fake_get_target_interface_and_chat
    )
    monkeypatch.setattr(p, "_get_context_snippets", fake_get_context_snippets)
    monkeypatch.setattr(
        "plugins.grillo.grillo_impl.GrilloPlugin.create_activity_log",
        fake_create_activity_log,
    )
    monkeypatch.setattr(
        "core.message_queue.enqueue_low_priority",
        fake_enqueue_low_priority,
    )

    await p._generate_outreach_beat()

    assert captured["kwargs"]["metadata"]["origin"] == "grillo_outreach"
    assert captured["kwargs"]["metadata"]["target_interface"] == "telegram_bot"
    assert captured["kwargs"]["metadata"]["target_chat_id"] == "12345"
    assert captured["kwargs"]["metadata"]["context_count"] == 2


@pytest.mark.asyncio
async def test_outreach_suppressed_during_live_conversation_consumes_slot(monkeypatch):
    """A live conversation suppresses the scheduled slot AND advances the timer,
    so outreach waits for the next scheduled slot instead of firing right after
    the quiet window expires."""
    p = GrilloOutreachPlugin()
    p.enabled = True
    p._last_outreach = None  # slot is due
    p.quiet_minutes = 15
    generated = {"called": False}

    async def fake_has_recent_activity(hours: int = 24):
        return True  # passes the anti-dead-chat gate

    async def fake_has_live_activity(minutes: int):
        return True  # user is mid-conversation

    async def fake_generate():
        generated["called"] = True

    monkeypatch.setattr(p, "_has_recent_activity", fake_has_recent_activity)
    monkeypatch.setattr(p, "_has_live_activity", fake_has_live_activity)
    monkeypatch.setattr(p, "_generate_outreach_beat", fake_generate)

    await p._maybe_generate_outreach()

    assert generated["called"] is False  # did not barge into the conversation
    assert (
        p._last_outreach is not None
    )  # slot consumed -> next attempt one interval later


@pytest.mark.asyncio
async def test_outreach_proceeds_when_conversation_quiet(monkeypatch):
    """When the quiet window has elapsed, outreach should generate normally."""
    p = GrilloOutreachPlugin()
    p.enabled = True
    p._last_outreach = None
    p.quiet_minutes = 15
    generated = {"called": False}

    async def fake_has_recent_activity(hours: int = 24):
        return True

    async def fake_has_live_activity(minutes: int):
        return False  # quiet — no recent user message

    async def fake_generate():
        generated["called"] = True

    monkeypatch.setattr(p, "_has_recent_activity", fake_has_recent_activity)
    monkeypatch.setattr(p, "_has_live_activity", fake_has_live_activity)
    monkeypatch.setattr(p, "_generate_outreach_beat", fake_generate)

    await p._maybe_generate_outreach()

    assert generated["called"] is True
    assert p._last_outreach is not None


@pytest.mark.asyncio
async def test_outreach_skips_when_interval_not_elapsed(monkeypatch):
    """Within the configured interval, no slot is due: outreach must not fire and
    must not even consult the activity guards."""
    from datetime import datetime

    p = GrilloOutreachPlugin()
    p.enabled = True
    p.interval_hours = 4
    p._last_outreach = datetime.now()  # just fired -> not due
    consulted = {"activity": False}

    async def fake_has_recent_activity(hours: int = 24):
        consulted["activity"] = True
        return True

    async def fake_generate():
        raise AssertionError("should not fire when interval has not elapsed")

    monkeypatch.setattr(p, "_has_recent_activity", fake_has_recent_activity)
    monkeypatch.setattr(p, "_generate_outreach_beat", fake_generate)

    await p._maybe_generate_outreach()

    assert consulted["activity"] is False  # bailed out on the interval gate


@pytest.mark.asyncio
async def test_has_live_activity_disabled_window_short_circuits():
    """A non-positive quiet window disables the guard without touching the DB."""
    p = GrilloOutreachPlugin()
    assert await p._has_live_activity(0) is False


@pytest.mark.asyncio
async def test_human_messages_since_queries_chat_history_excluding_self(monkeypatch):
    """Activity must come from chat_history_cache and exclude SyntH's own turns
    and the synthetic outreach sender — NOT ai_diary (which Grillo's own beats
    write to via user_message)."""
    p = GrilloOutreachPlugin()
    captured = {}

    class DummyCursor:
        async def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

        async def fetchone(self):
            return (0,)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return DummyCursor()

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", lambda: DummyConn())

    from datetime import datetime, timezone

    result = await p._human_messages_since(datetime.now(timezone.utc))

    assert result is False  # count 0 -> no human activity
    assert "chat_history_cache" in captured["sql"]
    assert "ai_diary" not in captured["sql"]
    assert "'self'" in captured["sql"]


@pytest.mark.asyncio
async def test_live_and_recent_activity_failsafe_directions(monkeypatch):
    """On DB error (None), live-activity stays quiet (True) while recent-activity
    treats the chat as dead (False)."""
    p = GrilloOutreachPlugin()

    async def fake_none(cutoff):
        return None

    monkeypatch.setattr(p, "_human_messages_since", fake_none)

    assert await p._has_live_activity(15) is True  # fail-safe: don't double-text
    assert await p._has_recent_activity(24) is False  # fail-safe: don't spam
