import asyncio
import json
import pytest

from plugins.grillo.grillo_compactor import GrilloCompactorPlugin


@pytest.mark.asyncio
async def test_skip_whole_untagged_batches_then_process_next(monkeypatch):
    p = GrilloCompactorPlugin()

    class DummyCursor:
        def __init__(self):
            self.calls = 0
            self.queries = []

        async def execute(self, sql, params=None):
            self.calls += 1
            self.queries.append((sql, params))

        async def fetchall(self):
            # First call: untagged batch
            if self.calls == 1:
                return [
                    {"id": 1001, "content": "Legacy A", "tags": None, "timestamp": "2020-01-01"},
                    {"id": 1002, "content": "Legacy B", "tags": None, "timestamp": "2020-01-02"},
                ]
            # Second call: tagged batch
            if self.calls == 2:
                return [
                    {"id": 1003, "content": "Tagged A", "tags": json.dumps(["food"]), "timestamp": "2020-02-01"},
                    {"id": 1004, "content": "Tagged B", "tags": json.dumps(["food"]), "timestamp": "2020-02-02"},
                ]
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
    monkeypatch.setattr(cdb, 'get_conn_ctx', mock_get_conn_ctx)

    # Fake LLM returns a single cluster for the tagged ids
    class FakeEngine:
        async def generate_response(self, prompt):
            return json.dumps({"clusters": [{"cluster_id":1, "should_compact": True, "summary": "Tagged summary", "summary_chars": 14, "tags":["food"], "feeling":"ok", "source_ids":[1003,1004], "confidence":"high", "justification":"same"}]})

    class FakeRegistry:
        def get_engine(self, name):
            return FakeEngine()

    monkeypatch.setattr('core.llm_registry.get_llm_registry', lambda: FakeRegistry())
    monkeypatch.setattr('core.config.get_active_llm', lambda: asyncio.sleep(0, result='dummy'))

    logged = []
    monkeypatch.setattr('plugins.grillo.grillo_compactor.log_info', lambda msg, *a, **kw: logged.append(str(msg)))
    monkeypatch.setattr('plugins.grillo.grillo_compactor.log_debug', lambda msg, *a, **kw: logged.append(str(msg)))

    res = await p._run_one_compaction_cycle(dry_run=True)
    assert isinstance(res, dict)
    assert res.get('dry_run') is True
    assert len(res.get('results', [])) == 1
    assert any('Skipping entire batch of' in m for m in logged), f"Expected skip log in: {logged}"
    assert any('Dry-run clustering results' in m or 'Processed clusters for current window' in m for m in logged), f"Expected processing log in: {logged}"
