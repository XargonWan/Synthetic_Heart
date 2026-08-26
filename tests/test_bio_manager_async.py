"""Regression tests for bio_manager async/loop-safety.

The ``bio_full_request`` action is executed from the Agent Lane on the
event-loop thread (``agent_tool_executor`` -> ``run_action`` -> sync
``execute_action``). The old sync ``execute_action`` went through ``_run()``
which called ``run_coroutine_threadsafe(coro, loop).result(timeout=30)`` on the
very loop it was blocking -> deadlock -> 30s ``TimeoutError`` ("Error in _run: ")
repeated every 30s. See docs/database_connection_management.rst.

The fix converts ``execute_action`` to async and routes reads through the
loop-safe async DB helpers. These tests prove the action path never touches the
``_run`` bridge (i.e. it can run inside a live event loop without deadlocking).
"""

import asyncio
import inspect

import pytest

import plugins.bio_manager as bio_manager  # noqa: E402 (package shim rebinds to the module)

BioPlugin = bio_manager.BioPlugin


class DummyCursor:
    def __init__(self, row=None):
        self.row = row
        self.executed = []

    async def execute(self, query, params=None):
        self.executed.append((query, params))

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return [self.row] if self.row else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummyConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *args, **kwargs):
        return self._cursor

    async def commit(self):
        return None


class DummyCtx:
    def __init__(self, row=None):
        self._conn = DummyConn(DummyCursor(row))
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        self.entered = False
        return False


def _install_fake_db(monkeypatch, row=None):
    """Patch get_conn_ctx so queries return a canned bio row."""

    ctx = DummyCtx(row)

    def fake_get_conn_ctx():
        return ctx

    monkeypatch.setattr(bio_manager, "get_conn_ctx", fake_get_conn_ctx)
    return ctx


def _trap_run_bridge(monkeypatch):
    """Any use of the sync _run bridge fails the test loudly."""

    def boom(*args, **kwargs):
        raise AssertionError("async action path must not use the _run bridge")

    monkeypatch.setattr(bio_manager, "_run", boom)


def _bio_row(user_id: str = "42") -> dict:
    return {
        "id": user_id,
        "information": "A grillo.",
        "known_as": '["grillo"]',
        "likes": "[]",
        "not_likes": "[]",
        "past_events": "[]",
        "feelings": "[]",
        "contacts": "{}",
        "social_accounts": "[]",
        "privacy": "default",
        "created_at": "2026-01-01T00:00:00",
        "last_accessed": "",
        "last_update": "",
        "update_count": 0,
    }


def test_execute_action_is_async():
    """execute_action must be a coroutine so the loop thread awaits it."""
    assert inspect.iscoroutinefunction(BioPlugin.execute_action)


@pytest.mark.asyncio
async def test_async_full_request_never_uses_run_bridge(monkeypatch):
    """bio_full_request resolves targets via async helpers, not _run()."""
    _install_fake_db(monkeypatch, _bio_row())
    _trap_run_bridge(monkeypatch)

    plugin = BioPlugin()
    plugin._participants = [{"id": "42", "username": "grillo", "usertag": "grillo"}]

    result = await plugin.execute_action(
        {"type": "bio_full_request", "payload": {"targets": "grillo"}},
        {},
        None,
        None,
    )
    assert result["success"] is True
    assert result["data"][0]["id"] == "42"
    assert result["data"][0]["information"] == "A grillo."


@pytest.mark.asyncio
async def test_async_full_request_numeric_target(monkeypatch):
    """A bare numeric target resolves directly without the participant loop."""
    _install_fake_db(monkeypatch, _bio_row("123"))
    _trap_run_bridge(monkeypatch)

    plugin = BioPlugin()
    plugin._participants = []

    result = await plugin.execute_action(
        {"type": "bio_full_request", "payload": {"targets": "123"}},
        {},
        None,
        None,
    )
    assert result["success"] is True
    assert result["data"][0]["id"] == "123"


@pytest.mark.asyncio
async def test_async_bio_update_never_uses_run_bridge(monkeypatch):
    """bio_update goes through _update_bio_fields_async, not the sync update."""
    _install_fake_db(monkeypatch, _bio_row())
    _trap_run_bridge(monkeypatch)
    monkeypatch.setattr(bio_manager, "_get_db_type", lambda: "mariadb")
    # Table is created at startup in production; skip the DDL path in the test.
    monkeypatch.setattr(bio_manager, "_table_initialized", True)

    plugin = BioPlugin()
    plugin._participants = [{"id": "42", "username": "grillo", "usertag": "grillo"}]

    result = await plugin.execute_action(
        {
            "type": "bio_update",
            "payload": {"target": "grillo", "fields": {"likes": ["pizza"]}},
        },
        {},
        None,
        None,
    )
    assert result["success"] is True
    assert result["updated_fields"] == ["likes"]


def test_run_bridge_deadlock_path_runs_on_worker_thread(monkeypatch):
    """_run() called from a running loop thread must not deadlock.

    This simulates the exact failure mode: a coroutine scheduled on the loop we
    are about to block on. The loop-thread branch must delegate to a worker
    thread instead of run_coroutine_threadsafe().result().
    """

    async def probe():
        result = getattr(bio_manager, "_run")(_noop_coro())
        assert result == "done"

    async def _noop_coro():
        return "done"

    asyncio.run(probe())
