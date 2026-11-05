"""Tests for the ``is_test`` isolation marker on the LLM failure store.

Review item 10: the ``fake`` interface and ``test reason`` entries pollute
runtime failure statistics. These tests lock down the structural marker and
the read-side exclusion so test entries never leak into the health dashboard
or failure summaries.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core import llm_failure_log as failure_log


def test_build_failure_entry_tags_fake_interface() -> None:
    entry = failure_log.build_failure_entry(
        reason="boom", stage="llm", interface_path="fake"
    )
    assert entry["is_test"] is True


def test_build_failure_entry_tags_fake_interface_case_insensitive() -> None:
    entry = failure_log.build_failure_entry(
        reason="boom", stage="llm", interface_path="FAKE"
    )
    assert entry["is_test"] is True


def test_build_failure_entry_tags_test_reason() -> None:
    entry = failure_log.build_failure_entry(reason="test reason", stage="llm")
    assert entry["is_test"] is True


def test_build_failure_entry_normal_entry_not_tagged(monkeypatch) -> None:
    """A normal path is not tagged by the PATH heuristics.

    Outside a test process only ``fake*`` and ``reason='test reason'`` are test
    data; a real chat path is not. (Under pytest every write is test data, which
    is the point of the marker - see the two tests below.)
    """
    monkeypatch.setattr(failure_log, "_is_test_process", lambda: False)
    entry = failure_log.build_failure_entry(
        reason="timeout", stage="delivery", interface_path="telegram_bot/123"
    )
    assert entry["is_test"] is False


def test_build_failure_entry_explicit_is_test_wins() -> None:
    entry = failure_log.build_failure_entry(
        reason="timeout",
        stage="delivery",
        interface_path="telegram_bot/123",
        is_test=True,
    )
    assert entry["is_test"] is True


def test_normalize_entry_preserves_is_test() -> None:
    normalized = failure_log._normalize_entry_for_storage({"is_test": True})
    assert normalized["is_test"] is True
    normalized_false = failure_log._normalize_entry_for_storage({"is_test": False})
    assert normalized_false["is_test"] is False
    normalized_absent = failure_log._normalize_entry_for_storage({})
    assert normalized_absent["is_test"] is False


@pytest.mark.asyncio
async def test_in_memory_list_excludes_test_entries(monkeypatch) -> None:
    failure_log._in_memory_failure_entries[:] = [
        {
            "id": -1,
            "failure_code": "llm_failure",
            "stage": "llm",
            "reason": "test reason",
            "is_test": True,
        },
        {
            "id": -2,
            "failure_code": "timeout",
            "stage": "llm",
            "reason": "real timeout",
            "is_test": False,
        },
    ]
    monkeypatch.setattr(failure_log, "_include_test_failures_enabled", lambda: False)

    entries = await failure_log._list_in_memory_failure_entries(
        search="", failure_code="", stage=""
    )

    assert [e["id"] for e in entries] == [-2]


@pytest.mark.asyncio
async def test_in_memory_list_includes_test_entries_when_enabled(monkeypatch) -> None:
    failure_log._in_memory_failure_entries[:] = [
        {
            "id": -1,
            "failure_code": "llm_failure",
            "stage": "llm",
            "reason": "test reason",
            "is_test": True,
        },
        {
            "id": -2,
            "failure_code": "timeout",
            "stage": "llm",
            "reason": "real timeout",
            "is_test": False,
        },
    ]
    monkeypatch.setattr(failure_log, "_include_test_failures_enabled", lambda: True)

    entries = await failure_log._list_in_memory_failure_entries(
        search="", failure_code="", stage=""
    )

    assert sorted(e["id"] for e in entries) == [-2, -1]


@pytest.mark.asyncio
async def test_db_list_filters_is_test_column(monkeypatch) -> None:
    captured: list[str] = []

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            captured.append(sql)

        async def fetchall(self):
            return []

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def cursor(self):
            return FakeCursor()

    import core.db as db_module

    monkeypatch.setattr(db_module, "get_conn_ctx", lambda: FakeConn())
    monkeypatch.setattr(
        failure_log, "ensure_failure_log_table", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(failure_log, "_include_test_failures_enabled", lambda: False)

    await failure_log._list_db_failure_entries(
        search="", failure_code="", stage="", sort="desc"
    )

    assert any("is_test = 0" in sql for sql in captured)


@pytest.mark.asyncio
async def test_db_list_omits_is_test_filter_when_enabled(monkeypatch) -> None:
    captured: list[str] = []

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            captured.append(sql)

        async def fetchall(self):
            return []

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def cursor(self):
            return FakeCursor()

    import core.db as db_module

    monkeypatch.setattr(db_module, "get_conn_ctx", lambda: FakeConn())
    monkeypatch.setattr(
        failure_log, "ensure_failure_log_table", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(failure_log, "_include_test_failures_enabled", lambda: True)

    await failure_log._list_db_failure_entries(
        search="", failure_code="", stage="", sort="desc"
    )

    assert not any("is_test = 0" in sql for sql in captured)


def test_is_test_process_latches_when_a_test_is_seen(monkeypatch) -> None:
    """Once a test is seen, the whole process is a test process.

    pytest sets ``PYTEST_CURRENT_TEST`` for the duration of every test and the
    suite drives the real message chain, so its writes must not land in a live
    store as runtime failures. The latch covers a background task that a test
    started and that writes after that test's teardown, when the variable is
    already gone.
    """
    monkeypatch.setattr(failure_log, "_TEST_PROCESS", False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert failure_log._is_test_process() is False

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::test_y (call)")
    assert failure_log._is_test_process() is True

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert failure_log._is_test_process() is True


def test_build_failure_entry_marks_a_test_process(monkeypatch) -> None:
    """A test write is test data even for a path that looks like a real chat."""
    monkeypatch.setattr(failure_log, "_is_test_process", lambda: True)
    entry = failure_log.build_failure_entry(
        reason="timeout", stage="delivery", interface_path="telegram_bot/5208932647"
    )
    assert entry["is_test"] is True


def test_test_process_check_applies_to_every_write(monkeypatch) -> None:
    """A write inside a test run is test data, whatever the caller passes.

    ``is_test=False`` is the default, so the check cannot tell "the caller wants a
    runtime-shaped row" from "the caller did not say" - and the path heuristics
    already overrode False for a ``fake`` path. A test that needs a runtime row
    clears the marker instead, as ``test_build_failure_entry_normal_entry_not_tagged``
    does.
    """
    monkeypatch.setattr(failure_log, "_is_test_process", lambda: True)
    entry = failure_log.build_failure_entry(
        reason="timeout", stage="delivery", interface_path="telegram_bot/5208932647"
    )
    assert entry["is_test"] is True


@pytest.mark.asyncio
async def test_historic_test_rows_are_flagged_not_deleted(monkeypatch) -> None:
    """The backfill flags fixture rows and never removes anything.

    ``is_test`` was added after the fact and never backfilled, so the rows the
    suite wrote before it still read as runtime failures: the recovery loop finds
    them and spends a turn on a chat that does not exist.
    """
    from core import migrations as migrations_mod

    executed: list[tuple[str, object]] = []

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            executed.append((sql, params))

        async def fetchone(self):
            return (1,)

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def cursor(self):
            return FakeCursor()

        async def commit(self):
            return None

    import core.db as db_module

    monkeypatch.setattr(db_module, "get_conn_ctx", lambda: FakeConn())
    monkeypatch.setattr(db_module, "_get_db_type", lambda: "postgres")

    await migrations_mod._flag_historic_test_failure_rows()

    updates = [
        (sql, params) for sql, params in executed if sql.strip().startswith("UPDATE")
    ]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "SET is_test = 1" in sql
    assert "DELETE" not in sql.upper()
    # Only rows still unflagged are touched, so the pass is idempotent ...
    assert "is_test = 0" in sql
    # ... and the fixture paths are passed as parameters, never interpolated.
    assert "fake%' " in sql or "LIKE 'fake%'" in sql
    assert tuple(params) == migrations_mod._HISTORIC_TEST_FAILURE_PATHS


@pytest.mark.asyncio
async def test_historic_flag_skips_a_store_without_the_column(monkeypatch) -> None:
    from core import migrations as migrations_mod

    executed: list[str] = []

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            executed.append(sql)

        async def fetchone(self):
            # No such table.
            return (None,)

        async def fetchall(self):
            return []

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def cursor(self):
            return FakeCursor()

    import core.db as db_module

    monkeypatch.setattr(db_module, "get_conn_ctx", lambda: FakeConn())
    monkeypatch.setattr(db_module, "_get_db_type", lambda: "postgres")

    await migrations_mod._flag_historic_test_failure_rows()

    assert not any(sql.strip().startswith("UPDATE") for sql in executed)
