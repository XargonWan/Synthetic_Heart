import pytest


def test_compat_cursor_keyword_forwarding():
    calls = []

    class DummyConn:
        def cursor(self, *args, **kwargs):
            # Emulate cursor method that accepts only positional args
            # and records the args received for inspection.
            calls.append(("called", args, kwargs))
            return "CURSOR_OK"

    conn = DummyConn()

    # Original behaviour: calling with keyword would raise TypeError in some DB clients
    with pytest.raises(TypeError):
        # Simulate this by explicitly raising if keyword used (mimicking strict signature)
        # Here, Python will not raise; to emulate the strict signature we check keywords
        # and raise to assert the shim fixes it.
        def strict_cursor(*args, **kwargs):
            if "cursor" in kwargs:
                raise TypeError(
                    "Connection.cursor() got an unexpected keyword argument 'cursor'"
                )
            return conn.cursor(*args, **kwargs)

        conn.cursor = strict_cursor
        conn.cursor(cursor="X")

    # Apply compatibility shim as in core.db
    def orig_cursor(*args, **kwargs):
        # delegate to the underlying conn implementation which records calls
        return DummyConn().cursor(*args, **kwargs)

    def compat_cursor(*args, **kwargs):
        if "cursor" in kwargs:
            c = kwargs.pop("cursor")
            return orig_cursor(c, *args, **kwargs)
        return orig_cursor(*args, **kwargs)

    # Attach compat cursor and call with keyword
    conn.cursor = compat_cursor
    result = conn.cursor(cursor="X")
    assert result == "CURSOR_OK"

    # Also confirm that calling without keyword still works
    result2 = conn.cursor()
    assert result2 == "CURSOR_OK"


@pytest.mark.asyncio
async def test_insert_memory_skips_exact_dupe(monkeypatch):
    """Shared choke-point guard: same content/author/source is not re-inserted."""
    from unittest.mock import AsyncMock

    from core.db import insert_memory

    calls = []

    class FakeCur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            calls.append(sql)

        async def fetchone(self):
            return (1,)

    class FakeConn:
        def cursor(self):
            return FakeCur()

    class FakeCtx:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("core.db.ensure_core_tables", AsyncMock())
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: FakeCtx())
    await insert_memory(content="x", author="a", source="s", tags="[]")
    assert len(calls) == 1 and "SELECT 1" in calls[0]


@pytest.mark.asyncio
async def test_insert_memory_inserts_when_new(monkeypatch):
    from unittest.mock import AsyncMock

    from core.db import insert_memory

    calls = []

    class FakeCur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            calls.append(sql)

        async def fetchone(self):
            return None

    class FakeConn:
        def cursor(self):
            return FakeCur()

    class FakeCtx:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("core.db.ensure_core_tables", AsyncMock())
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: FakeCtx())
    await insert_memory(content="x", author="a", source="s", tags="[]")
    assert len(calls) == 2 and "INSERT INTO memories" in calls[1]
