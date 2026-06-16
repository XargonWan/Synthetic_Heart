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


@pytest.mark.asyncio
async def test_ensure_plugin_tables_creates_ai_diary_if_plugin_init_fails(monkeypatch):
    """If plugins.ai_diary.init_diary_table raises, ensure_plugin_tables must still
    create a fallback `ai_diary` table so queries that JOIN it don't keep failing.
    """
    executed_sql = []

    class FakeCursor:
        async def execute(self, sql, *args, **kwargs):
            executed_sql.append(sql)

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

    # Simulate plugin init failing (importable but init raises)
    async def fake_init_fail():
        raise RuntimeError("simulated init_diary_table failure")

    monkeypatch.setattr("core.db.get_conn_ctx", fake_get_conn_ctx)
    monkeypatch.setattr("plugins.ai_diary.init_diary_table", fake_init_fail)

    # Run the preflight
    from core import db

    await db.ensure_plugin_tables()

    # Ensure at least one CREATE for ai_diary was attempted by the fallback
    found_ai = any("ai_diary" in (s or "").lower() for s in executed_sql)
    assert found_ai, f"ai_diary CREATE not issued, SQL executed: {executed_sql}"


@pytest.mark.asyncio
async def test_ensure_plugin_tables_creates_minimal_ai_diary_placeholder(monkeypatch):
    """When plugin init fails, the DB preflight must create a *minimal*
    placeholder for `ai_diary` (avoid duplicating plugin-managed schema).
    """
    executed_sql = []

    class FakeCursor:
        async def execute(self, sql, *args, **kwargs):
            executed_sql.append(sql)

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

    # Simulate plugin init failing (importable but init raises)
    async def fake_init_fail():
        raise RuntimeError("simulated init_diary_table failure")

    monkeypatch.setattr("core.db.get_conn_ctx", fake_get_conn_ctx)
    monkeypatch.setattr("plugins.ai_diary.init_diary_table", fake_init_fail)

    # Run the preflight
    from core import db

    await db.ensure_plugin_tables()

    # Find the ai_diary CREATE statement
    ai_creates = [
        s
        for s in executed_sql
        if s and "create table" in s.lower() and "ai_diary" in s.lower()
    ]
    assert ai_creates, f"No ai_diary CREATE found in executed SQL: {executed_sql}"

    ai_sql = "\n".join(ai_creates).lower()

    # Must contain only minimal columns used by joins (id, content, timestamp)
    assert "content" in ai_sql and "timestamp" in ai_sql and "id" in ai_sql

    # Must NOT contain plugin-managed columns (guard against schema duplication)
    for forbidden in (
        "context_tags",
        "involved_users",
        "personal_thought",
        "interface",
        "chat_id",
    ):
        assert forbidden not in ai_sql, (
            f"Fallback created plugin-only column: {forbidden}"
        )


@pytest.mark.asyncio
async def test_ensure_plugin_tables_supports_proxy_cursor_contexts(monkeypatch):
    executed = []

    class InnerCursor:
        async def execute(self, sql, *args, **kwargs):
            executed.append(sql)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class ProxyCursor:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def close(self):
            return None

    class FakeConn:
        def cursor(self):
            async def _make_cursor():
                return ProxyCursor(InnerCursor())

            return _make_cursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def commit(self):
            return None

    async def fake_get_conn_ctx():
        return FakeConn()

    async def fake_init_diary_table():
        return None

    monkeypatch.setattr("core.db.get_conn_ctx", fake_get_conn_ctx)
    monkeypatch.setattr("plugins.ai_diary.init_diary_table", fake_init_diary_table)

    await db.ensure_plugin_tables()

    assert executed, "Expected ensure_plugin_tables to execute CREATE statements"
