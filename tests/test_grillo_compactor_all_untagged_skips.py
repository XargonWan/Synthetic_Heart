import pytest

from plugins.grillo.grillo_compactor import GrilloCompactorPlugin


@pytest.mark.asyncio
async def test_all_untagged_batch_skipped(monkeypatch):
    p = GrilloCompactorPlugin()

    class DummyCursor:
        def __init__(self):
            self.offset = 0

        async def execute(self, sql, params=None):
            self.offset = params[2] if params and len(params) >= 3 else 0

        async def fetchall(self):
            if self.offset > 0:
                return []
            return [
                {
                    "id": 901,
                    "content": "Legacy A",
                    "tags": None,
                    "timestamp": "2020-01-01",
                },
                {
                    "id": 902,
                    "content": "Legacy B",
                    "tags": None,
                    "timestamp": "2020-01-02",
                },
            ]

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

    def mock_get_conn_ctx():
        return DummyConn()

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx)

    logged = []
    monkeypatch.setattr(
        "plugins.grillo.grillo_compactor.log_info",
        lambda msg, *a, **kw: logged.append(str(msg)),
    )

    res = await p._run_one_compaction_cycle(dry_run=True)
    assert res == {"dry_run": True, "results": []}
    assert any("Skipping entire batch of 2 untagged candidate(s)" in m for m in logged)
