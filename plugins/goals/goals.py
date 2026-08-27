# plugins/goals/goals.py
"""Generic self-directed goal store and plugin.

Synth authors its own goals — free text, out of its own personality and mood.
This module is deliberately small: **persist and recall** the free-form goals
Synth sets for itself so a goal survives across beats and sessions, and Synth
can review, update, complete, or drop it. There is **no catalogue** of
predefined objectives.

This started life as the Minecraft-only ``minecraft_goals`` store inside the
Rift Vessel, but goals are not a game concept — Synth may pursue a goal in a
game world, in a *different* game world, or entirely outside any game (a
personal life goal, a plan such as "write a poem about a horse"). So the store
is generic and every goal carries a three-level structural **scope**:

* ``scope`` — the high-level domain (e.g. ``"vessel"`` for in-world play, or
  ``"none"`` for a personal / non-game goal).
* ``game`` — the game name when in a game (e.g. ``"minecraft"``), else
  ``"none"``.
* ``world`` — the specific server/world instance when a game has several, else
  ``"none"``.

A goal with ``scope=none / game=none / world=none`` is a plain personal goal.
The active goal is resolved **per scope tuple**, so an in-world goal and a
personal goal can coexist without clashing.

Design notes
------------
* **No hard-coded objectives.** Goals are plain text authored by Synth. This
  module owns lifecycle, storage and *structural* progress only, never what the
  goals should be.
* **Auto-completion is structural.** When a goal has an ordered plan and its
  pointer reaches the end (Synth advanced past the last step), the goal is
  marked ``done`` automatically — a pure ``current_step >= total_steps`` test,
  never a keyword parse.
* **Persistent.** Goals live in the ``goals`` table (Postgres + MariaDB). Time
  columns are ``created_at`` / ``updated_at`` — never a bare ``timestamp`` (a
  Postgres reserved word, see AGENTS.md section 12).
* **Fail-safe.** Every DB touch is guarded; a failure degrades to "no goal" and
  never breaks the caller.
* **Fast Lane only.** The actions declare **no** ``external_effects``, so they
  stay on the Fast Lane and never spawn agentic tasks (AGENTS.md section 5c).
"""

from __future__ import annotations

import json
import traceback
from typing import Any, Dict, List

from core.db import _get_db_type, get_conn_ctx
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.plugin_base import PluginBase

LOG_PREFIX = "[goals]"

# Goal lifecycle states.
STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_ABANDONED = "abandoned"

# Guardrail only -- a self-authored goal is free text, we just cap its length so
# a runaway model can't write an unbounded blob into the DB.
_MAX_GOAL_CHARS = 500
_MAX_NOTE_CHARS = 1000
# A multi-step plan is a list of free-text steps Synth authored itself (there is
# no catalogue). These caps only stop a runaway model from writing an unbounded
# blob -- they never constrain *what* the steps are.
_MAX_STEPS = 30
_MAX_STEP_CHARS = 300

# Default scope value for every level. A three-part scope tuple keys a goal to a
# domain/game/world; "none" is the neutral value that also marks a personal,
# non-game goal.
SCOPE_NONE = "none"
_MAX_SCOPE_CHARS = 64

# A structural target is the *technical* thing the body should head for, chosen
# from what the world actually reports (never a keyword parse of the goal text).
# ``target_kind`` is a closed enum; ``target_name`` is the exact block/entity id
# the will beat / drone planner selected from the live scan.
TARGET_KIND_BLOCK = "block"
TARGET_KIND_ENTITY = "entity"
TARGET_KIND_COORDINATE = "coordinate"
_TARGET_KINDS = (TARGET_KIND_BLOCK, TARGET_KIND_ENTITY, TARGET_KIND_COORDINATE)
_MAX_TARGET_NAME_CHARS = 64

_table_initialized = False


def _coerce_scope(value: Any) -> str:
    """Normalise a single scope level into a trimmed, lower-case token.

    Empty / ``None`` degrades to :data:`SCOPE_NONE`. This never inspects the
    goal text — it only sanitises one structural scope level so the store can
    key on it.
    """
    s = str(value or "").strip().lower()
    if not s:
        return SCOPE_NONE
    if len(s) > _MAX_SCOPE_CHARS:
        s = s[:_MAX_SCOPE_CHARS].rstrip()
    return s or SCOPE_NONE


def _coerce_target(kind: Any, name: Any) -> Dict[str, str] | None:
    """Normalise a structural target into ``{kind, name}`` or ``None``.

    ``kind`` must be one of :data:`_TARGET_KINDS` (a closed enum — never a
    free-text parse). For a ``block`` / ``entity`` target, ``name`` is the exact
    id the will beat picked from the live scan and is required; for a
    ``coordinate`` target the destination coordinates carry the location and no
    name is needed. This never inspects the goal's free text — it only validates
    the enum and trims the id. Returns ``None`` when there is no usable target.
    """
    if kind is None:
        return None
    k = str(kind).strip().lower()
    if k not in _TARGET_KINDS:
        return None
    if k == TARGET_KIND_COORDINATE:
        return {"kind": k, "name": ""}
    n = _clip(name, _MAX_TARGET_NAME_CHARS).lower()
    if not n:
        return None
    return {"kind": k, "name": n}


def _coerce_steps(steps: Any) -> List[str]:
    """Normalise a self-authored plan into a clean list of free-text steps.

    ``steps`` is whatever cognition supplied for a multi-step goal (e.g. the
    ordered stages of "build a full iron armor set"): a list of strings, a
    single string, or None. Each step is trimmed and length-capped; empties are
    dropped and the list is capped at ``_MAX_STEPS``. This never inspects step
    *text* for keywords -- it only sanitises structure.
    """
    if steps is None:
        return []
    if isinstance(steps, str):
        # A JSON-encoded array (e.g. the goal-expansion Drone serialising the
        # plan) must be decoded, not wrapped — wrapping produced the observed
        # double-encoded steps column (a list containing one JSON string:
        # ["[\"Gather logs…\", \"Craft…\"]"]), which broke step rendering and
        # advancement. Anything that does not parse as an array is a single
        # free-text step.
        stripped = steps.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                raw_list: List[Any] = list(parsed)
            else:
                raw_list = [steps]
        else:
            raw_list = [steps]
    elif isinstance(steps, (list, tuple)):
        raw_list = list(steps)
    else:
        return []
    out: List[str] = []
    for item in raw_list:
        s = _clip(item, _MAX_STEP_CHARS)
        if s:
            out.append(s)
        if len(out) >= _MAX_STEPS:
            break
    return out


def _coerce_destination(destination: Any) -> Dict[str, float] | None:
    """Normalise a self-chosen travel destination into ``{x, z}`` (optional y).

    The *will beat* (cognition) decides **where** Synth wants to head when what
    it is looking for is not in the current area -- e.g. leaving a treeless
    desert to find a forest. That destination is a pure structural coordinate
    hint: this helper coerces whatever cognition supplied (a dict with x/y/z, or
    None) into a clean ``{x, z}`` (plus ``y`` when given), or ``None`` when there
    is no usable destination. It never inspects goal *text* -- purely numeric.
    """
    if not isinstance(destination, dict):
        return None
    out: Dict[str, float] = {}
    for axis in ("x", "y", "z"):
        val = destination.get(axis)
        if val is None:
            continue
        try:
            out[axis] = float(val)
        except (TypeError, ValueError):
            continue
    # A destination is only meaningful with a horizontal target (x and z).
    if "x" not in out or "z" not in out:
        return None
    return out


async def init_goal_table() -> None:
    """Create the ``goals`` table (idempotent, both backends)."""
    global _table_initialized
    if _table_initialized:
        return

    is_postgres = _get_db_type() == "postgres"
    if is_postgres:
        id_col = "id SERIAL PRIMARY KEY"
        created_col = "created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP"
        updated_col = "updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP"
    else:
        id_col = "id INT AUTO_INCREMENT PRIMARY KEY"
        created_col = "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
        updated_col = "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"

    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS goals (
                        {id_col},
                        session_id VARCHAR(128),
                        scope VARCHAR(64) DEFAULT '{SCOPE_NONE}',
                        game VARCHAR(64) DEFAULT '{SCOPE_NONE}',
                        world VARCHAR(64) DEFAULT '{SCOPE_NONE}',
                        description TEXT NOT NULL,
                        note TEXT,
                        destination TEXT,
                        steps TEXT,
                        current_step INT DEFAULT 0,
                        target_kind VARCHAR(16),
                        target_name VARCHAR(64),
                        status VARCHAR(32) DEFAULT '{STATUS_ACTIVE}',
                        {created_col},
                        {updated_col}
                    )
                """)
                # Idempotent add for pre-existing tables (both backends support
                # IF NOT EXISTS on ADD COLUMN in the versions we target). This
                # also self-migrates a renamed legacy ``minecraft_goals`` table
                # that lacks the scope columns.
                for col_ddl in (
                    f"scope VARCHAR(64) DEFAULT '{SCOPE_NONE}'",
                    f"game VARCHAR(64) DEFAULT '{SCOPE_NONE}'",
                    f"world VARCHAR(64) DEFAULT '{SCOPE_NONE}'",
                    "destination TEXT",
                    "steps TEXT",
                    "current_step INT DEFAULT 0",
                    "target_kind VARCHAR(16)",
                    "target_name VARCHAR(64)",
                ):
                    try:
                        await cur.execute(
                            f"ALTER TABLE goals ADD COLUMN IF NOT EXISTS {col_ddl}"
                        )
                    except Exception as col_exc:  # pragma: no cover - defensive
                        log_debug(
                            f"{LOG_PREFIX} column add skipped ({col_ddl}): {col_exc}"
                        )
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_goals_status
                    ON goals (status)
                """)
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_goals_session
                    ON goals (session_id)
                """)
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_goals_scope
                    ON goals (scope, game, world)
                """)
                await conn.commit()
        _table_initialized = True
        log_info(f"{LOG_PREFIX} DB table initialized")
    except Exception as exc:
        log_error(f"{LOG_PREFIX} failed to init goal table: {exc}")


def _clip(text: Any, limit: int) -> str:
    """Coerce to a trimmed, length-capped string."""
    s = str(text or "").strip()
    if len(s) > limit:
        s = s[:limit].rstrip()
    return s


# ----------------------------------------------------------------------
# Persistence helpers (all fail-safe)
# ----------------------------------------------------------------------

# The full column list every read selects (order matters for tuple cursors).
_GOAL_COLS = (
    "id, session_id, scope, game, world, description, note, "
    "destination, steps, current_step, target_kind, target_name, status"
)


async def get_active_goal(
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
) -> Dict[str, Any] | None:
    """Return the most recently set active goal for a scope tuple, or None."""
    await init_goal_table()
    sc, gm, wd = _coerce_scope(scope), _coerce_scope(game), _coerce_scope(world)
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_GOAL_COLS} FROM goals "
                    "WHERE status = %s AND scope = %s AND game = %s AND world = %s "
                    "ORDER BY id DESC LIMIT 1",
                    (STATUS_ACTIVE, sc, gm, wd),
                )
                row = await cur.fetchone()
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} get_active_goal failed: {exc}")
        return None
    return _row_to_goal(row)


async def get_most_recent_active_goal() -> Dict[str, Any] | None:
    """Return the most recently set active goal across ALL scopes, or None.

    Structural fallback for unscoped goal actions from ordinary chats: lets an
    unscoped ``goal_update`` / ``goal_list`` act on "the goal I just
    set/mentioned" even when that goal was created under an explicit scope
    tuple (e.g. ``scope='game', game='minecraft'`` — the mismatch that caused
    ``no_active_goal`` when a goal_set honoured an explicit scope but the
    follow-up goal_update passed none). Read-only; fail-safe; never inspects
    goal text — recency is ``id DESC``.
    """
    await init_goal_table()
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_GOAL_COLS} FROM goals "
                    "WHERE status = %s ORDER BY id DESC LIMIT 1",
                    (STATUS_ACTIVE,),
                )
                row = await cur.fetchone()
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} get_most_recent_active_goal failed: {exc}")
        return None
    return _row_to_goal(row)


async def list_recent_goals(
    limit: int = 10,
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
) -> List[Dict[str, Any]]:
    """Return recent goals for a scope tuple (any status), newest first."""
    await init_goal_table()
    sc, gm, wd = _coerce_scope(scope), _coerce_scope(game), _coerce_scope(world)
    try:
        lim = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        lim = 10
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_GOAL_COLS} FROM goals "
                    "WHERE scope = %s AND game = %s AND world = %s "
                    "ORDER BY id DESC LIMIT %s",
                    (sc, gm, wd, lim),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} list_recent_goals failed: {exc}")
        return []
    return [g for g in (_row_to_goal(r) for r in rows or []) if g is not None]


async def list_all_goals(limit: int = 200) -> List[Dict[str, Any]]:
    """Return every goal with timestamps for the WebUI Goals view (fail-safe).

    Unlike :func:`list_recent_goals` this is **scope-agnostic**: it returns
    goals across every scope so the WebUI can render and filter them itself
    (each goal carries its ``scope`` / ``game`` / ``world``). It also carries
    ``created_at`` / ``updated_at`` for the history cards. Active goals sort
    first, then newest-first. Read-only; degrades to an empty list on error.
    """
    await init_goal_table()
    try:
        lim = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        lim = 200
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_GOAL_COLS}, created_at, updated_at FROM goals "
                    "ORDER BY (status = %s) DESC, id DESC LIMIT %s",
                    (STATUS_ACTIVE, lim),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} list_all_goals failed: {exc}")
        return []
    goals: List[Dict[str, Any]] = []
    for row in rows or []:
        created_at: Any = None
        updated_at: Any = None
        core_row: Any = row
        if isinstance(row, (tuple, list)):
            if len(row) > 13:
                created_at = row[13]
            if len(row) > 14:
                updated_at = row[14]
            core_row = row[:13]
        else:
            # dict cursors and asyncpg ``Record`` rows both support key access
            # (``.get`` / mapping ``__getitem__``); read the two appended
            # timestamp columns by name so Postgres ``Record`` rows — which are
            # neither ``tuple`` nor ``dict`` under ``isinstance`` — are handled.
            getter = getattr(row, "get", None)
            if callable(getter):
                created_at = getter("created_at")
                updated_at = getter("updated_at")
            else:
                try:
                    created_at = row["created_at"]
                    updated_at = row["updated_at"]
                except (KeyError, TypeError, IndexError):
                    created_at = None
                    updated_at = None
        goal = _row_to_goal(core_row)
        if goal is None:
            continue
        goal["created_at"] = (
            created_at.isoformat() if hasattr(created_at, "isoformat") else created_at
        )
        goal["updated_at"] = (
            updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at
        )
        goals.append(goal)
    return goals


async def delete_goal(goal_id: int) -> Dict[str, Any]:
    """Delete a single non-active goal by id (fail-safe).

    The active goal is protected — Synth's current objective is not removable
    from the WebUI so a session in progress is never disrupted. Returns a status
    dict with the number of rows deleted.
    """
    await init_goal_table()
    try:
        gid = int(goal_id)
    except (TypeError, ValueError):
        return {"status": "error", "message": "invalid_id"}
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status FROM goals WHERE id = %s",
                    (gid,),
                )
                row = await cur.fetchone()
                if row is None:
                    return {"status": "error", "message": "not_found"}
                status = row[0] if isinstance(row, (tuple, list)) else row.get("status")
                if status == STATUS_ACTIVE:
                    return {"status": "error", "message": "cannot_delete_active"}
                await cur.execute("DELETE FROM goals WHERE id = %s", (gid,))
                deleted = getattr(cur, "rowcount", 0) or 0
                await conn.commit()
        log_info(f"{LOG_PREFIX} deleted goal id={gid}")
        return {"status": "ok", "deleted_count": deleted}
    except Exception as exc:
        log_error(f"{LOG_PREFIX} delete_goal failed: {exc}")
        return {"status": "error", "message": str(exc)}


async def clear_abandoned_goals() -> Dict[str, Any]:
    """Delete every abandoned goal across all scopes (fail-safe).

    Only ``abandoned`` goals are removed; the active and done goals are left
    untouched. Returns a status dict with the number of rows deleted.
    """
    await init_goal_table()
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM goals WHERE status = %s",
                    (STATUS_ABANDONED,),
                )
                deleted = getattr(cur, "rowcount", 0) or 0
                await conn.commit()
        log_info(f"{LOG_PREFIX} cleared {deleted} abandoned goal(s)")
        return {"status": "ok", "deleted_count": deleted}
    except Exception as exc:
        log_error(f"{LOG_PREFIX} clear_abandoned_goals failed: {exc}")
        return {"status": "error", "message": str(exc)}


async def clear_all_goals(
    scope: Any = None,
    game: Any = None,
    world: Any = None,
) -> Dict[str, Any]:
    """Delete EVERY goal matching the scope tuple (fail-safe, structural).

    The WebUI "clear all" — wipes active, done and abandoned goals alike so
    Synth starts a completely clean attempt (unlike :func:`delete_goal`, which
    protects the active goal, and :func:`clear_abandoned_goals`, which keeps
    active/done rows). Scope params are optional: an omitted column matches any
    value, so the Minecraft shim can pin ``scope``/``game`` while clearing all
    concrete servers. Filtering is column equality on the structural scope
    tuple — never text parsing. Returns a status dict with the rows deleted.
    """
    await init_goal_table()
    clauses: list[str] = []
    params: list[Any] = []
    for col, val in (("scope", scope), ("game", game), ("world", world)):
        if val is not None and str(val).strip():
            clauses.append(f"{col} = %s")
            params.append(str(val).strip())
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                if clauses:
                    await cur.execute(
                        f"DELETE FROM goals WHERE {' AND '.join(clauses)}",
                        params,
                    )
                else:
                    await cur.execute("DELETE FROM goals")
                deleted = getattr(cur, "rowcount", 0) or 0
                await conn.commit()
        log_info(
            f"{LOG_PREFIX} cleared {deleted} goal(s) "
            f"(scope={scope!r} game={game!r} world={world!r})"
        )
        return {"status": "ok", "deleted_count": deleted}
    except Exception as exc:
        log_error(f"{LOG_PREFIX} clear_all_goals failed: {exc}")
        return {"status": "error", "message": str(exc)}


async def set_goal(
    description: str,
    session_id: str | None = None,
    note: str | None = None,
    destination: Any = None,
    steps: Any = None,
    target_kind: Any = None,
    target_name: Any = None,
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
) -> Dict[str, Any]:
    """Adopt a **self-authored** free-text goal as the new active objective.

    ``description`` is whatever Synth decided it wants to do, in its own words --
    there is no catalogue to validate against. ``steps`` is an optional ordered
    list of free-text sub-steps Synth wrote itself to break a bigger objective
    down. ``destination`` is an optional ``{x, z}`` (plus ``y``) coordinate
    cognition chose to head toward. ``scope`` / ``game`` / ``world`` key the goal
    to a domain (a game world, or ``none`` for a personal goal). Any currently
    active goal **for the same scope tuple** is demoted to abandoned so there is
    a single active objective per scope. Returns a status dict.
    """
    desc = _clip(description, _MAX_GOAL_CHARS)
    if not desc:
        return {"status": "error", "message": "empty_goal"}
    sc, gm, wd = _coerce_scope(scope), _coerce_scope(game), _coerce_scope(world)
    note_txt = _clip(note, _MAX_NOTE_CHARS) or None
    dest = _coerce_destination(destination)
    dest_json = json.dumps(dest) if dest else None
    step_list = _coerce_steps(steps)
    steps_json = json.dumps(step_list) if step_list else None
    target = _coerce_target(target_kind, target_name)
    tgt_kind = target["kind"] if target else None
    tgt_name = target["name"] if target and target["name"] else None
    await init_goal_table()
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE goals SET status = %s, updated_at = CURRENT_TIMESTAMP "
                    "WHERE status = %s AND scope = %s AND game = %s AND world = %s",
                    (STATUS_ABANDONED, STATUS_ACTIVE, sc, gm, wd),
                )
                await cur.execute(
                    "INSERT INTO goals "
                    "(session_id, scope, game, world, description, note, "
                    "destination, steps, current_step, target_kind, "
                    "target_name, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        session_id,
                        sc,
                        gm,
                        wd,
                        desc,
                        note_txt,
                        dest_json,
                        steps_json,
                        0,
                        tgt_kind,
                        tgt_name,
                        STATUS_ACTIVE,
                    ),
                )
                await conn.commit()
        log_info(
            f"{LOG_PREFIX} adopted self-authored goal: {desc!r} "
            f"(scope={sc}/{gm}/{wd}, {len(step_list)} step(s), target={target})"
        )
        return {
            "status": "ok",
            "description": desc,
            "scope": sc,
            "game": gm,
            "world": wd,
            "destination": dest,
            "steps": step_list,
            "target": target,
        }
    except Exception as exc:
        # This guard keeps the caller alive, but a *deterministic* bug here
        # silently disables the entire goal subsystem (no goal ever persists).
        # Log the full traceback so the next occurrence points straight at the
        # offending line instead of only surfacing ``str(exc)`` (see TODO §0).
        log_error(f"{LOG_PREFIX} set_goal failed: {exc}\n{traceback.format_exc()}")
        return {"status": "error", "message": str(exc)}


async def update_active_goal(
    note: str | None = None,
    status: str | None = None,
    destination: Any = None,
    steps: Any = None,
    current_step: Any = None,
    advance: bool = False,
    target_kind: Any = None,
    target_name: Any = None,
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
) -> Dict[str, Any]:
    """Let Synth annotate progress on, complete, drop, re-aim, or re-plan its goal.

    Operates on the active goal for the given scope tuple. ``note`` records how
    it is going; ``status`` may be set to ``done`` / ``abandoned``.
    ``destination`` re-aims where the body heads. ``steps`` replaces the whole
    ordered plan (resetting the current step to 0). ``current_step`` jumps the
    pointer to an explicit 0-based index (clamped). ``advance`` moves the pointer
    to the next step -- this is how Synth records "I finished the current
    sub-step".

    **Auto-completion (structural, never a keyword parse):** the goal is marked
    ``done`` automatically in two structural cases — (a) a *stepped* goal whose
    pointer reaches the end (``current_step >= total_steps``); (b) a *stepless*
    goal on which ``advance`` is signalled, since a goal with no ordered plan
    has a single implicit step (the goal itself) so "I advanced/finished it" is
    its completion. Both are suppressed when Synth explicitly abandons the goal.
    The returned dict carries ``completed: True`` in either case so callers (and
    the will/reflection beat) know to propose a fresh goal.
    """
    sc, gm, wd = _coerce_scope(scope), _coerce_scope(game), _coerce_scope(world)
    goal = await get_active_goal(scope=sc, game=gm, world=wd)
    if goal is None:
        return {"status": "error", "message": "no_active_goal"}
    explicit_status = (
        status if status in (STATUS_DONE, STATUS_ABANDONED) else STATUS_ACTIVE
    )
    note_txt = _clip(note, _MAX_NOTE_CHARS)
    # ``None`` = leave destination as-is; anything else = replace/clear it.
    change_dest = destination is not None
    dest = _coerce_destination(destination)
    dest_json = json.dumps(dest) if dest else None

    # ``target_kind is None`` = leave the structural target untouched; any other
    # value re-aims (or, when it fails the enum/id check, clears) the target.
    change_target = target_kind is not None
    new_target = _coerce_target(target_kind, target_name) if change_target else None
    tgt_kind = new_target["kind"] if new_target else None
    tgt_name = new_target["name"] if new_target and new_target["name"] else None

    # Plan re-write / pointer moves. Replacing steps resets the pointer to 0.
    change_steps = steps is not None
    new_steps = _coerce_steps(steps) if change_steps else goal.get("steps") or []
    steps_json = json.dumps(new_steps) if new_steps else None
    total_steps = len(new_steps)

    change_step_idx = False
    new_idx = int(goal.get("current_step") or 0)
    if change_steps:
        # A fresh plan starts from its first step.
        new_idx = 0
        change_step_idx = True
    if current_step is not None:
        try:
            new_idx = int(current_step)
            change_step_idx = True
        except (TypeError, ValueError):
            pass
    if advance:
        new_idx += 1
        change_step_idx = True
    if total_steps:
        new_idx = max(0, min(new_idx, total_steps))
    else:
        new_idx = 0

    # Structural auto-completion — never a keyword parse:
    #   (a) A *stepped* goal whose pointer has reached the end of its plan
    #       (``current_step >= total_steps``): Synth advanced past the last step.
    # A *stepless* goal is only ever completed EXPLICITLY (``status='done'``) or
    # by the goal debrief when its outcome is structurally satisfied. Rule (b)
    # — auto-completing a stepless goal on ``advance`` — was removed: Synth
    # habitually signals ``advance`` as a generic "keep going" while its own
    # note says the first step is still ahead (observed live: a fresh wool-bed
    # goal was auto-completed 54 s after being set, before its Drone-expanded
    # plan had landed, re-triggering the goal beat and a churn of re-authored
    # goals). ``advance`` on a stepless goal is now a harmless no-op.
    # Auto-completion is suppressed when Synth explicitly abandoned the goal.
    auto_completed = explicit_status != STATUS_ABANDONED and (
        total_steps > 0 and new_idx >= total_steps
    )
    new_status = STATUS_DONE if auto_completed else explicit_status
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                sets = ["status = %s", "updated_at = CURRENT_TIMESTAMP"]
                params: list[Any] = [new_status]
                if note_txt:
                    sets.insert(0, "note = %s")
                    params.insert(0, note_txt)
                if change_dest:
                    sets.append("destination = %s")
                    params.append(dest_json)
                if change_steps:
                    sets.append("steps = %s")
                    params.append(steps_json)
                if change_step_idx:
                    sets.append("current_step = %s")
                    params.append(new_idx)
                if change_target:
                    sets.append("target_kind = %s")
                    params.append(tgt_kind)
                    sets.append("target_name = %s")
                    params.append(tgt_name)
                params.append(goal.get("id"))
                await cur.execute(
                    f"UPDATE goals SET {', '.join(sets)} WHERE id = %s",
                    tuple(params),
                )
                await conn.commit()
        if new_status == STATUS_DONE:
            if auto_completed:
                log_info(
                    f"{LOG_PREFIX} goal auto-completed (all {total_steps} steps done)"
                )
            else:
                log_info(f"{LOG_PREFIX} goal marked done by Synth")
        elif advance:
            log_info(f"{LOG_PREFIX} advanced to plan step {new_idx}/{total_steps}")
        return {
            "status": "ok",
            "goal_status": new_status,
            "completed": new_status == STATUS_DONE,
            "auto_completed": auto_completed,
            "current_step": new_idx,
            "steps_total": total_steps,
            "target": new_target if change_target else goal.get("target"),
        }
    except Exception as exc:
        # Sibling write path (action beat / goal-expander drone record progress
        # here). Surface the full traceback for the same reason as ``set_goal``.
        log_error(
            f"{LOG_PREFIX} update_active_goal failed: {exc}\n{traceback.format_exc()}"
        )
        return {"status": "error", "message": str(exc)}


def _row_to_goal(row: Any) -> Dict[str, Any] | None:
    """Normalize a DB row (tuple or dict cursor) into a goal dict."""
    if not row:
        return None
    if isinstance(row, (tuple, list)):
        keys = (
            "id",
            "session_id",
            "scope",
            "game",
            "world",
            "description",
            "note",
            "destination",
            "steps",
            "current_step",
            "target_kind",
            "target_name",
            "status",
        )
        row = dict(zip(keys, row))
    raw_dest = row.get("destination")
    destination: Dict[str, float] | None = None
    if isinstance(raw_dest, dict):
        destination = _coerce_destination(raw_dest)
    elif isinstance(raw_dest, str) and raw_dest.strip():
        try:
            destination = _coerce_destination(json.loads(raw_dest))
        except (ValueError, TypeError):
            destination = None
    raw_steps = row.get("steps")
    steps: List[str] = []
    if isinstance(raw_steps, (list, tuple)):
        steps = _coerce_steps(raw_steps)
    elif isinstance(raw_steps, str) and raw_steps.strip():
        try:
            steps = _coerce_steps(json.loads(raw_steps))
        except (ValueError, TypeError):
            steps = []
    try:
        current_step = int(row.get("current_step") or 0)
    except (TypeError, ValueError):
        current_step = 0
    if steps:
        current_step = max(0, min(current_step, len(steps)))
    current_step_text: str | None = None
    if steps and current_step < len(steps):
        current_step_text = steps[current_step]
    target = _coerce_target(row.get("target_kind"), row.get("target_name"))
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "scope": row.get("scope") or SCOPE_NONE,
        "game": row.get("game") or SCOPE_NONE,
        "world": row.get("world") or SCOPE_NONE,
        "description": row.get("description"),
        "note": row.get("note"),
        "destination": destination,
        "steps": steps,
        "current_step": current_step,
        "current_step_text": current_step_text,
        "steps_total": len(steps),
        "target": target,
        "target_kind": target["kind"] if target else None,
        "target_name": target["name"] if target and target["name"] else None,
        "status": row.get("status"),
    }


# ----------------------------------------------------------------------
# Plugin
# ----------------------------------------------------------------------


def _interface_path_from(
    context: Dict[str, Any] | None,
    message: Any = None,
) -> str:
    """Best-effort structural read of the turn's ``interface_path``.

    Reads it from the context dict first, then falls back to the original
    message object/dict. Never inspects message *text* — only routing metadata.
    """
    if isinstance(context, dict):
        ipath = context.get("interface_path")
        if isinstance(ipath, str) and ipath.strip():
            return ipath.strip()
    if message is not None:
        ipath = getattr(message, "interface_path", None)
        if isinstance(ipath, str) and ipath.strip():
            return ipath.strip()
        if isinstance(message, dict):
            ipath = message.get("interface_path")
            if isinstance(ipath, str) and ipath.strip():
                return ipath.strip()
    return ""


def _scope_from_context(
    context: Dict[str, Any] | None,
    message: Any = None,
) -> tuple[str, str, str]:
    """Derive the ``(scope, game, world)`` tuple from the turn context.

    Structural only — it reads routing metadata (``interface_path``), never the
    message text. A vessel turn (``interface_path`` starting with
    ``vessel/<world>``) is keyed to ``scope='vessel'``, ``game=<world>``;
    everything else defaults to the neutral personal scope. When Synth passes an
    explicit scope in the action payload it always wins over this fallback
    (handled by the caller).
    """
    ipath = _interface_path_from(context, message).lower()
    if ipath.startswith("vessel/") or ipath == "vessel":
        parts = ipath.split("/", 2)
        game = _coerce_scope(parts[1]) if len(parts) > 1 else SCOPE_NONE
        world = _coerce_scope(parts[2]) if len(parts) > 2 else SCOPE_NONE
        return ("vessel", game, world)
    return (SCOPE_NONE, SCOPE_NONE, SCOPE_NONE)


class GoalsPlugin(PluginBase):
    """Generic self-directed goal plugin.

    Exposes ``goal_set`` / ``goal_list`` / ``goal_update`` as ordinary Fast-Lane
    actions (no ``external_effects``). Goals are scoped by a ``(scope, game,
    world)`` tuple so an in-world objective and a personal life goal coexist.
    Synth may pass the scope explicitly; if it does not, the plugin derives it
    structurally from the turn context.
    """

    display_name = "Goals"

    def __init__(self) -> None:
        super().__init__()
        try:
            from core.core_initializer import register_plugin

            register_plugin("goals", self)
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} register_plugin failed: {exc}")
        log_info(f"{LOG_PREFIX} GoalsPlugin registered")

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "goals",
            "display_name": "Goals",
            "description": (
                "Synth's self-directed goal store: free-text objectives it "
                "authors for itself, scoped to a game world or to its personal "
                "life. Goals persist across sessions and can carry an ordered "
                "plan of sub-steps that auto-completes when finished."
            ),
            "category": "Various",
            "icon": "icon.svg",
            "guide": "guide.md",
            "disable_allowed": True,
        }

    def get_supported_actions(self) -> Dict[str, Any]:
        return {
            "goal_list": {
                "description": (
                    "Recall what you are currently trying to do and the things "
                    "you set out to do before, in this scope. Purely "
                    "informational — use it to remember your own intentions. "
                    "Optionally pass 'scope', 'game' and 'world' to look at a "
                    "specific domain; leave them out to use the current one."
                ),
                "required_fields": [],
                "optional_fields": ["scope", "game", "world"],
                "security_level": "low",
            },
            "goal_set": {
                "description": (
                    "Decide, in your own words, what you want to do right now, "
                    "and make it your goal. There is no fixed list to pick "
                    "from — say whatever you actually feel like doing. Put it in "
                    "'description'. This becomes your single active goal for "
                    "this scope and guides you until you finish or change your "
                    "mind. Just say what you want in 'description' — do NOT try "
                    "to spell out the ordered sub-steps yourself; a separate "
                    "planning pass fills concrete steps in for you shortly "
                    "after. If you are in a game world it is set for you, but "
                    "you may also set 'scope'/'game'/'world' explicitly (use "
                    "'none' for a personal, non-game goal such as writing a "
                    "poem). For a spatial goal you can name a structural target "
                    "with 'target_kind' ('block'/'entity') and 'target_name' "
                    "(the exact id from what you observed)."
                ),
                "required_fields": ["description"],
                "optional_fields": [
                    "note",
                    "scope",
                    "game",
                    "world",
                    "destination_x",
                    "destination_z",
                    "target_kind",
                    "target_name",
                ],
                "security_level": "low",
            },
            "goal_update": {
                "description": (
                    "Reflect on the goal you set for yourself: jot a 'note' on "
                    "how it is going, or set 'status' to 'done' when you feel "
                    "you have achieved it or 'abandoned' if you changed your "
                    "mind. You are the judge of your own progress. If your goal "
                    "has an ordered plan and you just finished the current "
                    "sub-step, set 'advance' to true to move on — when you "
                    "advance past the last step the goal completes "
                    "automatically. You can rewrite the whole plan with a new "
                    "'steps' list, or jump to a step with 'current_step' "
                    "(0-based). Pass 'scope'/'game'/'world' to target a specific "
                    "domain; leave them out to use the current one."
                ),
                "required_fields": [],
                "optional_fields": [
                    "note",
                    "status",
                    "advance",
                    "steps",
                    "current_step",
                    "scope",
                    "game",
                    "world",
                    "destination_x",
                    "destination_z",
                    "target_kind",
                    "target_name",
                ],
                "security_level": "low",
            },
        }

    @staticmethod
    def _resolve_scope(
        payload: Dict[str, Any],
        context: Dict[str, Any] | None,
        message: Any = None,
    ) -> tuple[str, str, str]:
        """Resolve the scope tuple: explicit payload wins, else context fallback.

        Structural only — an explicit ``scope``/``game``/``world`` in the action
        payload always takes precedence; when none is given the tuple is derived
        from the turn context (vessel world, else the neutral personal scope).
        """
        has_explicit = any(
            payload.get(k) not in (None, "") for k in ("scope", "game", "world")
        )
        if has_explicit:
            return (
                _coerce_scope(payload.get("scope")),
                _coerce_scope(payload.get("game")),
                _coerce_scope(payload.get("world")),
            )
        return _scope_from_context(context, message)

    @staticmethod
    def _destination_from_payload(payload: Dict[str, Any]) -> Dict[str, float] | None:
        """Build a ``{x, z}`` destination from flat payload fields, or None."""
        dx = payload.get("destination_x")
        dz = payload.get("destination_z")
        if dx is None and dz is None:
            return None
        dest: Dict[str, Any] = {}
        if dx is not None:
            dest["x"] = dx
        if dz is not None:
            dest["z"] = dz
        return _coerce_destination(dest)

    async def execute_action(
        self,
        action: Dict[str, Any],
        context: Dict[str, Any] | None = None,
        bot: Any = None,
        original_message: Any = None,
    ) -> Dict[str, Any]:
        """Dispatch the ``goal_*`` actions (all fail-safe, Fast Lane).

        Follows the standard plugin action contract: ``action`` carries
        ``type`` and ``payload``; the parser passes an empty ``context`` and the
        originating message, so the scope is resolved from the message routing
        metadata when the payload does not carry an explicit scope.
        """
        action = action or {}
        action_name = str(action.get("type") or "")
        payload = action.get("payload") or {}
        ctx = context or {}
        sc, gm, wd = self._resolve_scope(payload, ctx, original_message)
        # Scope fallback for plain-chat goal actions. ``_resolve_scope`` derives
        # ``(none, none, none)`` for a non-vessel chat (telegram/webui/etc.), but
        # the goal may have been created moments earlier under an EXPLICIT scope
        # tuple (e.g. ``goal_set`` with ``scope="game", game="minecraft"`` from
        # the same chat — the exact mismatch that produced ``no_active_goal`` on
        # the follow-up ``goal_update``). When the neutral scope has no active
        # goal and the caller passed no explicit scope, act on the most recently
        # set active goal in ANY scope. Structural (DB recency), fail-safe (no
        # goal anywhere -> unchanged neutral-scope behaviour), never a keyword
        # parse; vessel turns keep their own scope because their derived tuple
        # is non-neutral.
        if (
            action_name in ("goal_update", "goal_list")
            and (sc, gm, wd) == (SCOPE_NONE, SCOPE_NONE, SCOPE_NONE)
            and not any(
                payload.get(k) not in (None, "") for k in ("scope", "game", "world")
            )
        ):
            active_here = await get_active_goal(scope=sc, game=gm, world=wd)
            if active_here is None:
                latest = await get_most_recent_active_goal()
                if latest is not None:
                    sc = _coerce_scope(latest.get("scope"))
                    gm = _coerce_scope(latest.get("game"))
                    wd = _coerce_scope(latest.get("world"))
                    log_info(
                        f"{LOG_PREFIX} unscoped chat goal action fell back to "
                        f"most recent active goal scope=({sc}/{gm}/{wd})"
                    )
        try:
            if action_name == "goal_list":
                active = await get_active_goal(scope=sc, game=gm, world=wd)
                recent = await list_recent_goals(scope=sc, game=gm, world=wd)
                return {
                    "status": "ok",
                    "current_goal": active,
                    "recent_goals": recent,
                    "scope": {"scope": sc, "game": gm, "world": wd},
                }
            if action_name == "goal_update":
                dest = self._destination_from_payload(payload)
                # Only pass a destination when the payload actually carried one,
                # so an update that doesn't touch travel leaves it untouched.
                dest_arg: Any = dest if dest is not None else None
                if payload.get("destination_x") is None and (
                    payload.get("destination_z") is None
                ):
                    dest_arg = None
                result = await update_active_goal(
                    note=payload.get("note"),
                    status=payload.get("status"),
                    destination=dest_arg,
                    steps=payload.get("steps"),
                    current_step=payload.get("current_step"),
                    advance=bool(payload.get("advance")),
                    target_kind=payload.get("target_kind"),
                    target_name=payload.get("target_name"),
                    scope=sc,
                    game=gm,
                    world=wd,
                )
                return result
            if action_name == "goal_set":
                description = str(payload.get("description") or "").strip()
                if not description:
                    return {
                        "status": "error",
                        "message": "goal_set requires a free-text description",
                    }
                result = await set_goal(
                    description,
                    session_id=payload.get("session_id"),
                    note=payload.get("note"),
                    destination=self._destination_from_payload(payload),
                    target_kind=payload.get("target_kind"),
                    target_name=payload.get("target_name"),
                    scope=sc,
                    game=gm,
                    world=wd,
                )
                return result
            return {"status": "error", "message": f"unknown_action:{action_name}"}
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} action '{action_name}' failed: {exc}")
            return {"status": "error", "message": str(exc)}


PLUGIN_CLASS = GoalsPlugin
