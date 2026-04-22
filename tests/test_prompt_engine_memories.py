from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.prompt_engine import build_json_prompt, search_memories


@pytest.mark.asyncio
async def test_search_memories_includes_ai_diary(monkeypatch):
    # Dummy cursor that records executed queries and returns rows for ai_diary query
    class DummyCursor:
        def __init__(self):
            self.queries = []
            self.calls = 0

        async def execute(self, sql, params=None):
            self.calls += 1
            self.queries.append((sql, params))

        async def fetchall(self):
            # First call: memories query -> return empty
            if self.calls == 1:
                return []
            # Second call: ai_diary query -> return some rows
            return [["Diary memory A"], ["Diary memory B"]]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyConn:
        def __init__(self):
            self.cursor_obj = DummyCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_obj

    conn_instance = DummyConn()

    def mock_get_conn_ctx():
        return conn_instance

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx)
    import core.prompt_engine as pe

    monkeypatch.setattr(pe, "get_conn_ctx", mock_get_conn_ctx)

    results = await search_memories(tags=["food"], limit=5)
    assert "Diary memory A" in results
    assert "Diary memory B" in results


@pytest.mark.asyncio
async def test_build_json_prompt_merges_soul_recalled_memories(monkeypatch):
    soul_memory = (
        "[SOUL recalled memory | 2026-04-18 | same chat] Scarlet loves jasmine tea."
    )

    async def fake_build_context(
        self,
        *,
        message,
        context_memory,
        interface_name,
        text,
        memories,
        history_scope=None,
    ):
        del self, message, context_memory, interface_name, text, memories, history_scope
        return {"memories": ["Legacy memory"]}

    async def fake_gather_static_injections(message, context_memory):
        del message, context_memory
        return {"soul_recalled_memories": [soul_memory]}

    async def fake_gather_recon_contributions(**kwargs):
        del kwargs
        return []

    async def fake_resolve_language(**kwargs):
        del kwargs
        return None

    async def fake_resolve_tone(**kwargs):
        del kwargs
        return None, None

    monkeypatch.setattr("core.prompt_engine.extract_tags", lambda _text: [])
    monkeypatch.setattr("core.prompt_engine.expand_tags", lambda tags: tags)
    monkeypatch.setattr(
        "core.history_engine.HistoryEngine.build_context", fake_build_context
    )
    monkeypatch.setattr(
        "core.action_parser.gather_static_injections", fake_gather_static_injections
    )
    monkeypatch.setattr(
        "core.recon.gather_recon_contributions", fake_gather_recon_contributions
    )
    monkeypatch.setattr("core.recon.resolve_language", fake_resolve_language)
    monkeypatch.setattr("core.recon.resolve_tone", fake_resolve_tone)
    monkeypatch.setattr(
        "core.prompt_engine.load_json_instructions",
        lambda: "RESPOND ONLY WITH VALID JSON",
    )

    message = SimpleNamespace(
        interface_path="telegram_bot/123",
        text="hello",
        caption=None,
        message_id=1,
        date=datetime.now(timezone.utc),
        from_user=None,
        reply_to_message=None,
    )

    result = await build_json_prompt(message, {}, interface_name="telegram_bot")

    assert result["context"]["memories"] == ["Legacy memory", soul_memory]
