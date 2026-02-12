import asyncio
from datetime import datetime, timezone, timedelta
from collections import deque
from types import SimpleNamespace


from core import prompt_engine


async def _dummy_gather(message, ctx):
    return {}


def _make_msg(name, text, dt):
    return {
        "sender_name": name,
        "sender_id": name.lower(),
        "text": text,
        "timestamp": dt.isoformat(),
    }


def test_format_current_chat_history(monkeypatch):
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)

    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    msg1 = _make_msg("Alice", "Hello world", now - timedelta(minutes=2))
    msg2 = _make_msg("Bob", "Hi there", now - timedelta(minutes=1))

    interface = "telegram_bot/123"
    context_memory = {interface: deque([])}

    async def _fake_cache_load(ip):
        assert ip == interface
        return deque([msg1, msg2])

    monkeypatch.setattr("core.chat_history_cache.load_chat_history", _fake_cache_load)

    message = SimpleNamespace(
        interface_path=interface,
        text="test",
        message_id=1,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=now,
    )

    res = asyncio.run(
        prompt_engine.build_json_prompt(
            message, context_memory, interface_name="telegram"
        )
    )
    assert "history_current_chat" in res["context"]
    entries = res["context"]["history_current_chat"]
    assert isinstance(entries, list)
    assert len(entries) == 2
    assert "Bob" in entries[-1]
    assert entries[0].startswith("[") and "]" in entries[0]


def test_current_chat_history_respects_last_n(monkeypatch):
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)
    # Set global verbosity to 1
    monkeypatch.setattr(
        "core.history_engine._get_int",
        lambda key, default: 1 if key == "CONTEXT_VERBOSITY" else default,
    )

    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    msg1 = _make_msg("Alice", "Old", now - timedelta(minutes=10))
    msg2 = _make_msg("Bob", "New", now - timedelta(minutes=1))

    interface = "telegram_bot/456"
    # Put an empty in-memory context so cache loader will be called to fill missing
    context_memory = {interface: deque([])}

    # Force loader to return our two messages and let last_n=1 be applied
    async def _fake_cache_load(ip):
        assert ip == interface
        return deque([msg1, msg2])

    monkeypatch.setattr("core.chat_history_cache.load_chat_history", _fake_cache_load)

    message = SimpleNamespace(
        interface_path=interface,
        text="test",
        message_id=2,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=now,
    )

    res = asyncio.run(
        prompt_engine.build_json_prompt(
            message, context_memory, interface_name="telegram"
        )
    )
    entries = res["context"].get("history_current_chat", [])
    assert len(entries) == 1
    assert "Bob" in entries[0]


def test_no_duplication_with_history_recent(monkeypatch):
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    msg = _make_msg("Carol", "Dup", now - timedelta(minutes=1))

    interface = "telegram_bot/789"
    # Put same message into current chat; recent history should not duplicate it
    context_memory = {interface: deque([msg])}

    message = SimpleNamespace(
        interface_path=interface,
        text="test",
        message_id=3,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=now,
    )

    res = asyncio.run(
        prompt_engine.build_json_prompt(
            message, context_memory, interface_name="telegram"
        )
    )
    current_entries = res["context"].get("history_current_chat", [])
    recent_entries = res["context"].get("history_recent", [])
    assert any("Carol" in e for e in current_entries)
    assert recent_entries == []
