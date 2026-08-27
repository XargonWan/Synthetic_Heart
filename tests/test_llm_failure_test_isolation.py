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


def test_build_failure_entry_normal_entry_not_tagged() -> None:
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
