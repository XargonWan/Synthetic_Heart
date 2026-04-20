import pytest
from plugins.grillo.grillo_impl import GrilloPlugin


@pytest.mark.asyncio
async def test_create_action_exec_creates_table_if_missing(monkeypatch):
    # Simulate get_conn_ctx raising 1146 on first call, then succeeding
    call = {"n": 0}

    class FakeCursor:
        def __init__(self):
            self.lastrowid = 123

        async def execute(self, *args, **kwargs):
            return None

        async def fetchall(self):
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        async def commit(self):
            return None

        async def cursor(self):
            return FakeCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_get_conn_ctx():
        if call["n"] == 0:
            call["n"] += 1
            raise Exception(
                "(1146, \"Table 'synth.grillo_action_execs' doesn't exist\")"
            )
        return FakeConn()

    monkeypatch.setattr("core.db.get_conn_ctx", fake_get_conn_ctx)

    # Spy on ensure
    ensured = {"called": False}

    async def fake_ensure():
        ensured["called"] = True
        return True

    monkeypatch.setattr(
        GrilloPlugin,
        "_ensure_action_execs_table",
        classmethod(lambda cls: fake_ensure()),
    )

    res = await GrilloPlugin.create_action_exec(
        activity_log_id=1, action_index=0, action_type="test", payload={"a": 1}
    )
    assert ensured["called"] is True
    assert res == 123


@pytest.mark.asyncio
async def test_fetch_action_execs_creates_table_if_missing(monkeypatch):
    call = {"n": 0}

    class FakeCursor:
        def __init__(self):
            pass

        async def execute(self, *args, **kwargs):
            return None

        async def fetchall(self):
            return [(1, 1, 0, "schedule", "{}", "pending", None, None, None)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        async def commit(self):
            return None

        async def cursor(self):
            return FakeCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_get_conn_ctx():
        if call["n"] == 0:
            call["n"] += 1
            raise Exception(
                "(1146, \"Table 'synth.grillo_action_execs' doesn't exist\")"
            )
        return FakeConn()

    monkeypatch.setattr("core.db.get_conn_ctx", fake_get_conn_ctx)

    ensured = {"called": False}

    async def fake_ensure():
        ensured["called"] = True
        return True

    monkeypatch.setattr(
        GrilloPlugin,
        "_ensure_action_execs_table",
        classmethod(lambda cls: fake_ensure()),
    )

    res = await GrilloPlugin.fetch_action_execs([1])
    assert ensured["called"] is True
    assert isinstance(res, dict)
    assert 1 in res
    assert res[1][0]["action_type"] == "schedule"
