import asyncio
import pytest
import json

from plugins.grillo.grillo_compactor import GrilloCompactorPlugin


@pytest.mark.asyncio
async def test_seconds_until_next_run_returns_int():
    p = GrilloCompactorPlugin()
    sec = p._seconds_until_next_run("03:00")
    assert isinstance(sec, int)
    assert 0 <= sec <= 24 * 3600


@pytest.mark.asyncio
async def test_run_one_compaction_cycle_basic(monkeypatch):
    p = GrilloCompactorPlugin()

    # Prepare fake DB: first call returns candidates, second returns batch
    class DummyCursor:
        def __init__(self, which=0):
            self.queries = []
            self.which = which

        async def execute(self, sql, params=None):
            self.queries.append((sql, params))

        async def fetchall(self):
            # First call (candidates)
            if len(self.queries) == 1:
                # return list of dict rows like aiomysql.DictCursor would
                return [
                    {
                        "id": 101,
                        "content": "I love pizza",
                        "tags": json.dumps(["food", "pizza"]),
                        "timestamp": "2023-01-01",
                    }
                ]
            # Second call (batch)
            return [
                {
                    "id": 101,
                    "content": "I love pizza",
                    "tags": json.dumps(["food", "pizza"]),
                    "timestamp": "2023-01-01",
                }
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyConn:
        def __init__(self):
            self.executed = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return DummyCursor()

    def mock_get_conn_ctx():
        return DummyConn()

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx)

    # Mock LLM engine
    class FakeEngine:
        async def generate_response(self, prompt):
            return json.dumps(
                {
                    "summary": "I like pizza and food",
                    "tags": ["food", "pizza"],
                    "feeling": "nostalgic",
                    "source_ids": [101],
                    "confidence": "high",
                }
            )

    class FakeRegistry:
        def get_engine(self, name):
            return FakeEngine()

    monkeypatch.setattr("core.llm_registry.get_llm_registry", lambda: FakeRegistry())

    # Ensure active LLM name resolves without DB access
    monkeypatch.setattr(
        "core.config.get_active_llm",
        lambda: asyncio.sleep(0, result="selenium_chatgpt"),
    )

    # Run one cycle
    res = await p._run_one_compaction_cycle()
    assert res is True


@pytest.mark.asyncio
async def test_tag_selection_fallback(monkeypatch):
    p = GrilloCompactorPlugin()

    # Candidates: oldest has no tags, second has tags
    class DummyCursor2:
        def __init__(self):
            self.calls = 0

        async def execute(self, sql, params=None):
            self.calls += 1

        async def fetchall(self):
            if self.calls == 1:
                return [
                    {
                        "id": 201,
                        "content": "no tags here",
                        "tags": None,
                        "timestamp": "2020-01-01",
                    },
                    {
                        "id": 202,
                        "content": "tagged mem",
                        "tags": json.dumps(["travel"]),
                        "timestamp": "2020-02-01",
                    },
                ]
            return [
                {
                    "id": 202,
                    "content": "tagged mem",
                    "tags": json.dumps(["travel"]),
                    "timestamp": "2020-02-01",
                }
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyConn2:
        def __init__(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return DummyCursor2()

    def mock_get_conn_ctx2():
        return DummyConn2()

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx2)

    # Fake LLM engine as before
    class FakeEngine2:
        async def generate_response(self, prompt):
            return json.dumps(
                {
                    "summary": "Travel memory summary",
                    "tags": ["travel"],
                    "feeling": "happy",
                    "source_ids": [202],
                    "confidence": "medium",
                }
            )

    class FakeRegistry2:
        def get_engine(self, name):
            return FakeEngine2()

    monkeypatch.setattr("core.llm_registry.get_llm_registry", lambda: FakeRegistry2())
    monkeypatch.setattr(
        "core.config.get_active_llm",
        lambda: asyncio.sleep(0, result="selenium_chatgpt"),
    )

    res = await p._run_one_compaction_cycle()
    assert res is True
