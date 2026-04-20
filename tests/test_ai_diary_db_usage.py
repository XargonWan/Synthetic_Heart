import asyncio
import pytest

import plugins.ai_diary as ai_diary


class DummyCursor:
    def __init__(self):
        self.executed = []
        self.lastrowid = 123

    async def execute(self, q, params=None):
        self.executed.append((q, params))

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummyConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class DummyCtx:
    def __init__(self):
        self.conn = DummyConn(DummyCursor())
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        self.entered = False
        return False


@pytest.mark.asyncio
async def test_add_diary_entry_async_uses_get_db(monkeypatch):
    monkeypatch.setenv("SYNTH_TESTING", "1")
    # Ensure plugin is enabled otherwise it may attempt table creation
    ai_diary.PLUGIN_ENABLED = True

    dummy = DummyCtx()

    def fake_get_db():
        return dummy

    monkeypatch.setattr(ai_diary, "get_db", fake_get_db)

    await ai_diary.add_diary_entry_async(content="test entry", personal_thought="p")

    assert dummy.entered is False  # context should have exited
    assert isinstance(dummy.conn._cursor, DummyCursor)
    assert dummy.conn._cursor.executed, "Expected DB execute to have been called"


def test_run_uses_run_coroutine_threadsafe(monkeypatch):
    called = {"used": False}

    def fake_run_coroutine_threadsafe(coro, loop):
        class R:
            def result(self):
                return "ok"

        called["used"] = True
        return R()

    monkeypatch.setattr(
        asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe
    )

    # Simulate a running loop
    class DummyLoop:
        def is_running(self):
            return True

    monkeypatch.setattr(asyncio, "get_event_loop", lambda: DummyLoop())

    res = ai_diary._run(asyncio.sleep(0))
    assert called["used"] is True
    assert res == "ok"
