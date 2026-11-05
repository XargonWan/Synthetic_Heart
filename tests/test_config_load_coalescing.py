"""Focused tests for the ``load_all_from_db`` coalescing guard.

The full config sweep is called from many startup paths (core_initializer twice,
then each interface's reload handler), so without a guard one boot re-runs and
re-logs it ~34 times. These tests pin the guard: redundant re-scans within the
window are skipped, while genuine loads (a pending definition, ``force=True``,
or a failed read) always run.
"""

import asyncio

import pytest

from core import config_manager
from core.config_manager import ConfigDefinition, ConfigRegistry


def _def(key, loaded=True):
    """A minimal already-or-not-loaded ConfigDefinition (no DB/env dependencies)."""
    return ConfigDefinition(
        key=key,
        label=key,
        description=key,
        default=None,
        value_type=str,
        group="test",
        component="test",
        loaded=loaded,
    )


def _rows_ctx_factory(rows, counter):
    """Build a ``core.db.get_conn_ctx`` replacement that counts executed sweeps.

    Each successful ``async with get_conn_ctx()`` increments ``counter["n"]`` and
    returns a fake connection whose cursor yields ``rows``.
    """

    import core.db as db_m

    class _FakeCursor:
        def __init__(self, _rows):
            self._rows = _rows

        async def execute(self, *a, **k):
            return None

        async def fetchall(self):
            return list(self._rows)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeConn:
        def __init__(self, _rows):
            self._rows = _rows

        def cursor(self):
            return _FakeCursor(self._rows)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeCtx:
        async def __aenter__(self):
            counter["n"] += 1
            return _FakeConn(rows)

        async def __aexit__(self, *a):
            return False

    async def _ensure_tables(*a, **k):
        return None

    db_m.get_conn_ctx = _FakeCtx  # type: ignore[invalid-assignment]
    db_m.ensure_core_tables = _ensure_tables  # type: ignore[invalid-assignment]


@pytest.fixture
def reset_guard():
    """Reset the module-level coalesce timestamp so tests are independent."""
    config_manager._last_full_load_monotonic = None
    yield
    config_manager._last_full_load_monotonic = None


def run(reg):
    asyncio.run(reg.load_all_from_db())


def run_forced(reg):
    asyncio.run(reg.load_all_from_db(force=True))


def _registry():
    reg = ConfigRegistry()
    reg._definitions = {
        "A": _def("A", loaded=True),
        "B": _def("B", loaded=True),
    }
    return reg


def test_first_call_always_run(monkeypatch, reset_guard):
    counter = {"n": 0}
    _rows_ctx_factory([("A", "1")], counter)
    reg = _registry()
    run(reg)
    assert counter["n"] == 1


def test_redundant_rescan_within_window_is_skipped(monkeypatch, reset_guard):
    counter = {"n": 0}
    _rows_ctx_factory([("A", "1")], counter)
    reg = _registry()
    run(reg)
    assert counter["n"] == 1
    # Immediate re-call, nothing pending -> coalesced (no extra run, no re-log).
    run(reg)
    assert counter["n"] == 1


def test_force_always_runs(monkeypatch, reset_guard):
    counter = {"n": 0}
    _rows_ctx_factory([("A", "1")], counter)
    reg = _registry()
    run(reg)
    assert counter["n"] == 1
    run_forced(reg)
    assert counter["n"] == 2


def test_pending_definition_forces_run_within_window(monkeypatch, reset_guard):
    counter = {"n": 0}
    _rows_ctx_factory([("A", "1")], counter)
    reg = _registry()
    run(reg)
    assert counter["n"] == 1
    # A definition is now waiting to be loaded -> the re-scan must NOT be skipped.
    reg._definitions["C"] = _def("C", loaded=False)
    run(reg)
    assert counter["n"] == 2


def test_failed_read_does_not_coalesce_next_call(monkeypatch, reset_guard):
    counter = {"n": 0}

    import core.db as db_m

    class _FailCtx:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *a):
            return False

    async def _failing_ensure(*a, **k):
        return None

    db_m.get_conn_ctx = _FailCtx  # type: ignore[invalid-assignment]
    db_m.ensure_core_tables = _failing_ensure  # type: ignore[invalid-assignment]
    # First call hits the failing read; note that the real load_all_from_db wraps
    # the DB read in try/except, so the error surfaces as a warning and the loop
    # still runs. The timestamp must stay unset, so a retry is never swallowed.
    run(_registry())
    assert config_manager._last_full_load_monotonic is None
    # Even with a successful db available next time, because the previous read
    # failed we must still run.
    _rows_ctx_factory([("A", "1")], counter)
    run(_registry())
    assert counter["n"] == 1
