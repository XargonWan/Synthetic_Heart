"""Regression tests for the memory write path (memory compaction plan, F1, F2, F4, F7).

F1  `insert_memory` must hand asyncpg a datetime, never a string: the Postgres backend forwards
    parameters unchanged, so a string is rejected client-side and the row is never written. The
    failure used to be swallowed, which is how summaries vanished while their diary days were deleted.
F2  the memory row must be written before the source rows are archived and deleted.
F4  a cluster the model declined must not be persisted.
F7  `detailed` may arrive as a JSON array and must not reach the database as a Python repr.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from core import db as core_db
from core.db import _coerce_memory_timestamp, insert_memory

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "grillo" / "grillo_compactor" / "grillo_compactor.py"


# --------------------------------------------------------------------------- F1: the timestamp type

def test_iso_string_becomes_a_timezone_aware_datetime():
    got = _coerce_memory_timestamp("2026-08-08 14:30:00")
    assert isinstance(got, datetime), "asyncpg rejects a str for TIMESTAMPTZ; this must be a datetime"
    assert got.tzinfo is not None
    assert (got.year, got.month, got.day, got.hour) == (2026, 8, 8, 14)


def test_common_string_shapes_are_accepted():
    for text in ("2026-08-08", "2026-08-08 14:30", "2026-08-08T14:30:00Z", "2026/08/08 14:30:00", "08.08.2026 14:30:00"):
        got = _coerce_memory_timestamp(text)
        assert isinstance(got, datetime) and got.tzinfo is not None, text


def test_datetime_and_none_and_epoch():
    aware = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    assert _coerce_memory_timestamp(aware) == aware
    naive = datetime(2026, 8, 8, 12, 0)
    assert _coerce_memory_timestamp(naive).tzinfo is timezone.utc
    assert _coerce_memory_timestamp(None).tzinfo is timezone.utc
    assert isinstance(_coerce_memory_timestamp(1754654400), datetime)


def test_unreadable_values_raise_instead_of_defaulting_to_now():
    with pytest.raises(ValueError):
        _coerce_memory_timestamp("last tuesday")
    with pytest.raises(TypeError):
        _coerce_memory_timestamp(object())


class _FakeCursor:
    def __init__(self, sink, boom=False):
        self.sink, self.boom = sink, boom

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        if self.boom:
            raise RuntimeError("simulated asyncpg type rejection")
        self.sink.append((sql, params))


class _FakeConn:
    def __init__(self, sink, boom=False):
        self.sink, self.boom = sink, boom
        self.used = 0

    def cursor(self):
        self.used += 1
        return _FakeCursor(self.sink, boom=self.boom)


@pytest.fixture(autouse=True)
def _no_schema_touch(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(core_db, "ensure_core_tables", _noop)


async def test_insert_memory_sends_a_datetime_and_the_content():
    sink: list = []
    conn = _FakeConn(sink)
    ok = await insert_memory(
        content="the day itself",
        author="grillo",
        source="compaction",
        tags='["x"]',
        conn=conn,
    )
    assert ok is True
    sql, params = sink[0]
    assert "INSERT INTO memories" in sql
    assert isinstance(params[0], datetime), "params[0] is created_at and must be a datetime"
    assert params[0].tzinfo is not None
    assert params[1] == "the day itself"


async def test_insert_memory_uses_the_callers_connection_when_given_one():
    sink: list = []
    conn = _FakeConn(sink)
    await insert_memory(content="x", author="grillo", source="compaction", tags=None, conn=conn)
    assert conn.used == 1, "with conn= it must not open its own connection"


async def test_a_failed_write_raises_instead_of_reporting_success():
    """The swallowed exception is what made data loss invisible."""
    conn = _FakeConn([], boom=True)
    with pytest.raises(RuntimeError):
        await insert_memory(content="x", author="grillo", source="compaction", tags=None, conn=conn)


async def test_empty_content_is_refused():
    with pytest.raises(ValueError):
        await insert_memory(content="   ", author="grillo", source="compaction", tags=None, conn=_FakeConn([]))


# ------------------------------------------------------------------- F7: detailed may be a list

def test_detailed_list_is_joined_not_python_repr():
    from plugins.grillo.grillo_compactor.grillo_compactor import _coerce_text

    got = _coerce_text(["first point", "second point"])
    assert got == "first point\nsecond point"
    assert "[" not in got and "'" not in got
    assert _coerce_text("already prose") == "already prose"
    assert _coerce_text(None) == ""
    assert _coerce_text(["a", None, "  "]) == "a"


# --------------------------------------------------- F2 and F4: guarded by reading the source order

def _plugin_source() -> str:
    return PLUGIN.read_text(encoding="utf-8")


def test_the_memory_is_written_before_the_sources_are_deleted():
    """F2: order inside the persist block, which no unit test can observe without a real database."""
    src = _plugin_source()
    persist = src.index("# Persist accepted clusters.")
    delete = src.index("DELETE FROM ai_diary", persist)
    write = src.index("await insert_memory(", persist)
    assert write < delete, "insert_memory must appear before the DELETE of the source rows"
    assert "conn=conn" in src[write:delete], "the memory write must join the caller's connection"


def test_the_raw_sql_fallback_is_gone():
    src = _plugin_source()
    assert "INSERT INTO memories (created_at, content, author, source, tags, scope, emotion, intensity, emotion_state) VALUES (NOW()" not in src


def test_a_declined_cluster_is_terminal():
    """F4: the decline must short-circuit the cluster, not merely skip the size gate."""
    src = _plugin_source()
    assert 'if not should_compact and _setting("GRILLO_COMPACT_SKIP_DECLINED", True, bool):' in src
    decl = src.index('GRILLO_COMPACT_SKIP_DECLINED')
    size = src.index("smaller than min_cluster_size", decl)
    assert decl < size
    assert '"status": "declined"' in src
