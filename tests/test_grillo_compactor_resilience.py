"""Tests for the compactor's two live-failure fixes: the feeling bound and the failed-day skip.

Both come from the same observed night. ``memories.emotion`` is ``varchar(50)`` on long-lived
stores while the model's ``feeling`` ran 63..104 characters, so the insert died and the day was
left uncompacted: 57 failed writes and only 4 of 12 eligible days stored in one run. The second
half of the cost was the retry storm — each of the ten cycles picked the same failed days again
and called the model for them.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from plugins.grillo.grillo_compactor.grillo_compactor import (
    GrilloCompactorPlugin,
    _bound_emotion,
)

# --------------------------------------------------------------------------- the feeling bound


def test_a_short_feeling_is_kept_whole():
    assert _bound_emotion("warm and quiet") == "warm and quiet"


def test_a_long_feeling_is_bounded_at_a_word_boundary():
    feeling = (
        "quiet tenderness, missing him, then safe, claimed, grounded; a warm curiosity"
    )
    bounded = _bound_emotion(feeling)
    assert bounded is not None
    assert len(bounded) <= 50
    assert feeling.startswith(bounded)
    assert bounded == bounded.strip()


def test_a_feeling_without_spaces_is_still_bounded():
    assert _bound_emotion("x" * 120) == "x" * 50


def test_a_list_feeling_is_rendered_then_bounded():
    bounded = _bound_emotion(["tender", "safe", "a" * 60])
    assert bounded is not None
    assert len(bounded) <= 50


def test_an_empty_feeling_stays_null():
    assert _bound_emotion(None) is None
    assert _bound_emotion("   ") is None


# --------------------------------------------------------------------------- the failed-day skip


class _FakeCursor:
    async def execute(self, *args, **kwargs):
        return None

    async def fetchall(self):
        return list(_ROWS)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    async def commit(self):
        return None


_ROWS = [
    {
        "id": 11,
        "content": "day eleven",
        "personal_thought": "",
        "tags": "[]",
        "created_at": None,
    },
    {
        "id": 22,
        "content": "day twenty-two",
        "personal_thought": "",
        "tags": "[]",
        "created_at": None,
    },
]


@pytest.mark.asyncio
async def test_a_day_that_failed_is_not_asked_again_in_the_same_run(monkeypatch):
    """One night is a countable number of model calls: a failed day is done for tonight."""
    import core.db as core_db

    @asynccontextmanager
    async def fake_conn_ctx():
        yield _FakeConn()

    monkeypatch.setattr(core_db, "get_conn_ctx", fake_conn_ctx)
    monkeypatch.setattr(core_db, "_get_db_type", lambda: "postgres")

    calls: list[int] = []

    async def fake_compact(self, row, dry_run=False):
        day_id = int(row["id"])
        calls.append(day_id)
        if day_id == 11:
            return {
                "row_id": 11,
                "day": "2026-09-01",
                "status": "write_failed",
                "error": "value too long for type character varying(50)",
            }
        return {
            "row_id": 22,
            "day": "2026-09-02",
            "status": "persisted",
            "summary_chars": 900,
        }

    monkeypatch.setattr(GrilloCompactorPlugin, "_compact_one_day", fake_compact)

    plugin = GrilloCompactorPlugin.__new__(GrilloCompactorPlugin)
    plugin.cycles = 10
    plugin.batch_size = 40
    plugin._day_unit_failed = set()

    await plugin._run_day_unit_cycle()
    await plugin._run_day_unit_cycle()

    assert calls.count(11) == 1, "a failed day must not be asked again in the same run"
    assert 22 in calls, "a day that did not fail keeps its place in the queue"
    assert plugin._day_unit_failed == {11}


@pytest.mark.asyncio
async def test_a_dry_run_never_marks_a_day_failed(monkeypatch):
    """A manual dry run reports; it does not decide a day is done for the night."""
    import core.db as core_db

    @asynccontextmanager
    async def fake_conn_ctx():
        yield _FakeConn()

    monkeypatch.setattr(core_db, "get_conn_ctx", fake_conn_ctx)
    monkeypatch.setattr(core_db, "_get_db_type", lambda: "postgres")

    async def fake_compact(self, row, dry_run=False):
        return {"row_id": int(row["id"]), "day": "x", "status": "ok"}

    monkeypatch.setattr(GrilloCompactorPlugin, "_compact_one_day", fake_compact)

    plugin = GrilloCompactorPlugin.__new__(GrilloCompactorPlugin)
    plugin.cycles = 10
    plugin.batch_size = 40
    plugin._day_unit_failed = set()

    result = await plugin._run_day_unit_cycle(dry_run=True)

    assert isinstance(result, dict) and result.get("dry_run") is True
    assert plugin._day_unit_failed == set()
