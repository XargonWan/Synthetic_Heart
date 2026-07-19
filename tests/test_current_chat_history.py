import asyncio
from datetime import datetime, timezone, timedelta
from collections import deque
from types import SimpleNamespace
from typing import Any


from core import history_engine, prompt_engine


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

    now = datetime.now(timezone.utc)
    msg1 = _make_msg("Alice", "Hello world", now - timedelta(minutes=2))
    msg2 = _make_msg("Bob", "Hi there", now - timedelta(minutes=1))

    interface = "telegram_bot/123"
    context_memory = {interface: deque([])}

    async def _fake_cache_load(ip, match_chat_level: bool = False):
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

    now = datetime.now(timezone.utc)
    msg1 = _make_msg("Alice", "Old", now - timedelta(minutes=10))
    msg2 = _make_msg("Bob", "New", now - timedelta(minutes=1))

    interface = "telegram_bot/456"
    # Put an empty in-memory context so cache loader will be called to fill missing
    context_memory = {interface: deque([])}

    # Force loader to return our two messages and let last_n=1 be applied
    async def _fake_cache_load(ip, match_chat_level: bool = False):
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


def test_lite_mode_history_limit_overrides_context_verbosity(monkeypatch):
    """LITE_MODE_HISTORY_LIMIT is the WebUI-exposed dial next to the Lite Mode
    toggle -- it must be authoritative while lite mode is on, not silently
    capped by the separate, general CONTEXT_VERBOSITY dial."""
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)

    def _fake_get_int(key, default):
        if key == "CONTEXT_VERBOSITY":
            return 2
        if key == "LITE_MODE_HISTORY_LIMIT":
            return 8
        return default

    monkeypatch.setattr("core.history_engine._get_int", _fake_get_int)
    monkeypatch.setattr(
        "core.history_engine._get_bool",
        lambda key, default: True if key == "PROMPT_LITE_MODE" else default,
    )

    now = datetime.now(timezone.utc)
    msgs = [
        _make_msg(f"User{i}", f"Message {i}", now - timedelta(minutes=10 - i))
        for i in range(6)
    ]

    interface = "telegram_bot/999"
    context_memory = {interface: deque([])}

    async def _fake_cache_load(ip, match_chat_level: bool = False):
        assert ip == interface
        return deque(msgs)

    monkeypatch.setattr("core.chat_history_cache.load_chat_history", _fake_cache_load)

    message = SimpleNamespace(
        interface_path=interface,
        text="test",
        message_id=7,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=now,
    )

    res = asyncio.run(
        prompt_engine.build_json_prompt(
            message, context_memory, interface_name="telegram"
        )
    )
    entries = res["context"].get("history_current_chat", [])
    # All 6 available messages should come through -- capped by the lite-mode
    # limit (8), not by the lower general CONTEXT_VERBOSITY (2).
    assert len(entries) == 6


def test_no_duplication_with_history_recent(monkeypatch):
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})
    now = datetime.now(timezone.utc)
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

    async def _fake_global_load(limit=10):
        return deque([{**msg, "interface_path": interface}])

    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history", _fake_global_load
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

    now = datetime.now(timezone.utc)
    local_msg = _make_msg("Alice", "Local message", now - timedelta(minutes=2))
    other_msg = _make_msg("Eve", "Other chat message", now - timedelta(minutes=1))

    interface = "telegram_bot/123"
    # in-memory chat_map contains the local message
    context_memory = {interface: deque([local_msg])}

    async def _fake_cache_load(ip, match_chat_level: bool = False):
        assert ip == interface
        return deque([local_msg])

    async def _fake_global_load(limit=10):
        # Simulate DB returning both local and other messages
        return deque(
            [
                {**local_msg, "interface_path": interface},
                {**other_msg, "interface_path": "discord_bot/999"},
            ]
        )

    monkeypatch.setattr("core.chat_history_cache.load_chat_history", _fake_cache_load)
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history", _fake_global_load
    )

    message = SimpleNamespace(
        interface_path=interface,
        text="hi",
        message_id=10,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=now,
    )

    res = asyncio.run(
        prompt_engine.build_json_prompt(
            message, context_memory, interface_name="telegram"
        )
    )

    ctx = res["context"]
    # local/global aliases are only present when history_scope='local';
    # check canonical keys instead.
    assert any("Local message" in e for e in ctx["history_current_chat"])
    assert all("Other chat message" not in e for e in ctx["history_current_chat"])
    # history_recent must NOT contain the local message
    assert all("Local message" not in e for e in ctx["history_recent"])
    # history_recent should include the other chat's message
    assert any("Other chat message" in e for e in ctx["history_recent"])


def test_history_scope_local_only(monkeypatch):
    """When history_scope='local' we MUST still include both local and global histories
    in the master prompt, but mark `scope`/`history_scope` so the assistant knows
    which stream is primary."""
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)

    now = datetime.now(timezone.utc)
    local_msg = _make_msg("Alice", "Local only", now - timedelta(minutes=2))
    other_msg = _make_msg("Eve", "External", now - timedelta(minutes=1))

    interface = "telegram_bot/555"
    context_memory = {interface: deque([local_msg])}

    async def _fake_cache_load(ip, match_chat_level: bool = False):
        return deque([local_msg])

    async def _fake_global_load(limit=10):
        return deque([{**other_msg, "interface_path": "discord/1"}])

    monkeypatch.setattr("core.chat_history_cache.load_chat_history", _fake_cache_load)
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history", _fake_global_load
    )

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
    # local_history / global_history aliases are no longer emitted (they were
    # always identical to the canonical keys, wasting tokens).  Instead verify
    # the canonical keys carry the expected data.
    assert "local_history" not in ctx
    assert "global_history" not in ctx
    assert any("Local only" in e for e in ctx.get("history_current_chat", []))
    assert any("External" in e for e in ctx.get("history_recent", []))
    assert all("Local only" not in e for e in ctx.get("history_recent", []))

    # history_scope is echoed so the LLM knows which stream is primary
    assert ctx.get("history_scope") == "local"

    # The input payload should expose the requested scope so the LLM can prioritise
    payload = res.get("input", {}).get("payload", {})
    assert payload.get("scope") == "local"
    assert payload.get("history_scope", "local") == "local"


def test_unified_history_keeps_local_messages_when_global_tail_is_busy(monkeypatch):
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)
    monkeypatch.setattr(
        "core.history_engine._get_int",
        lambda key, default: 3 if key == "CONTEXT_VERBOSITY" else default,
    )
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})

    now = datetime.now(timezone.utc)
    interface = "telegram_bot/777"

    local_old = _make_msg("Alice", "Local oldest", now - timedelta(minutes=8))
    local_mid = _make_msg("Alice", "Local middle", now - timedelta(minutes=6))
    local_new = _make_msg("Alice", "Local newest", now - timedelta(minutes=4))
    other_1 = _make_msg("Eve", "Other one", now - timedelta(minutes=3))
    other_2 = _make_msg("Mallory", "Other two", now - timedelta(minutes=2))
    other_3 = _make_msg("Trent", "Other three", now - timedelta(minutes=1))

    context_memory = {
        interface: deque([local_old, local_mid, local_new]),
        "discord_bot/1": deque([{**other_1, "interface_path": "discord_bot/1"}]),
        "discord_bot/2": deque([{**other_2, "interface_path": "discord_bot/2"}]),
        "discord_bot/3": deque([{**other_3, "interface_path": "discord_bot/3"}]),
    }

    async def _fake_cache_load(ip, match_chat_level: bool = False):
        assert ip == interface
        return deque([local_old, local_mid, local_new])

    async def _fake_global_load(limit=10):
        return deque(
            [
                {**local_old, "interface_path": interface},
                {**local_mid, "interface_path": interface},
                {**local_new, "interface_path": interface},
                {**other_1, "interface_path": "discord_bot/1"},
                {**other_2, "interface_path": "discord_bot/2"},
                {**other_3, "interface_path": "discord_bot/3"},
            ]
        )

    monkeypatch.setattr("core.chat_history_cache.load_chat_history", _fake_cache_load)
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history", _fake_global_load
    )

    message = SimpleNamespace(
        interface_path=interface,
        text="current input",
        message_id=12,
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

    assert len(current_entries) == 3
    assert any("Local oldest" in e for e in current_entries)
    assert any("Local middle" in e for e in current_entries)
    assert any("Local newest" in e for e in current_entries)
    assert all("Other one" not in e for e in current_entries)

    assert len(recent_entries) == 3
    assert any("Other one" in e for e in recent_entries)
    assert any("Other two" in e for e in recent_entries)
    assert any("Other three" in e for e in recent_entries)
    assert all("Local newest" not in e for e in recent_entries)


def test_load_chat_history_for_guild_queries(monkeypatch):
    """Verify helper builds correct SQL and respects the `since` and `limit` args."""

    # prepare a fake cursor/connection that records last query and returns sample row
    class FakeCursor:
        def __init__(self):
            self.last_query = None
            self.last_params = None

        async def execute(self, query, params):
            self.last_query = query
            self.last_params = params

        async def fetchall(self):
            # return row with dummy timestamp object that has isoformat
            class T:
                def isoformat(self):
                    return "2026-01-01T00:00:00"

            return [("Alice", "alice", "hello", T(), "discord_123_456")]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    # holder so we can inspect the cursor used by the helper
    cursor_holder: dict[str, Any] = {}

    class FakeConn:
        def __init__(self):
            self.cursor_obj = FakeCursor()
            # expose to outer scope
            cursor_holder["cur"] = self.cursor_obj

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        # ``get_conn_ctx()`` in production returns an object whose ``cursor``
        # method is synchronous and returns a cursor.  Our fake must match that
        # signature, otherwise ``async with conn.cursor()`` will try to treat the
        # coroutine as a context manager and blow up with a missing ``__aenter__``.
        def cursor(self):
            return self.cursor_obj

    monkeypatch.setattr(
        "core.chat_history_cache.get_conn_ctx",
        lambda: FakeConn(),
    )

    from core.chat_history_cache import load_chat_history_for_guild

    # call without since
    result = asyncio.run(load_chat_history_for_guild(123, since=None, limit=5))
    assert len(result) == 1
    msg = result[0]
    assert msg["interface_path"] == "discord_123_456"
    # check that query included LIKE pattern and limit
    cur = cursor_holder.get("cur")
    assert cur is not None
    assert "interface_path LIKE" in cur.last_query
    # interface paths start with the guild prefix, no leading wildcard necessary
    assert cur.last_params[0].startswith("discord_123_")
    assert cur.last_params[0].endswith("%")
    assert cur.last_params[-1] == 5

    # call with since parameter
    result2 = asyncio.run(
        load_chat_history_for_guild(123, since="2026-02-01T00:00:00", limit=2)
    )
    assert len(result2) == 1
    cur2 = cursor_holder.get("cur")
    assert cur2 is not None
    assert "timestamp > %s" in cur2.last_query
    assert cur2.last_params[1] == "2026-02-01T00:00:00"
    assert cur2.last_params[-1] == 2


def test_cross_chat_privacy_note_in_recent_block(monkeypatch):
    """The [Recent context from other conversations] block must carry a note
    telling the model not to name-drop people from other chats."""
    summary = prompt_engine._build_context_summary(
        {
            "history_recent": [
                '[from the group chat] [05/07/26:0132] Lybris: "did you see the track?"'
            ]
        },
        is_grillo_internal=False,
    )
    assert "[Recent context from other conversations]" in summary
    assert "Lybris" in summary
    # The anti-name-dropping note must be present and reference the current interlocutor
    assert "name-drop" in summary
    assert "current interlocutor" in summary
    assert "current conversation" in summary


def test_cross_chat_privacy_directive_in_master_instructions(monkeypatch):
    """The master chat instruction set must include a general cross-chat privacy
    directive that applies to any non-current context (not just unified history)."""
    instructions = prompt_engine.load_json_instructions()
    assert "CROSS-CHAT PRIVACY" in instructions
    assert "name-drop" in instructions
    assert "current conversation" in instructions
    assert "current user" in instructions


def test_cross_chat_source_label_tags_telegram_group_vs_dm():
    """Cross-chat history entries must be tagged with a readable room label,
    not the raw interface_path -- otherwise the model has no way to tell
    where an injected entry came from. Telegram group chat IDs are negative,
    private chat IDs are positive; use that to say which room without
    needing any chat-title tracking (nothing populates interface_path_pretty)."""
    group_entry = {
        "interface_path": "telegram_bot/-100987654321",
        "sender_name": "SynthA",
        "text": "hello from the group",
        "timestamp": "2026-07-01T18:00:00+00:00",
    }
    dm_entry = {
        "interface_path": "telegram_bot/5551234567",
        "sender_name": "Alice",
        "text": "hello from the dm",
        "timestamp": "2026-07-01T18:00:00+00:00",
    }

    group_line = history_engine._entry_to_text_with_source(
        group_entry, current_interface_path="telegram_bot/5551234567"
    )
    dm_line = history_engine._entry_to_text_with_source(
        dm_entry, current_interface_path="telegram_bot/-100987654321"
    )

    assert "[from the group chat]" in group_line
    assert "telegram_bot/-100987654321" not in group_line
    assert "[from your DM]" in dm_line
    assert "telegram_bot/5551234567" not in dm_line


def test_telegram_chat_kind_classifies_group_and_dm():
    """telegram_chat_kind is the reusable primitive behind the cross-chat
    label above -- also consumed directly by prompt_engine to tell the model
    whether the *current* turn itself is happening in the group or a DM."""
    assert history_engine.telegram_chat_kind("telegram_bot/-100987654321") == "group"
    assert history_engine.telegram_chat_kind("telegram_bot/5551234567") == "dm"


def test_telegram_chat_kind_none_for_non_telegram_or_unparseable():
    assert history_engine.telegram_chat_kind("discord_bot/123456") is None
    assert history_engine.telegram_chat_kind("telegram_bot/not_a_number") is None
    assert history_engine.telegram_chat_kind("telegram_bot") is None


def test_source_label_is_none_for_current_chat():
    """An entry from the chat we're already building a prompt for shouldn't
    get a redundant '[from ...]' tag."""
    entry = {
        "interface_path": "telegram_bot/-100987654321",
        "sender_name": "SynthA",
        "text": "hi",
        "timestamp": "2026-07-01T18:00:00+00:00",
    }
    line = history_engine._entry_to_text_with_source(
        entry, current_interface_path="telegram_bot/-100987654321"
    )
    assert "[from" not in line
