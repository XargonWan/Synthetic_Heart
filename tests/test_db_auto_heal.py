import os
import asyncio
import pytest

import core.db as db_module


class FakeCursor:
    def __init__(self, shared_state):
        self._state = shared_state

    async def execute(self, query, *args, **kwargs):
        # allow session-setting queries used in get_conn() to succeed
        if isinstance(query, str) and query.strip().upper().startswith("SET SESSION MAX_EXECUTION_TIME"):
            return None

        # Simulate schema error on first real query, then succeed on retry
        calls = self._state.setdefault("calls", 0)
        if calls == 0:
            self._state["calls"] = 1
            raise Exception("1146: Table 'synth.fake_table' doesn't exist")
        # return a dummy successful result
        return None

    async def fetchone(self):
        return (1,)

    async def fetchall(self):
        return []

    async def close(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, shared_state):
        self._state = shared_state

    def cursor(self, *args, **kwargs):
        # Return an async context manager cursor
        return FakeCursor(self._state)

    def close(self):
        return None


class FakePool:
    def __init__(self, shared_state):
        self._state = shared_state

    async def acquire(self):
        return FakeConnection(self._state)

    def release(self, conn):
        return None


@pytest.mark.asyncio
async def test_db_auto_heal_retries_on_schema_error(monkeypatch):
    shared = {}

    async def fake_get_pool():
        return FakePool(shared)

    # Track whether ensure_* were called
    called = {"core": 0, "plugin": 0}

    async def fake_ensure_core_tables():
        called["core"] += 1

    async def fake_ensure_plugin_tables():
        called["plugin"] += 1

    monkeypatch.setattr(db_module, "get_pool", fake_get_pool)
    monkeypatch.setattr(db_module, "ensure_core_tables", fake_ensure_core_tables)
    monkeypatch.setattr(db_module, "ensure_plugin_tables", fake_ensure_plugin_tables)

    monkeypatch.setenv("DB_AUTO_HEAL", "1")

    # Should not raise because auto-heal will retry and succeed
    async with db_module.get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO fake_table (id) VALUES (1)")

    assert called["core"] == 1
    assert called["plugin"] == 1


@pytest.mark.asyncio
async def test_db_auto_heal_disabled_raises(monkeypatch):
    shared = {}

    async def fake_get_pool():
        return FakePool(shared)

    called = {"core": 0, "plugin": 0}

    async def fake_ensure_core_tables():
        called["core"] += 1

    async def fake_ensure_plugin_tables():
        called["plugin"] += 1

    monkeypatch.setattr(db_module, "get_pool", fake_get_pool)
    monkeypatch.setattr(db_module, "ensure_core_tables", fake_ensure_core_tables)
    monkeypatch.setattr(db_module, "ensure_plugin_tables", fake_ensure_plugin_tables)

    monkeypatch.setenv("DB_AUTO_HEAL", "0")

    with pytest.raises(Exception):
        async with db_module.get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute("INSERT INTO fake_table (id) VALUES (1)")

    # ensure_* should not have been invoked when auto-heal is disabled
    assert called["core"] == 0
    assert called["plugin"] == 0
