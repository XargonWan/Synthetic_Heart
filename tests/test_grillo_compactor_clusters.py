import asyncio
import json
import pytest

from plugins.grillo.grillo_compactor import GrilloCompactorPlugin


@pytest.mark.asyncio
async def test_multiple_clusters_and_preserve_non_sources(monkeypatch):
    p = GrilloCompactorPlugin()

    # Mock DB: return 3 candidate memories
    class DummyCursor:
        def __init__(self):
            self.queries = []

        async def execute(self, sql, params=None):
            self.queries.append((sql, params))

        async def fetchall(self):
            # Return 3 rows: two frog memories and one risotto memory
            return [
                {
                    "id": 201,
                    "content": "I wonder how it feels to be a frog",
                    "tags": json.dumps(["life"]),
                    "timestamp": "2020-01-01",
                },
                {
                    "id": 202,
                    "content": "Maybe frogs are blue sometimes",
                    "tags": json.dumps(["life"]),
                    "timestamp": "2020-01-02",
                },
                {
                    "id": 203,
                    "content": "I once ate frog risotto at the market",
                    "tags": json.dumps(["food"]),
                    "timestamp": "2020-01-03",
                },
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

    # Mock LLM to return two clusters: frogs cluster (should compact) and risotto as single cluster (should_compact=false)
    class FakeEngine:
        async def generate_response(self, prompt):
            # Return cluster JSON
            return json.dumps(
                {
                    "clusters": [
                        {
                            "cluster_id": 1,
                            "should_compact": True,
                            "summary": "Questions about being a frog",
                            "summary_chars": 28,
                            "tags": ["frog"],
                            "feeling": "curious",
                            "source_ids": [201, 202],
                            "confidence": "high",
                            "justification": "both entries ask about what it's like to be a frog",
                        },
                        {
                            "cluster_id": 2,
                            "should_compact": False,
                            "summary": "A memory about eating frog risotto",
                            "summary_chars": 36,
                            "tags": ["food"],
                            "feeling": "neutral",
                            "source_ids": [203],
                            "confidence": "high",
                            "justification": "this is a distinct event about food",
                        },
                    ]
                }
            )

    class FakeRegistry:
        def get_engine(self, name):
            return FakeEngine()

    # Patch the llm registry function directly by module path
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )
    monkeypatch.setattr(
        "core.config.get_active_cortex_engine",
        lambda: asyncio.sleep(0, result="dummy"),
    )

    # Run one cycle
    res = await p._run_one_compaction_cycle(dry_run=False)
    assert res is True


@pytest.mark.asyncio
async def test_dry_run_returns_proposed_clusters(monkeypatch):
    p = GrilloCompactorPlugin()

    class DummyCursor2:
        async def execute(self, sql, params=None):
            pass

        async def fetchall(self):
            return [
                {
                    "id": 301,
                    "content": "I love apples",
                    "tags": json.dumps(["food"]),
                    "timestamp": "2020-02-01",
                },
                {
                    "id": 302,
                    "content": "Apples are delicious in pie",
                    "tags": json.dumps(["food"]),
                    "timestamp": "2020-02-02",
                },
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyConn2:
        def __init__(self):
            self.executed = []

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

    # Fake LLM returns a single cluster compacting both apples
    class FakeEngine2:
        async def generate_response(self, prompt):
            return json.dumps(
                {
                    "clusters": [
                        {
                            "cluster_id": 1,
                            "should_compact": True,
                            "summary": "Apples and apple pie memories",
                            "summary_chars": 30,
                            "tags": ["food", "apple"],
                            "feeling": "pleasant",
                            "source_ids": [301, 302],
                            "confidence": "high",
                            "justification": "shared theme about apples",
                        }
                    ]
                }
            )

    class FakeRegistry2:
        def get_engine(self, name):
            return FakeEngine2()

    # Patch the llm registry function directly by module path
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry2()
    )
    monkeypatch.setattr(
        "core.config.get_active_cortex_engine",
        lambda: asyncio.sleep(0, result="dummy"),
    )

    result = await p._run_one_compaction_cycle(dry_run=True)
    assert isinstance(result, dict)
    assert result.get("dry_run") is True
    results = result.get("results")
    assert isinstance(results, list) and len(results) == 1
    assert results[0]["status"] == "ok" and results[0]["should_compact"] is True
