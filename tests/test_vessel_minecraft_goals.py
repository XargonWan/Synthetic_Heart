"""Tests for the Minecraft Vessel self-directed goal store.

The goal store no longer ships a catalogue of predefined objectives: goals are
free text authored by Synth. These tests cover the pure helpers (``_clip`` /
``_row_to_goal``) and the persistence API (``set_goal`` / ``get_active_goal`` /
``list_recent_goals`` / ``update_active_goal``) against a tiny in-memory fake
DB, so no real ``minecraft_goals`` table is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from plugins.rift_vessel.minecraft import goals


# ----------------------------------------------------------------------
# In-memory fake DB
# ----------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, store: "_FakeStore") -> None:
        self._store = store
        self._rows: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._store.execute(sql, params)
        self._rows = self._store.pop_result()

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, store: "_FakeStore") -> None:
        self._store = store

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._store)

    async def commit(self) -> None:
        return None


class _FakeCtx:
    def __init__(self, store: "_FakeStore") -> None:
        self._store = store

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._store)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeStore:
    """Minimal SQL-ish store for the handful of statements goals.py issues."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._next_id = 1
        self._result: list[tuple[Any, ...]] = []

    def pop_result(self) -> list[tuple[Any, ...]]:
        r = self._result
        self._result = []
        return r

    def _tuple(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            row["id"],
            row["session_id"],
            row["description"],
            row["note"],
            row.get("destination"),
            row.get("steps"),
            row.get("current_step", 0),
            row.get("target_kind"),
            row.get("target_name"),
            row["status"],
        )

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        s = " ".join(sql.split())
        if s.startswith("CREATE TABLE") or s.startswith("CREATE INDEX"):
            return
        if (
            s.startswith("UPDATE minecraft_goals SET status = %s")
            and "WHERE status" in s
        ):
            # Bulk demote: "SET status = %s, updated_at = ... WHERE status = %s"
            new_status, old_status = params[0], params[1]
            for row in self.rows:
                if row["status"] == old_status:
                    row["status"] = new_status
            return
        if s.startswith("INSERT INTO minecraft_goals"):
            # Columns: session_id, description, note, destination,
            #          steps, current_step, target_kind, target_name, status
            (
                session_id,
                description,
                note,
                destination,
                steps,
                current_step,
                target_kind,
                target_name,
                status,
            ) = params
            self.rows.append(
                {
                    "id": self._next_id,
                    "session_id": session_id,
                    "description": description,
                    "note": note,
                    "destination": destination,
                    "steps": steps,
                    "current_step": current_step,
                    "target_kind": target_kind,
                    "target_name": target_name,
                    "status": status,
                }
            )
            self._next_id += 1
            return
        if s.startswith("UPDATE minecraft_goals SET") and "WHERE id = %s" in s:
            # Dynamic SET clause from update_active_goal -- parse column order
            # from the SQL text so it is robust to which fields were supplied.
            set_clause = s[len("UPDATE minecraft_goals SET ") : s.index(" WHERE id")]
            cols = [c.strip().split(" = ")[0] for c in set_clause.split(",")]
            placeholder_cols = [c for c in cols if c != "updated_at"]
            goal_id = params[-1]
            values = params[:-1]
            mapping = dict(zip(placeholder_cols, values))
            for row in self.rows:
                if row["id"] == goal_id:
                    for col, val in mapping.items():
                        row[col] = val
            return
        if s.startswith("SELECT") and "WHERE status = %s" in s:
            status = params[0]
            matches = [r for r in self.rows if r["status"] == status]
            matches.sort(key=lambda r: r["id"], reverse=True)
            self._result = [self._tuple(matches[0])] if matches else []
            return
        if s.startswith("SELECT") and "ORDER BY id DESC LIMIT %s" in s:
            lim = params[0]
            ordered = sorted(self.rows, key=lambda r: r["id"], reverse=True)[:lim]
            self._result = [self._tuple(r) for r in ordered]
            return
        raise AssertionError(f"unexpected SQL: {s}")


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    store = _FakeStore()
    monkeypatch.setattr(goals, "get_conn_ctx", lambda: _FakeCtx(store))
    monkeypatch.setattr(goals, "_get_db_type", lambda: "mariadb")
    monkeypatch.setattr(goals, "_table_initialized", False)
    return store


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def test_clip_trims_and_caps() -> None:
    assert goals._clip("  hello  ", 100) == "hello"
    assert goals._clip(None, 100) == ""
    long = "x" * 999
    assert len(goals._clip(long, 500)) == 500
    assert goals._clip(1234, 100) == "1234"


def test_row_to_goal_tuple_and_dict() -> None:
    assert goals._row_to_goal(None) is None
    # Row columns: id, session_id, description, note, destination,
    #              steps, current_step, target_kind, target_name, status
    tup = (7, "sess", "build a house", "started", None, None, 0, None, None, "active")
    g = goals._row_to_goal(tup)
    assert g == {
        "id": 7,
        "session_id": "sess",
        "description": "build a house",
        "note": "started",
        "destination": None,
        "steps": [],
        "current_step": 0,
        "current_step_text": None,
        "steps_total": 0,
        "target": None,
        "target_kind": None,
        "target_name": None,
        "status": "active",
    }
    d = goals._row_to_goal(
        {
            "id": 8,
            "session_id": None,
            "description": "x",
            "note": None,
            "destination": None,
            "steps": None,
            "current_step": 0,
            "target_kind": None,
            "target_name": None,
            "status": "done",
        }
    )
    assert d is not None and d["status"] == "done"


# ----------------------------------------------------------------------
# Persistence API
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_goal_rejects_empty(fake_db: _FakeStore) -> None:
    res = await goals.set_goal("   ")
    assert res == {"status": "error", "message": "empty_goal"}
    assert fake_db.rows == []


@pytest.mark.asyncio
async def test_set_goal_is_free_text(fake_db: _FakeStore) -> None:
    res = await goals.set_goal("wander the forest and pet a fox", session_id="s1")
    assert res["status"] == "ok"
    assert res["description"] == "wander the forest and pet a fox"
    active = await goals.get_active_goal()
    assert active is not None
    assert active["description"] == "wander the forest and pet a fox"
    assert active["status"] == goals.STATUS_ACTIVE


@pytest.mark.asyncio
async def test_set_goal_clips_length(fake_db: _FakeStore) -> None:
    res = await goals.set_goal("y" * 800)
    assert res["status"] == "ok"
    assert len(res["description"]) == goals._MAX_GOAL_CHARS


@pytest.mark.asyncio
async def test_setting_new_goal_abandons_previous(fake_db: _FakeStore) -> None:
    await goals.set_goal("first goal")
    await goals.set_goal("second goal")
    active = await goals.get_active_goal()
    assert active is not None and active["description"] == "second goal"
    # only one active at a time
    actives = [r for r in fake_db.rows if r["status"] == goals.STATUS_ACTIVE]
    assert len(actives) == 1
    abandoned = [r for r in fake_db.rows if r["status"] == goals.STATUS_ABANDONED]
    assert len(abandoned) == 1


@pytest.mark.asyncio
async def test_list_recent_goals_newest_first(fake_db: _FakeStore) -> None:
    await goals.set_goal("a")
    await goals.set_goal("b")
    await goals.set_goal("c")
    recent = await goals.list_recent_goals()
    assert [g["description"] for g in recent] == ["c", "b", "a"]


@pytest.mark.asyncio
async def test_update_active_goal_note_and_done(fake_db: _FakeStore) -> None:
    await goals.set_goal("build a shelter")
    res = await goals.update_active_goal(note="found a good spot")
    assert res["status"] == "ok"
    assert res["goal_status"] == goals.STATUS_ACTIVE
    active = await goals.get_active_goal()
    assert active is not None and active["note"] == "found a good spot"

    done = await goals.update_active_goal(status=goals.STATUS_DONE)
    assert done["goal_status"] == goals.STATUS_DONE
    assert await goals.get_active_goal() is None


@pytest.mark.asyncio
async def test_update_active_goal_no_active(fake_db: _FakeStore) -> None:
    res = await goals.update_active_goal(note="whatever")
    assert res == {"status": "error", "message": "no_active_goal"}


@pytest.mark.asyncio
async def test_no_catalogue_symbols() -> None:
    # The hard-coded catalogue must be gone entirely.
    for removed in (
        "GOAL_CATALOGUE",
        "GoalTemplate",
        "get_template",
        "_match_count",
        "evaluate_progress",
        "available_goal_types",
        "list_done_types",
        "refresh_active_goal_progress",
    ):
        assert not hasattr(goals, removed), f"{removed} should have been removed"
