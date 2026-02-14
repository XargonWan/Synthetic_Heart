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


def test_local_global_separation(monkeypatch):
    """Verify prompt exposes `local_history` and `global_history` and that
    global_history excludes messages from the same `interface_path`."""
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)

    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    local_msg = _make_msg("Alice", "Local message", now - timedelta(minutes=2))
    other_msg = _make_msg("Eve", "Other chat message", now - timedelta(minutes=1))

    interface = "telegram_bot/123"
    # in-memory chat_map contains the local message
    context_memory = {interface: deque([local_msg])}

    async def _fake_cache_load(ip):
        assert ip == interface
        return deque([local_msg])

    async def _fake_global_load(limit=10):
        # Simulate DB returning both local and other messages
        return deque([
            {**local_msg, "interface_path": interface},
            {**other_msg, "interface_path": "discord_bot/999"},
        ])

    monkeypatch.setattr("core.chat_history_cache.load_chat_history", _fake_cache_load)
    monkeypatch.setattr("core.chat_history_cache.load_global_chat_history", _fake_global_load)

    message = SimpleNamespace(
        interface_path=interface,
        text="hi",
        message_id=10,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=now,
    )

    res = asyncio.run(
        prompt_engine.build_json_prompt(message, context_memory, interface_name="telegram")
    )

    ctx = res["context"]
    assert "local_history" in ctx and "global_history" in ctx
    assert any("Local message" in e for e in ctx["local_history"]) 
    # global_history must NOT contain the local message
    assert all("Local message" not in e for e in ctx["global_history"])
    # global_history should include the other chat's message
    assert any("Other chat message" in e for e in ctx["global_history"])


def test_history_scope_local_only(monkeypatch):
    """When history_scope='local' we MUST still include both local and global histories
    in the master prompt, but mark `scope`/`history_scope` so the assistant knows
    which stream is primary."""
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)

    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    local_msg = _make_msg("Alice", "Local only", now - timedelta(minutes=2))
    other_msg = _make_msg("Eve", "External", now - timedelta(minutes=1))

    interface = "telegram_bot/555"
    context_memory = {interface: deque([local_msg])}

    async def _fake_cache_load(ip):
        return deque([local_msg])

    async def _fake_global_load(limit=10):
        return deque([{**other_msg, "interface_path": "discord/1"}])

    monkeypatch.setattr("core.chat_history_cache.load_chat_history", _fake_cache_load)
    monkeypatch.setattr("core.chat_history_cache.load_global_chat_history", _fake_global_load)

    message = SimpleNamespace(
        interface_path=interface,
        text="check",
        message_id=11,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=now,
    )

    # Explicit per-call override — expect BOTH histories present but scope marked
    res = asyncio.run(
        prompt_engine.build_json_prompt(
            message, context_memory, interface_name="telegram", history_scope="local"
        )
    )

    ctx = res["context"]
    # local_history must contain the local entry
    assert any("Local only" in e for e in ctx.get("local_history", []))
    # global_history must still include external chat entries (and MUST NOT include local entries)
    assert any("External" in e for e in ctx.get("global_history", []))
    assert all("Local only" not in e for e in ctx.get("global_history", []))

    # The input payload should expose the requested scope so the LLM can prioritise
    assert res.get("input", {}).get("payload", {}).get("history_scope") == "local"
    assert res.get("input", {}).get("payload", {}).get("scope") == "local"

    # Also verify that passing the scope via `context_memory` dict works equivalently
    context_memory_with_scope = {**context_memory, "history_scope": "local"}
    res2 = asyncio.run(
        prompt_engine.build_json_prompt(message, context_memory_with_scope, interface_name="telegram")
    )
    ctx2 = res2["context"]
    assert any("External" in e for e in ctx2.get("global_history", []))
    assert res2.get("input", {}).get("payload", {}).get("history_scope") == "local"
