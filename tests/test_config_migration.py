import pytest

import core.db as db_module
from core.config import set_base_cortex


class _FakeConfigCursor:
    def __init__(self, shared_state):
        self._state = shared_state
        self._row = None

    async def execute(self, query, params=(), **kwargs):
        normalized = " ".join(query.split()).upper()
        if normalized.startswith("SELECT 1 FROM CONFIG"):
            key = params[0]
            self._row = (1,) if key in self._state else None
            return None
        if normalized.startswith("REPLACE INTO CONFIG"):
            key, value = params
            self._state[key] = value
            self._row = None
            return None
        if normalized.startswith("SELECT VALUE FROM CONFIG"):
            key = params[0]
            value = self._state.get(key)
            self._row = (value,) if value is not None else None
            return None
        raise AssertionError(f"Unexpected query: {query}")

    async def fetchone(self):
        return self._row

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConfigConn:
    def __init__(self, shared_state):
        self._state = shared_state

    def cursor(self, *args, **kwargs):
        return _FakeConfigCursor(self._state)

    async def commit(self):
        return None


class _FakeConfigCtx:
    def __init__(self, shared_state):
        self._state = shared_state

    async def __aenter__(self):
        return _FakeConfigConn(self._state)

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_set_base_cortex_persists_to_config_table(monkeypatch):
    shared = {}

    async def fake_ensure_core_tables():
        return None

    monkeypatch.setattr(db_module, "get_conn_ctx", lambda: _FakeConfigCtx(shared))
    monkeypatch.setattr(db_module, "ensure_core_tables", fake_ensure_core_tables)

    # Persist a new base cortex via public API
    await set_base_cortex("selenium_gemini")

    # Ensure config table contains the entry
    async with db_module.get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT value FROM config WHERE config_key = %s", ("BASE_CORTEX",)
            )
            row = await cur.fetchone()
            assert row and row[0] == "selenium_gemini"


@pytest.mark.asyncio
async def test_set_base_cortex_auto_heals_missing_config_table(monkeypatch):
    """If the config table is missing (1146), persistence should trigger ensure_core_tables and succeed."""
    import core.db as db_module

    # Reuse fake pool/cursor pattern from test_db_auto_heal
    shared = {}

    class FakeCursor:
        def __init__(self, shared_state):
            self._state = shared_state

        async def execute(self, query, *args, **kwargs):
            # allow session-setting queries used in get_conn() to succeed
            if isinstance(query, str) and query.strip().upper().startswith(
                "SET SESSION MAX_EXECUTION_TIME"
            ):
                return None

            calls = self._state.setdefault("calls", 0)
            if calls == 0:
                self._state["calls"] = 1
                # Simulate missing 'config' table on first attempt
                raise Exception("1146: Table 'synth.config' doesn't exist")
            return None

        async def fetchone(self):
            return (1,)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        def __init__(self, shared_state):
            self._state = shared_state

        def cursor(self, *args, **kwargs):
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

    async def fake_get_pool():
        return FakePool(shared)

    called = {"core": 0}

    async def fake_ensure_core_tables():
        called["core"] += 1

    monkeypatch.setattr(db_module, "get_pool", fake_get_pool)
    monkeypatch.setattr(db_module, "ensure_core_tables", fake_ensure_core_tables)

    # Should not raise — auto-heal will call ensure_core_tables() and retry
    await set_base_cortex("manual")
    assert called["core"] == 1
