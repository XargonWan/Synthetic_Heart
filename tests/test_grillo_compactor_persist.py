import json
import pytest
import asyncio

from plugins.grillo.grillo_compactor import GrilloCompactorPlugin


@pytest.mark.asyncio
async def test_persist_compaction_moves_to_archive_and_inserts_memory(monkeypatch):
    p = GrilloCompactorPlugin()

    class DummyCursor:
        def __init__(self):
            self.queries = []
            self.calls = 0

        async def execute(self, sql, params=None):
            self.calls += 1
            self.queries.append((sql, params))

        async def fetchall(self):
            # First call: candidate selection (two entries)
            if self.calls == 1:
                return [ {"id": 301, "content": "Old food memory A", "tags": json.dumps(["food"]), "timestamp": "2020-01-01"}, {"id": 302, "content": "Old food memory B", "tags": json.dumps(["food"]), "timestamp": "2020-01-02"} ]
            # For other fetches, return empty
            return []

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

    # Mock LLM to return a compactable cluster
    class FakeEngine:
        async def generate_response(self, prompt):
            return json.dumps({"clusters": [{"cluster_id": 1, "should_compact": True, "summary": "Compact summary", "summary_chars": 15, "tags": ["food"], "feeling": "nostalgic", "source_ids": [301,302], "confidence": "high", "justification": "similar"}]})

    class FakeRegistry:
        def get_engine(self, name):
            return FakeEngine()

    monkeypatch.setattr('core.llm_registry.get_llm_registry', lambda: FakeRegistry())
    monkeypatch.setattr('core.config.get_active_llm', lambda: asyncio.sleep(0, result='selenium_chatgpt'))

    # Capture insert_memory calls
    called = {}
    async def fake_insert_memory(*, content, author, source, tags, emotion, intensity, emotion_state, timestamp=None, scope=None):
        called['content'] = content
        called['author'] = author
        called['tags'] = tags

    monkeypatch.setattr('core.db.insert_memory', fake_insert_memory)

    # Run clustering/persistence on window
    window = [ {"id":301, "content":"Old food memory A", "tags": json.dumps(["food"]), "timestamp":"2020-01-01"}, {"id":302, "content":"Old food memory B", "tags": json.dumps(["food"]), "timestamp":"2020-01-02"} ]
    res = await p._cluster_and_compact_batch(window, dry_run=False)
    assert res is True

    # Ensure that archive insert and delete queries were executed
    queries = conn_instance.cursor().queries
    assert any('INSERT INTO ai_diary_archive' in sql for sql, params in queries), f"Archive insert not found in: {queries}"
    assert any('DELETE FROM ai_diary' in sql for sql, params in queries), f"Delete from ai_diary not found in: {queries}"

    # Ensure insert_memory was called with summary content
    assert called.get('content') == 'Compact summary'
    assert called.get('author') == 'grillo'
    assert json.loads(called.get('tags')) == ['food']
