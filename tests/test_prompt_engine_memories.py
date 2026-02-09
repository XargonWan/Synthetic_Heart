import pytest
import json
import asyncio

from core.prompt_engine import search_memories


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
