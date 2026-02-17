import pytest

from core import db


@pytest.mark.asyncio
async def test_ensure_plugin_tables_executes_create_statements(monkeypatch):
    """ensure_plugin_tables should attempt to CREATE TABLE IF NOT EXISTS for critical plugin tables."""
    executed = []

    class FakeCursor:
        async def execute(self, sql, *args, **kwargs):
            executed.append(sql.strip().split()[0:4])

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        async def cursor(self):
            return FakeCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def commit(self):
            return None

    async def fake_get_conn_ctx():
        return FakeConn()

    monkeypatch.setattr("core.db.get_conn_ctx", fake_get_conn_ctx)

    # Run the preflight
    await db.ensure_plugin_tables()

    # At least one CREATE must have been issued (e.g. for bio/recent_chats)
    found_create = any(item[0].upper() == "CREATE" for item in executed)
    assert found_create is True
