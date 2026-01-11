import asyncio
import pytest
from plugins.grillo.grillo_dream import GrilloDreamPlugin


@pytest.mark.asyncio
async def test_build_dream_prompt_contains_instructions():
    p = GrilloDreamPlugin()
    fragments = ["(chat:telegram_bot/1) Hello world", "(memory) Remember the red cat"]
    prompt = p._build_dream_prompt(fragments)

    assert "G.R.I.L.L.O. DREAM" in prompt
    assert "Fragments:" in prompt
    assert "create_personal_diary_entry" in prompt
    assert '"autonomous": true' in prompt
    # deduplication instruction should be present
    assert 'check the fragments' in prompt.lower() or 'avoid repeating' in prompt.lower() or 'do not repeat' in prompt.lower()


@pytest.mark.asyncio
async def test_collect_fragments_with_mocks(monkeypatch):
    p = GrilloDreamPlugin()

    async def mock_get_last_active_chats_verbose(n):
        return [(123, "Chat A"), (456, "Chat B")]

    async def mock_load_chat_history(interface_path):
        from collections import deque
        return deque([{"text": "hi there"}, {"text": "how are you?"}])

    async def mock_fetch_memories(limit):
        # Simulate DB rows
        return ["mem1", "mem2"]

    # Patch recent_chats and chat_history
    import core.recent_chats as recent_chats
    monkeypatch.setattr(recent_chats, "get_last_active_chats_verbose", mock_get_last_active_chats_verbose)

    import core.chat_history_cache as chat_history_cache
    monkeypatch.setattr(chat_history_cache, "load_chat_history", mock_load_chat_history)

    # Patch DB call inside _collect_fragments by monkeypatching core.db.get_conn_ctx to a fake
    class DummyCursor:
        async def execute(self, *args, **kwargs):
            pass
        async def fetchall(self):
            return [("some memory",), ("another mem",)]
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

    async def mock_get_conn_ctx():
        return DummyConn()

    import core.db as cdb
    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx)

    fragments = await p._collect_fragments(4)
    assert isinstance(fragments, list)
    assert len(fragments) <= 4
    # fragments should contain markers like (chat: or (memory) and include sender metadata
    assert any(f.startswith("(chat:") or f.startswith("(memory)") for f in fragments)
    assert any('sender:' in f or 'sender:' in f for f in fragments)


def test_seconds_until_next_run_returns_int():
    p = GrilloDreamPlugin()
    sec = p._seconds_until_next_run("05:00")
    assert isinstance(sec, int)
    assert 0 <= sec <= 24 * 3600
