"""Tests for the generic scope-aware goal store (``plugins.goals.goals``).

The store started life as the Minecraft-only ``minecraft_goals`` table inside
the Rift Vessel and was extracted into a generic, three-level scope-aware
plugin (``scope`` / ``game`` / ``world``) so goals can be used for any game *or*
for a personal / non-game objective. These tests cover:

* the pure helpers (``_clip`` / ``_coerce_scope`` / ``_row_to_goal``),
* the persistence API against a tiny in-memory fake DB (no real ``goals`` table
  is touched),
* **scope isolation** — an in-world goal and a personal goal coexist, each with
  its own active goal,
* the **auto-completion** fix — advancing past the last step of an ordered plan
  marks the goal ``done`` automatically,
* the WebUI helpers ``list_all_goals`` / ``delete_goal`` / ``clear_abandoned_goals``.
"""

from __future__ import annotations

from typing import Any

import pytest

# ``plugins.goals/__init__`` rebinds the package to the ``goals`` module via a
# sys.modules shim, so a plain ``import plugins.goals.goals`` fails at runtime.
# Trigger the package import (which registers ``plugins.goals.goals`` in
# sys.modules and rebinds the package) then bind the concrete submodule so both
# runtime and the static type checker resolve the private helpers under test.
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-checker resolution only
    from plugins.goals import goals
else:
    import importlib

    goals = importlib.import_module("plugins.goals.goals")


# ----------------------------------------------------------------------
# In-memory fake DB
# ----------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, store: "_FakeStore") -> None:
        self._store = store
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.rowcount = self._store.execute(sql, params)
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
    """Minimal SQL-ish store for the statements ``plugins.goals.goals`` issues."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._next_id = 1
        self._result: list[tuple[Any, ...]] = []

    def pop_result(self) -> list[tuple[Any, ...]]:
        r = self._result
        self._result = []
        return r

    def _tuple(self, row: dict[str, Any]) -> tuple[Any, ...]:
        # Order must match ``_GOAL_COLS`` (13 columns).
        return (
            row["id"],
            row["session_id"],
            row.get("scope", goals.SCOPE_NONE),
            row.get("game", goals.SCOPE_NONE),
            row.get("world", goals.SCOPE_NONE),
            row["description"],
            row["note"],
            row.get("destination"),
            row.get("steps"),
            row.get("current_step", 0),
            row.get("target_kind"),
            row.get("target_name"),
            row["status"],
        )

    def _tuple_with_times(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return self._tuple(row) + (row.get("created_at"), row.get("updated_at"))

    def execute(self, sql: str, params: tuple[Any, ...]) -> int:
        s = " ".join(sql.split())
        if (
            s.startswith("CREATE TABLE")
            or s.startswith("CREATE INDEX")
            or s.startswith("ALTER TABLE")
        ):
            return 0
        # Bulk demote of the active goal in a scope tuple (set_goal).
        if s.startswith("UPDATE goals SET status = %s") and "WHERE status = %s" in s:
            new_status, old_status, sc, gm, wd = params
            n = 0
            for row in self.rows:
                if (
                    row["status"] == old_status
                    and row.get("scope") == sc
                    and row.get("game") == gm
                    and row.get("world") == wd
                ):
                    row["status"] = new_status
                    n += 1
            return n
        if s.startswith("INSERT INTO goals"):
            (
                session_id,
                sc,
                gm,
                wd,
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
                    "scope": sc,
                    "game": gm,
                    "world": wd,
                    "description": description,
                    "note": note,
                    "destination": destination,
                    "steps": steps,
                    "current_step": current_step,
                    "target_kind": target_kind,
                    "target_name": target_name,
                    "status": status,
                    "created_at": None,
                    "updated_at": None,
                }
            )
            self._next_id += 1
            return 1
        # Dynamic SET clause from update_active_goal.
        if s.startswith("UPDATE goals SET") and "WHERE id = %s" in s:
            set_clause = s[len("UPDATE goals SET ") : s.index(" WHERE id")]
            cols = [c.strip().split(" = ")[0] for c in set_clause.split(",")]
            placeholder_cols = [c for c in cols if c != "updated_at"]
            goal_id = params[-1]
            values = params[:-1]
            mapping = dict(zip(placeholder_cols, values))
            n = 0
            for row in self.rows:
                if row["id"] == goal_id:
                    for col, val in mapping.items():
                        row[col] = val
                    n += 1
            return n
        if s.startswith("DELETE FROM goals WHERE id = %s"):
            gid = params[0]
            before = len(self.rows)
            self.rows = [r for r in self.rows if r["id"] != gid]
            return before - len(self.rows)
        if s.startswith("DELETE FROM goals WHERE status = %s"):
            status = params[0]
            before = len(self.rows)
            self.rows = [r for r in self.rows if r["status"] != status]
            return before - len(self.rows)
        if s.startswith("SELECT status FROM goals WHERE id = %s"):
            gid = params[0]
            matches = [r for r in self.rows if r["id"] == gid]
            self._result = [(matches[0]["status"],)] if matches else []
            return 0
        # get_active_goal: WHERE status/scope/game/world ... LIMIT 1
        if s.startswith("SELECT") and "WHERE status = %s AND scope = %s" in s:
            status, sc, gm, wd = params
            matches = [
                r
                for r in self.rows
                if r["status"] == status
                and r.get("scope") == sc
                and r.get("game") == gm
                and r.get("world") == wd
            ]
            matches.sort(key=lambda r: r["id"], reverse=True)
            self._result = [self._tuple(matches[0])] if matches else []
            return 0
        # get_most_recent_active_goal: WHERE status = %s ORDER BY id DESC LIMIT 1
        if s.startswith("SELECT") and "WHERE status = %s ORDER BY id DESC" in s:
            (status,) = params
            matches = [r for r in self.rows if r["status"] == status]
            matches.sort(key=lambda r: r["id"], reverse=True)
            self._result = [self._tuple(matches[0])] if matches else []
            return 0
        # list_recent_goals: WHERE scope/game/world ... LIMIT %s
        if s.startswith("SELECT") and "WHERE scope = %s AND game = %s" in s:
            sc, gm, wd, lim = params
            matches = [
                r
                for r in self.rows
                if r.get("scope") == sc and r.get("game") == gm and r.get("world") == wd
            ]
            matches.sort(key=lambda r: r["id"], reverse=True)
            self._result = [self._tuple(r) for r in matches[:lim]]
            return 0
        # list_all_goals: ORDER BY (status = %s) DESC, id DESC LIMIT %s
        if s.startswith("SELECT") and "created_at, updated_at FROM goals" in s:
            active_status, lim = params
            ordered = sorted(
                self.rows,
                key=lambda r: (r["status"] == active_status, r["id"]),
                reverse=True,
            )[:lim]
            self._result = [self._tuple_with_times(r) for r in ordered]
            return 0
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
    assert len(goals._clip("x" * 999, 500)) == 500


def test_coerce_scope_defaults_and_normalises() -> None:
    assert goals._coerce_scope(None) == goals.SCOPE_NONE
    assert goals._coerce_scope("") == goals.SCOPE_NONE
    assert goals._coerce_scope("  Vessel ") == "vessel"
    assert goals._coerce_scope("MINECRAFT") == "minecraft"
    assert len(goals._coerce_scope("z" * 200)) == goals._MAX_SCOPE_CHARS


def test_row_to_goal_carries_scope() -> None:
    assert goals._row_to_goal(None) is None
    tup = (
        7,
        "sess",
        "vessel",
        "minecraft",
        "none",
        "build a house",
        "started",
        None,
        None,
        0,
        None,
        None,
        "active",
    )
    g = goals._row_to_goal(tup)
    assert g is not None
    assert g["id"] == 7
    assert g["scope"] == "vessel"
    assert g["game"] == "minecraft"
    assert g["world"] == "none"
    assert g["description"] == "build a house"
    assert g["status"] == "active"


# ----------------------------------------------------------------------
# Persistence API
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_goal_rejects_empty(fake_db: _FakeStore) -> None:
    res = await goals.set_goal("   ")
    assert res == {"status": "error", "message": "empty_goal"}
    assert fake_db.rows == []


@pytest.mark.asyncio
async def test_personal_goal_defaults_to_none_scope(fake_db: _FakeStore) -> None:
    res = await goals.set_goal("write a poem about a horse")
    assert res["status"] == "ok"
    assert res["scope"] == goals.SCOPE_NONE
    assert res["game"] == goals.SCOPE_NONE
    assert res["world"] == goals.SCOPE_NONE
    active = await goals.get_active_goal()
    assert active is not None
    assert active["description"] == "write a poem about a horse"


@pytest.mark.asyncio
async def test_setting_new_goal_abandons_previous_same_scope(
    fake_db: _FakeStore,
) -> None:
    await goals.set_goal("first", scope="vessel", game="minecraft")
    await goals.set_goal("second", scope="vessel", game="minecraft")
    active = await goals.get_active_goal(scope="vessel", game="minecraft")
    assert active is not None and active["description"] == "second"
    actives = [
        r
        for r in fake_db.rows
        if r["status"] == goals.STATUS_ACTIVE and r["scope"] == "vessel"
    ]
    assert len(actives) == 1


@pytest.mark.asyncio
async def test_scopes_are_isolated(fake_db: _FakeStore) -> None:
    """A game goal and a personal goal coexist, each with its own active goal."""
    await goals.set_goal("mine iron", scope="vessel", game="minecraft")
    await goals.set_goal("learn the piano")  # personal (none/none/none)

    game_goal = await goals.get_active_goal(scope="vessel", game="minecraft")
    personal_goal = await goals.get_active_goal()
    assert game_goal is not None and game_goal["description"] == "mine iron"
    assert (
        personal_goal is not None and personal_goal["description"] == "learn the piano"
    )

    # Setting a second personal goal must NOT touch the game goal.
    await goals.set_goal("learn to paint")
    game_goal = await goals.get_active_goal(scope="vessel", game="minecraft")
    assert game_goal is not None and game_goal["description"] == "mine iron"
    personal_goal = await goals.get_active_goal()
    assert (
        personal_goal is not None and personal_goal["description"] == "learn to paint"
    )


@pytest.mark.asyncio
async def test_list_recent_goals_scoped_newest_first(fake_db: _FakeStore) -> None:
    await goals.set_goal("a", scope="vessel", game="minecraft")
    await goals.set_goal("b", scope="vessel", game="minecraft")
    await goals.set_goal("personal")  # different scope, must be excluded
    recent = await goals.list_recent_goals(scope="vessel", game="minecraft")
    assert [g["description"] for g in recent] == ["b", "a"]


# ----------------------------------------------------------------------
# Plain-chat scope fallback (goal_set explicit scope vs unscoped goal_update)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_most_recent_active_goal_across_scopes(fake_db: _FakeStore) -> None:
    assert await goals.get_most_recent_active_goal() is None
    await goals.set_goal("old vessel goal", scope="vessel", game="minecraft")
    await goals.set_goal("newer personal goal")
    latest = await goals.get_most_recent_active_goal()
    assert latest is not None
    assert latest["description"] == "newer personal goal"
    assert latest["scope"] == goals.SCOPE_NONE


@pytest.mark.asyncio
async def test_unscoped_chat_goal_update_falls_back_to_explicit_scope(
    fake_db: _FakeStore,
) -> None:
    """An unscoped goal_update from a plain chat acts on the goal just set.

    Regression: a ``goal_set`` that honoured an explicit scope tuple (e.g.
    ``scope='game', game='minecraft'``, as a telegram-originated set did) was
    followed by an unscoped ``goal_update``; the update resolved to the neutral
    ``(none, none, none)`` chat scope, found no active goal there, and returned
    ``no_active_goal`` — so Synth told the user "all goals cleared" while the
    goal stayed ``active`` in the DB. The plugin now falls back to the most
    recently set active goal in ANY scope for unscoped update/list actions.
    """
    plugin = goals.GoalsPlugin()
    set_res = await plugin.execute_action(
        {
            "type": "goal_set",
            "payload": {
                "description": "make a wooden pickaxe",
                "scope": "game",
                "game": "minecraft",
            },
        },
        context={"interface_path": "telegram_bot/5208932647"},
    )
    assert set_res["status"] == "ok"
    # The goal lives under the explicit scope, not the neutral chat scope.
    assert await goals.get_active_goal() is None
    assert await goals.get_active_goal(scope="game", game="minecraft") is not None

    # Unscoped update from the same chat must find it via the fallback.
    upd_res = await plugin.execute_action(
        {"type": "goal_update", "payload": {"status": "done"}},
        context={"interface_path": "telegram_bot/5208932647"},
    )
    assert upd_res["status"] == "ok"
    assert upd_res.get("goal_status") == goals.STATUS_DONE
    assert await goals.get_active_goal(scope="game", game="minecraft") is None


@pytest.mark.asyncio
async def test_unscoped_chat_goal_list_falls_back_to_explicit_scope(
    fake_db: _FakeStore,
) -> None:
    plugin = goals.GoalsPlugin()
    await plugin.execute_action(
        {
            "type": "goal_set",
            "payload": {
                "description": "build a cottage",
                "scope": "vessel",
                "game": "minecraft",
                "world": "w1",
            },
        },
        context={"interface_path": "telegram_bot/5208932647"},
    )
    res = await plugin.execute_action(
        {"type": "goal_list", "payload": {}},
        context={"interface_path": "telegram_bot/5208932647"},
    )
    assert res["status"] == "ok"
    assert res["current_goal"] is not None
    assert res["current_goal"]["description"] == "build a cottage"
    assert res["scope"] == {"scope": "vessel", "game": "minecraft", "world": "w1"}


@pytest.mark.asyncio
async def test_unscoped_chat_goal_update_no_goal_anywhere_still_fails(
    fake_db: _FakeStore,
) -> None:
    """No goal anywhere -> the fallback is a no-op and behaviour is unchanged."""
    plugin = goals.GoalsPlugin()
    res = await plugin.execute_action(
        {"type": "goal_update", "payload": {"status": "done"}},
        context={"interface_path": "telegram_bot/5208932647"},
    )
    assert res == {"status": "error", "message": "no_active_goal"}


@pytest.mark.asyncio
async def test_explicit_scope_goal_update_does_not_fall_back(
    fake_db: _FakeStore,
) -> None:
    """An explicit scope on the update is never overridden by the fallback."""
    await goals.set_goal("game goal", scope="game", game="minecraft")
    await goals.set_goal("personal goal")
    plugin = goals.GoalsPlugin()
    res = await plugin.execute_action(
        {
            "type": "goal_update",
            "payload": {"status": "done", "scope": "none", "game": "none"},
        },
        context={"interface_path": "telegram_bot/5208932647"},
    )
    assert res["status"] == "ok"
    assert res.get("goal_status") == goals.STATUS_DONE
    # The game-scoped goal is untouched.
    assert await goals.get_active_goal(scope="game", game="minecraft") is not None


# ----------------------------------------------------------------------
# Auto-completion (the "not working well" fix)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_past_last_step_auto_completes(fake_db: _FakeStore) -> None:
    await goals.set_goal(
        "get iron",
        scope="vessel",
        game="minecraft",
        steps=["chop wood", "craft pickaxe", "mine iron"],
    )
    # advance twice -> pointer at last step, still active
    r1 = await goals.update_active_goal(advance=True, scope="vessel", game="minecraft")
    assert r1["goal_status"] == goals.STATUS_ACTIVE
    r2 = await goals.update_active_goal(advance=True, scope="vessel", game="minecraft")
    assert r2["goal_status"] == goals.STATUS_ACTIVE
    assert r2["current_step"] == 2
    # advancing past the last step auto-completes the goal
    r3 = await goals.update_active_goal(advance=True, scope="vessel", game="minecraft")
    assert r3["goal_status"] == goals.STATUS_DONE
    assert r3["completed"] is True
    assert r3["auto_completed"] is True
    assert await goals.get_active_goal(scope="vessel", game="minecraft") is None


@pytest.mark.asyncio
async def test_stepless_goal_advance_is_noop(fake_db: _FakeStore) -> None:
    """A stepless goal is NOT completed by a reflexive ``advance``.

    Synth signals ``advance`` as a generic "keep going" while its own note says
    the first step is still ahead — auto-completing on that falsely closed a
    fresh goal before its Drone-expanded plan landed, re-triggering the goal
    beat and a churn of re-authored goals (observed live). A stepless goal
    completes only EXPLICITLY (``status='done'``) or via the goal debrief.
    """
    await goals.set_goal("wander around", scope="vessel", game="minecraft")
    r = await goals.update_active_goal(advance=True, scope="vessel", game="minecraft")
    assert r["goal_status"] == goals.STATUS_ACTIVE
    assert r["completed"] is False
    assert r["auto_completed"] is False
    assert await goals.get_active_goal(scope="vessel", game="minecraft") is not None

    # Explicit status='done' still completes a stepless goal.
    r2 = await goals.update_active_goal(
        status=goals.STATUS_DONE, scope="vessel", game="minecraft"
    )
    assert r2["goal_status"] == goals.STATUS_DONE
    assert r2["completed"] is True


@pytest.mark.asyncio
async def test_stepless_goal_note_only_stays_active(fake_db: _FakeStore) -> None:
    """A plain note keeps a stepless goal active."""
    await goals.set_goal("wander around", scope="vessel", game="minecraft")
    r = await goals.update_active_goal(
        note="still going", scope="vessel", game="minecraft"
    )
    assert r["goal_status"] == goals.STATUS_ACTIVE
    assert r["completed"] is False


@pytest.mark.asyncio
async def test_abandon_wins_over_auto_complete(fake_db: _FakeStore) -> None:
    await goals.set_goal("get iron", scope="vessel", steps=["a", "b"])
    r = await goals.update_active_goal(
        status=goals.STATUS_ABANDONED, advance=True, scope="vessel"
    )
    assert r["goal_status"] == goals.STATUS_ABANDONED
    assert r["auto_completed"] is False


@pytest.mark.asyncio
async def test_update_active_goal_no_active(fake_db: _FakeStore) -> None:
    res = await goals.update_active_goal(note="whatever")
    assert res == {"status": "error", "message": "no_active_goal"}


# ----------------------------------------------------------------------
# WebUI helpers
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_goals_scope_agnostic(fake_db: _FakeStore) -> None:
    await goals.set_goal("game goal", scope="vessel", game="minecraft")
    await goals.set_goal("personal goal")
    allg = await goals.list_all_goals()
    descs = {g["description"] for g in allg}
    assert descs == {"game goal", "personal goal"}
    # every goal carries its scope tuple + timestamps keys
    for g in allg:
        assert "scope" in g and "game" in g and "world" in g
        assert "created_at" in g and "updated_at" in g


@pytest.mark.asyncio
async def test_delete_goal_protects_active(fake_db: _FakeStore) -> None:
    await goals.set_goal("active one", scope="vessel")
    active = await goals.get_active_goal(scope="vessel")
    assert active is not None
    res = await goals.delete_goal(active["id"])
    assert res["status"] == "error"
    assert res["message"] == "cannot_delete_active"
    # still there
    assert await goals.get_active_goal(scope="vessel") is not None


@pytest.mark.asyncio
async def test_delete_goal_removes_non_active(fake_db: _FakeStore) -> None:
    await goals.set_goal("first", scope="vessel")
    await goals.set_goal("second", scope="vessel")  # abandons "first"
    abandoned = [r for r in fake_db.rows if r["status"] == goals.STATUS_ABANDONED]
    assert len(abandoned) == 1
    res = await goals.delete_goal(abandoned[0]["id"])
    assert res["status"] == "ok"
    assert res["deleted_count"] == 1


@pytest.mark.asyncio
async def test_delete_goal_not_found(fake_db: _FakeStore) -> None:
    res = await goals.delete_goal(999)
    assert res["status"] == "error"
    assert res["message"] == "not_found"


@pytest.mark.asyncio
async def test_clear_abandoned_goals(fake_db: _FakeStore) -> None:
    await goals.set_goal("a", scope="vessel")
    await goals.set_goal("b", scope="vessel")  # abandons "a"
    await goals.set_goal("c", scope="vessel")  # abandons "b"
    res = await goals.clear_abandoned_goals()
    assert res["status"] == "ok"
    assert res["deleted_count"] == 2
    remaining = [r["status"] for r in fake_db.rows]
    assert goals.STATUS_ABANDONED not in remaining
