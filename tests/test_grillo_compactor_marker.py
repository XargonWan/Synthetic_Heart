import json
from datetime import datetime
import pytest

from plugins.grillo.grillo_compactor import GrilloCompactorPlugin


@pytest.mark.asyncio
async def test_run_action_marker_filters(monkeypatch):
    p = GrilloCompactorPlugin()

    class DummyCursor:
        def __init__(self):
            self.queries = []

        async def execute(self, sql, params=None):
            self.queries.append((sql, params))

        async def fetchall(self):
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
    monkeypatch.setattr(cdb, "_get_db_type", lambda: "mariadb")

    # Run action with marker
    res = await p.run_action(
        "compact_now", payload={"cycles": 1, "dry_run": True, "marker": "food"}
    )
    assert res.get("status") == "ok"

    # Retrieve queries captured by the cursor used during execution
    q = conn_instance.cursor().queries
    assert any("ai_diary" in sql and "JSON_CONTAINS" in sql for sql, params in q), (
        f"Expected ai_diary+JSON_CONTAINS in queries: {q}"
    )
    assert any(params and json.dumps("food") in str(params) for sql, params in q)


@pytest.mark.asyncio
async def test_run_action_marker_filters_postgres(monkeypatch):
    p = GrilloCompactorPlugin()

    class DummyCursor:
        def __init__(self):
            self.queries = []

        async def execute(self, sql, params=None):
            self.queries.append((sql, params))

        async def fetchall(self):
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
    monkeypatch.setattr(cdb, "_get_db_type", lambda: "postgres")

    res = await p.run_action(
        "compact_now", payload={"cycles": 1, "dry_run": True, "marker": "food"}
    )
    assert res.get("status") == "ok"

    q = conn_instance.cursor().queries
    assert any("::jsonb ? %s" in sql for sql, params in q), q
    assert all("JSON_CONTAINS" not in sql for sql, params in q)
    assert any(params and params[1] == "food" for sql, params in q if len(params) > 1)
    assert any(
        params and isinstance(params[0], datetime)
        for sql, params in q
        if len(params) > 0
    )
