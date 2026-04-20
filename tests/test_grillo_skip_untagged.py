import asyncio
import json
import pytest

from plugins.grillo.grillo_compactor import GrilloCompactorPlugin


@pytest.mark.asyncio
async def test_skip_leading_untagged_candidates(monkeypatch):
    p = GrilloCompactorPlugin()

    # DB: first two untagged, then two tagged
    class DummyCursor:
        def __init__(self):
            self.queries = []

        async def execute(self, sql, params=None):
            self.queries.append((sql, params))

        async def fetchall(self):
            return [
                {
                    "id": 701,
                    "content": "No tags here 1",
                    "tags": None,
                    "timestamp": "2020-01-01",
                },
                {
                    "id": 702,
                    "content": "No tags here 2",
                    "tags": None,
                    "timestamp": "2020-01-02",
                },
                {
                    "id": 703,
                    "content": "Tagged memory A",
                    "tags": json.dumps(["a"]),
                    "timestamp": "2020-01-03",
                },
                {
                    "id": 704,
                    "content": "Tagged memory B",
                    "tags": json.dumps(["b"]),
                    "timestamp": "2020-01-04",
                },
            ]

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

    def mock_get_conn_ctx():
        return DummyConn()

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx)

    # Fake LLM returns a single cluster for the tagged ids
    class FakeEngine:
        async def generate_response(self, prompt):
            return json.dumps(
                {
                    "clusters": [
                        {
                            "cluster_id": 1,
                            "should_compact": True,
                            "summary": "Tagged summary",
                            "summary_chars": 14,
                            "tags": ["a"],
                            "feeling": "ok",
                            "source_ids": [703, 704],
                            "confidence": "high",
                            "justification": "same",
                        }
                    ]
                }
            )

    class FakeRegistry:
        def get_engine(self, name):
            return FakeEngine()

    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )
    monkeypatch.setattr(
        "core.config.get_active_cortex_engine",
        lambda: asyncio.sleep(0, result="dummy"),
    )

    # Capture logs to ensure skip was logged
    logged = []
    monkeypatch.setattr(
        "plugins.grillo.grillo_compactor.log_info",
        lambda msg, *a, **kw: logged.append(str(msg)),
    )

    res = await p._run_one_compaction_cycle(dry_run=True)
    assert isinstance(res, dict)
    # Ensure the skip message was logged
    assert any("Skipping 2 leading untagged candidate(s)" in m for m in logged), (
        f"Expected skip log in: {logged}"
    )
    # Ensure the dry-run results are present
    assert res.get("dry_run") is True
    assert len(res.get("results", [])) == 1
    assert res["results"][0]["status"] in (
        "ok",
        "persisted",
        "too_small",
        "skipped_not_short_enough",
    )
