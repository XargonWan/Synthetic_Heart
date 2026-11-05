"""Config values pinned to their default when the config store is unreadable.

The registry used to treat "the database could not be read" exactly like "no
value stored": the definition was marked loaded with its code default and the
stored value never reached the process again. Measured live: a setting stored in
the config table was absent from every prompt because the plugin registered the
key after the boot sweep and the first read happened inside the running event
loop, where the synchronous path cannot block for a query.

These tests pin the replacement behaviour: the default is used, the key is
remembered as deferred, and the next successful sweep applies the stored value.
"""

import asyncio

from core import config_manager
from core.config_manager import ConfigDefinition, ConfigRegistry


def _def(key, default="code-default", loaded=False):
    """A minimal ConfigDefinition with no DB or environment dependency."""
    return ConfigDefinition(
        key=key,
        label=key,
        description=key,
        default=default,
        value_type=str,
        group="test",
        component="test",
        loaded=loaded,
    )


def _fake_db(monkeypatch, rows, fail=False):
    """Replace the DB access the registry performs with an in-memory one."""
    import core.db as db_m

    class _FakeCursor:
        async def execute(self, *a, **k):
            return None

        async def fetchall(self):
            return list(rows)

        async def fetchone(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeCtx:
        async def __aenter__(self):
            if fail:
                raise RuntimeError("db down")
            return _FakeConn()

        async def __aexit__(self, *a):
            return False

    async def _ensure_tables(*a, **k):
        return None

    monkeypatch.setattr(db_m, "get_conn_ctx", _FakeCtx)
    monkeypatch.setattr(db_m, "ensure_core_tables", _ensure_tables)


def test_read_inside_event_loop_defers_then_sweep_recovers(monkeypatch):
    monkeypatch.setattr(config_manager, "_last_full_load_monotonic", None)
    _fake_db(monkeypatch, [("DECLARED", "stored-value")])
    reg = ConfigRegistry()
    reg._definitions = {"DECLARED": _def("DECLARED"), "ABSENT": _def("ABSENT")}
    # The subject here is the sweep, not the retry trigger (tested separately);
    # without this the pending task outlives the test's event loop.
    reg._schedule_deferred_retry = lambda: None  # type: ignore[method-assign]

    async def read_inside_loop():
        return reg.get_value("DECLARED", "code-default")

    # The sync path cannot query inside a running loop, so the default is used.
    assert asyncio.run(read_inside_loop()) == "code-default"
    assert "DECLARED" in reg._deferred_keys
    # Marked loaded, which is exactly what used to make the loss permanent.
    assert reg._definitions["DECLARED"].loaded is True

    asyncio.run(reg.load_all_from_db(force=True))

    assert reg.get_value("DECLARED", "code-default") == "stored-value"
    assert "DECLARED" not in reg._deferred_keys
    # A key with no stored row keeps its default and is not reported as deferred.
    assert reg.get_value("ABSENT", "code-default") == "code-default"
    assert "ABSENT" not in reg._deferred_keys


def test_failed_read_defers_instead_of_pinning(monkeypatch):
    monkeypatch.setattr(config_manager, "_last_full_load_monotonic", None)
    _fake_db(monkeypatch, [], fail=True)
    reg = ConfigRegistry()
    reg._definitions = {"BROKEN": _def("BROKEN")}

    # A store failure must not escape into the caller, and must not be mistaken
    # for "this key has no stored value".
    assert reg.get_value("BROKEN", "code-default") == "code-default"
    assert "BROKEN" in reg._deferred_keys
    assert reg._deferred_keys["BROKEN"]


def test_failed_sweep_keeps_the_key_deferred(monkeypatch):
    monkeypatch.setattr(config_manager, "_last_full_load_monotonic", None)
    _fake_db(monkeypatch, [], fail=True)
    reg = ConfigRegistry()
    reg._definitions = {"BROKEN": _def("BROKEN")}
    assert reg.get_value("BROKEN", "code-default") == "code-default"

    asyncio.run(reg.load_all_from_db(force=True))

    assert "BROKEN" in reg._deferred_keys


def test_sweep_clears_a_deferred_key_that_has_no_stored_row(monkeypatch):
    """A deferred key with no stored row is not a read failure.

    It is on its default because it was never persisted. Leaving it in the
    deferred set made the end-of-sweep warning call it "pinned because the store
    could not be read" for the rest of the process: live, that warning grew to 133
    keys by mid-boot and every one of them was behaving correctly.
    """
    monkeypatch.setattr(config_manager, "_last_full_load_monotonic", None)
    _fake_db(monkeypatch, [("DECLARED", "stored-value")])
    reg = ConfigRegistry()
    reg._definitions = {"DECLARED": _def("DECLARED"), "UNSTORED": _def("UNSTORED")}
    reg._schedule_deferred_retry = lambda: None  # type: ignore[method-assign]

    async def read_inside_loop():
        return reg.get_value("UNSTORED", "code-default")

    assert asyncio.run(read_inside_loop()) == "code-default"
    assert "UNSTORED" in reg._deferred_keys

    asyncio.run(reg.load_all_from_db(force=True))

    assert reg.get_value("UNSTORED", "code-default") == "code-default"
    assert "UNSTORED" not in reg._deferred_keys
    # The key that does have a stored row still recovers its value.
    assert reg.get_value("DECLARED", "code-default") == "stored-value"


def test_failed_sweep_keeps_an_unstored_key_deferred(monkeypatch):
    """When the read itself failed, a default proves nothing, so nothing is cleared."""
    monkeypatch.setattr(config_manager, "_last_full_load_monotonic", None)
    _fake_db(monkeypatch, [], fail=True)
    reg = ConfigRegistry()
    reg._definitions = {"UNSTORED": _def("UNSTORED")}
    reg._schedule_deferred_retry = lambda: None  # type: ignore[method-assign]

    async def read_inside_loop():
        return reg.get_value("UNSTORED", "code-default")

    assert asyncio.run(read_inside_loop()) == "code-default"

    asyncio.run(reg.load_all_from_db(force=True))

    assert "UNSTORED" in reg._deferred_keys


def test_get_persisted_value_falls_back_to_default(monkeypatch):
    _fake_db(monkeypatch, [], fail=True)
    reg = ConfigRegistry()
    reg._definitions = {"BOOTSTRAP": _def("BOOTSTRAP")}

    assert asyncio.run(reg.get_persisted_value("BOOTSTRAP", "fallback")) == "fallback"
    assert "BOOTSTRAP" in reg._deferred_keys


def test_deferred_read_schedules_a_retry(monkeypatch):
    """A deferred key must not wait for a sweep that may never come.

    The boot sweeps can finish before the plugin that owns the key registers it,
    so the pin happens after the last sweep of the run: without the retry the
    default would stand until the next restart.
    """
    monkeypatch.setattr(config_manager, "_last_full_load_monotonic", None)
    monkeypatch.setattr(config_manager, "_DEFERRED_RETRY_DELAY_SEC", 0.01)
    _fake_db(monkeypatch, [("DECLARED", "stored-value")])
    reg = ConfigRegistry()
    reg._definitions = {"DECLARED": _def("DECLARED")}

    async def scenario():
        assert reg.get_value("DECLARED", "code-default") == "code-default"
        assert reg._deferred_retry_task is not None
        await asyncio.sleep(0.05)
        assert reg.get_value("DECLARED", "code-default") == "stored-value"
        assert "DECLARED" not in reg._deferred_keys

    asyncio.run(scenario())


def test_mariadb_fallback_reports_the_inputs_that_chose_it(monkeypatch):
    from core import db as db_m

    monkeypatch.setenv("SYNTH_PRIMARY_DB", "memory")
    context = db_m._mariadb_fallback_context()

    assert "SYNTH_PRIMARY_DB='memory'" in context
    assert "resolved_target='memory'" in context
    # The caller chain has to name this file, so a log line points at the site.
    assert "test_config_store_deferral.py" in db_m._describe_caller()
