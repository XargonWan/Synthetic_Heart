# plugins/rift_vessel/vessel_quests.py
"""Generic scope-aware quest store + kill-tracker for the Rift Vessel.

A **quest** is a *directed* milestone Synth is working toward in a world — one
step of an ordered questline (e.g. "build your first base" → "craft a bed" →
… → "defeat the Ender Dragon"). Having a sense of *direction* is common to the
great majority of game/virtual worlds, so — per the Scope rule (AGENTS.md
§5c) — the quest **store + mechanism** live in the Rift Vessel **core**, while
the concrete *questline content* (the ordered list of Minecraft milestones and
their structural objectives) is world-specific and owned by the adapter.

Relationship to goals
----------------------
Quests and goals are complementary, not the same thing:

* A **goal** (:mod:`plugins.goals.goals`) is what Synth *spontaneously authors
  for itself right now* — free text, its own will.
* A **quest** is a longer-arc, ordered *milestone* the adapter registers. The
  active quest is surfaced to cognition **only as reference** — "here is the
  milestone you are working toward" — exactly like the knowledge base. Synth
  still authors its own goal freely and may bind it to the quest or not. The
  quest is **never** an engine that auto-executes steps (the spontaneity rule).

Only **one** quest is ``active`` per scope tuple at a time (like the active
goal). When its objectives are all satisfied it is marked ``done`` and the next
quest by ``order_index`` becomes ``active``.

Objectives & the kill-tracker
------------------------------
Each quest carries a list of structural **objectives**, each a dict::

    {"kind": "have_item"|"reach_dimension"|"kill"|"has_base"|"has_bed",
     "target": <game id or None>, "count": <int>}

Objectives are matched **structurally** — against the id→count inventory map,
the current dimension id, the base store, or a per-mob kill counter — never
against free text (the no-keyword rule is about natural-language intent, not
structural game ids). The kill-tracker persists per-quest kill progress so a
"defeat N of mob X" objective (WoW-style) advances as the adapter reports
kills via :func:`record_kill`.

Design notes
------------
* **Scope tuple.** ``scope`` / ``game`` / ``world`` identical to the goal/base
  stores. Minecraft quests are pinned ``scope="vessel"`` / ``game="minecraft"``
  by the Minecraft shim.
* **Persistent.** Quests live in the ``vessel_quests`` table (Postgres +
  MariaDB). Time columns are ``created_at`` / ``updated_at`` — never a bare
  ``timestamp`` (a Postgres reserved word, see AGENTS.md §12).
* **Fail-safe.** Every DB touch is guarded; a failure degrades to "no quest"
  and never breaks the caller.
"""

from __future__ import annotations

import json
import traceback
from typing import Any, Dict, List

from core.db import _get_db_type, get_conn_ctx
from core.logging_utils import log_debug, log_error, log_info

LOG_PREFIX = "[vessel_quests]"

# Quest lifecycle states.
STATUS_LOCKED = "locked"
STATUS_ACTIVE = "active"
STATUS_DONE = "done"

# Objective kinds (structural — never matched against free text).
OBJ_HAVE_ITEM = "have_item"
OBJ_REACH_DIMENSION = "reach_dimension"
OBJ_KILL = "kill"
OBJ_HAS_BASE = "has_base"
OBJ_HAS_BED = "has_bed"
_OBJECTIVE_KINDS = frozenset(
    {OBJ_HAVE_ITEM, OBJ_REACH_DIMENSION, OBJ_KILL, OBJ_HAS_BASE, OBJ_HAS_BED}
)

# Guardrails only — cap lengths so a runaway registration cannot write an
# unbounded blob into the DB.
_MAX_QUEST_ID_CHARS = 64
_MAX_TITLE_CHARS = 200
_MAX_DESC_CHARS = 2000
_MAX_TARGET_CHARS = 64
_MAX_OBJECTIVES = 16

# Default scope value for every level.
SCOPE_NONE = "none"
_MAX_SCOPE_CHARS = 64

_table_initialized = False


def _clip(text: Any, limit: int) -> str:
    """Coerce to a trimmed, length-capped string."""
    s = str(text or "").strip()
    if len(s) > limit:
        s = s[:limit].rstrip()
    return s


def _coerce_scope(value: Any) -> str:
    """Normalise a single scope level into a trimmed, lower-case token."""
    s = str(value or "").strip().lower()
    if not s:
        return SCOPE_NONE
    if len(s) > _MAX_SCOPE_CHARS:
        s = s[:_MAX_SCOPE_CHARS].rstrip()
    return s or SCOPE_NONE


def _coerce_int(value: Any, default: int = 1) -> int:
    """Coerce to a non-negative int, fail-safe."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def _coerce_objective(value: Any) -> Dict[str, Any] | None:
    """Normalise one objective dict into a structural shape or ``None``.

    Shape::

        {"kind": <one of _OBJECTIVE_KINDS>,
         "target": <game id str or None>,
         "count": <int >= 1>}

    Purely structural — the ``target`` is a canonical game id (item/dimension/
    mob), never inspected as free text.
    """
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "").strip().lower()
    if kind not in _OBJECTIVE_KINDS:
        return None
    target_raw = value.get("target")
    target: str | None
    if target_raw is None:
        target = None
    else:
        target = _clip(target_raw, _MAX_TARGET_CHARS).lower() or None
    count = _coerce_int(value.get("count"), default=1)
    if count < 1:
        count = 1
    return {"kind": kind, "target": target, "count": count}


def _coerce_objectives(value: Any) -> List[Dict[str, Any]]:
    """Normalise a list of objectives (fail-safe, capped)."""
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in value[:_MAX_OBJECTIVES]:
        obj = _coerce_objective(item)
        if obj is not None:
            out.append(obj)
    return out


async def init_quest_table() -> None:
    """Create the ``vessel_quests`` table (idempotent, both backends)."""
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
                    CREATE TABLE IF NOT EXISTS vessel_quests (
                        {id_col},
                        scope VARCHAR(64) DEFAULT '{SCOPE_NONE}',
                        game VARCHAR(64) DEFAULT '{SCOPE_NONE}',
                        world VARCHAR(64) DEFAULT '{SCOPE_NONE}',
                        quest_id VARCHAR(64) NOT NULL,
                        title VARCHAR(200) NOT NULL,
                        description TEXT,
                        order_index INT DEFAULT 0,
                        status VARCHAR(32) DEFAULT '{STATUS_LOCKED}',
                        objectives TEXT,
                        progress TEXT,
                        {created_col},
                        {updated_col}
                    )
                """)
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vessel_quests_scope
                    ON vessel_quests (scope, game, world)
                """)
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vessel_quests_status
                    ON vessel_quests (status)
                """)
                # Deduplicate any pre-existing rows sharing the identity tuple
                # before enforcing uniqueness (older installs / concurrent
                # connects could insert duplicates without the UNIQUE index).
                # Keep the "best" row per identity: farthest status
                # (done > active > locked), then most-progressed, then lowest
                # id; delete the rest. Fail-safe per-backend.
                try:
                    if is_postgres:
                        await cur.execute("""
                            DELETE FROM vessel_quests v USING (
                                SELECT id, ROW_NUMBER() OVER (
                                    PARTITION BY scope, game, world, quest_id
                                    ORDER BY
                                        CASE status
                                            WHEN 'done' THEN 0
                                            WHEN 'active' THEN 1
                                            ELSE 2
                                        END ASC,
                                        LENGTH(COALESCE(progress, '')) DESC,
                                        id ASC
                                ) AS rn
                                FROM vessel_quests
                            ) d
                            WHERE v.id = d.id AND d.rn > 1
                        """)
                    else:
                        await cur.execute("""
                            DELETE v FROM vessel_quests v
                            JOIN vessel_quests keep
                              ON v.scope = keep.scope
                             AND v.game = keep.game
                             AND v.world = keep.world
                             AND v.quest_id = keep.quest_id
                             AND v.id > keep.id
                        """)
                    await conn.commit()
                except Exception as dedup_exc:
                    log_debug(f"{LOG_PREFIX} quest dedup skipped: {dedup_exc}")
                # Enforce a single row per identity tuple going forward so
                # repeated/concurrent registration can never duplicate.
                await cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_vessel_quests_identity
                    ON vessel_quests (scope, game, world, quest_id)
                """)
                await conn.commit()
        _table_initialized = True
        log_info(f"{LOG_PREFIX} DB table initialized")
    except Exception as exc:
        log_error(f"{LOG_PREFIX} failed to init quest table: {exc}")


# ----------------------------------------------------------------------
# Persistence helpers (all fail-safe)
# ----------------------------------------------------------------------

_QUEST_COLS = (
    "id, scope, game, world, quest_id, title, description, "
    "order_index, status, objectives, progress"
)


def _parse_json_field(raw: Any, default: Any) -> Any:
    """Parse a JSON TEXT column (dict/list already-parsed passthrough)."""
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default
    return default


def _row_to_quest(row: Any) -> Dict[str, Any] | None:
    """Normalize a DB row (tuple or dict cursor) into a quest dict."""
    if not row:
        return None
    if isinstance(row, (tuple, list)):
        keys = (
            "id",
            "scope",
            "game",
            "world",
            "quest_id",
            "title",
            "description",
            "order_index",
            "status",
            "objectives",
            "progress",
        )
        row = dict(zip(keys, row))
    objectives = _coerce_objectives(_parse_json_field(row.get("objectives"), []))
    progress = _parse_json_field(row.get("progress"), {})
    if not isinstance(progress, dict):
        progress = {}
    return {
        "id": row.get("id"),
        "scope": row.get("scope") or SCOPE_NONE,
        "game": row.get("game") or SCOPE_NONE,
        "world": row.get("world") or SCOPE_NONE,
        "quest_id": row.get("quest_id"),
        "title": row.get("title"),
        "description": row.get("description"),
        "order_index": _coerce_int(row.get("order_index"), default=0),
        "status": row.get("status") or STATUS_LOCKED,
        "objectives": objectives,
        "progress": progress,
    }


async def register_quests(
    quests: List[Dict[str, Any]],
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
) -> Dict[str, Any]:
    """Idempotently register an ordered questline for a scope tuple.

    Each entry is a dict::

        {"quest_id": str, "title": str, "description": str,
         "order_index": int, "objectives": [<objective>, ...]}

    A quest already present (same ``scope/game/world`` + ``quest_id``) is
    **updated in place** — its title/description/order/objectives are refreshed
    but its ``status`` and ``progress`` are preserved so re-registering the
    questline at every connect never resets progress. A brand-new quest is
    inserted ``locked``, except the lowest ``order_index`` quest which, when no
    quest is yet ``active`` for the scope, is promoted to ``active`` so there is
    always a current milestone. Fully fail-safe.

    Returns ``{"status": "ok", "registered": N, "active": <quest_id|None>}``.
    """
    await init_quest_table()
    sc, gm, wd = _coerce_scope(scope), _coerce_scope(game), _coerce_scope(world)
    normalised: List[Dict[str, Any]] = []
    for q in quests or []:
        if not isinstance(q, dict):
            continue
        qid = _clip(q.get("quest_id"), _MAX_QUEST_ID_CHARS).lower()
        title = _clip(q.get("title"), _MAX_TITLE_CHARS)
        if not qid or not title:
            continue
        normalised.append(
            {
                "quest_id": qid,
                "title": title,
                "description": _clip(q.get("description"), _MAX_DESC_CHARS) or None,
                "order_index": _coerce_int(q.get("order_index"), default=0),
                "objectives": _coerce_objectives(q.get("objectives")),
            }
        )
    if not normalised:
        return {"status": "ok", "registered": 0, "active": None}

    registered = 0
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                for q in normalised:
                    await cur.execute(
                        "SELECT id FROM vessel_quests "
                        "WHERE scope = %s AND game = %s AND world = %s "
                        "AND quest_id = %s ORDER BY id DESC LIMIT 1",
                        (sc, gm, wd, q["quest_id"]),
                    )
                    row = await cur.fetchone()
                    existing_id = None
                    if row is not None:
                        existing_id = (
                            row[0] if isinstance(row, (tuple, list)) else row.get("id")
                        )
                    obj_json = json.dumps(q["objectives"])
                    if existing_id is not None:
                        # Preserve status + progress; refresh definition only.
                        await cur.execute(
                            "UPDATE vessel_quests SET title = %s, "
                            "description = %s, order_index = %s, "
                            "objectives = %s, updated_at = CURRENT_TIMESTAMP "
                            "WHERE id = %s",
                            (
                                q["title"],
                                q["description"],
                                q["order_index"],
                                obj_json,
                                existing_id,
                            ),
                        )
                    else:
                        try:
                            await cur.execute(
                                "INSERT INTO vessel_quests "
                                "(scope, game, world, quest_id, title, "
                                "description, order_index, status, objectives, "
                                "progress) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                                (
                                    sc,
                                    gm,
                                    wd,
                                    q["quest_id"],
                                    q["title"],
                                    q["description"],
                                    q["order_index"],
                                    STATUS_LOCKED,
                                    obj_json,
                                    json.dumps({}),
                                ),
                            )
                        except Exception:
                            # A concurrent connect inserted the same identity
                            # first; the UNIQUE index rejected this INSERT.
                            # Fall back to refreshing the definition in place.
                            await conn.rollback()
                            await cur.execute(
                                "UPDATE vessel_quests SET title = %s, "
                                "description = %s, order_index = %s, "
                                "objectives = %s, updated_at = CURRENT_TIMESTAMP "
                                "WHERE scope = %s AND game = %s AND world = %s "
                                "AND quest_id = %s",
                                (
                                    q["title"],
                                    q["description"],
                                    q["order_index"],
                                    obj_json,
                                    sc,
                                    gm,
                                    wd,
                                    q["quest_id"],
                                ),
                            )
                    registered += 1

                # Promote the lowest-order not-done quest to active if none is.
                await cur.execute(
                    "SELECT COUNT(*) FROM vessel_quests "
                    "WHERE scope = %s AND game = %s AND world = %s "
                    "AND status = %s",
                    (sc, gm, wd, STATUS_ACTIVE),
                )
                cnt_row = await cur.fetchone()
                active_count = (
                    cnt_row[0]
                    if isinstance(cnt_row, (tuple, list))
                    else (cnt_row.get("count") if cnt_row else 0)
                ) or 0
                if not active_count:
                    await cur.execute(
                        "UPDATE vessel_quests SET status = %s, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ("
                        "SELECT id FROM vessel_quests "
                        "WHERE scope = %s AND game = %s AND world = %s "
                        "AND status != %s "
                        "ORDER BY order_index ASC, id ASC LIMIT 1)",
                        (STATUS_ACTIVE, sc, gm, wd, STATUS_DONE),
                    )
                await conn.commit()
        active = await get_active_quest(scope=sc, game=gm, world=wd)
        log_info(
            f"{LOG_PREFIX} registered {registered} quest(s) "
            f"(scope={sc}/{gm}/{wd}, active={active.get('quest_id') if active else None})"
        )
        return {
            "status": "ok",
            "registered": registered,
            "active": active.get("quest_id") if active else None,
        }
    except Exception as exc:
        log_error(
            f"{LOG_PREFIX} register_quests failed: {exc}\n{traceback.format_exc()}"
        )
        return {"status": "error", "message": str(exc)}


async def get_active_quest(
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
) -> Dict[str, Any] | None:
    """Return the single active quest for a scope tuple, or ``None`` (fail-safe)."""
    await init_quest_table()
    sc, gm, wd = _coerce_scope(scope), _coerce_scope(game), _coerce_scope(world)
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_QUEST_COLS} FROM vessel_quests "
                    "WHERE scope = %s AND game = %s AND world = %s AND status = %s "
                    "ORDER BY order_index ASC, id ASC LIMIT 1",
                    (sc, gm, wd, STATUS_ACTIVE),
                )
                row = await cur.fetchone()
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} get_active_quest failed: {exc}")
        return None
    return _row_to_quest(row)


async def list_quests(
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return the questline for a scope tuple ordered by ``order_index`` (fail-safe)."""
    await init_quest_table()
    sc, gm, wd = _coerce_scope(scope), _coerce_scope(game), _coerce_scope(world)
    try:
        lim = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        lim = 100
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_QUEST_COLS} FROM vessel_quests "
                    "WHERE scope = %s AND game = %s AND world = %s "
                    "ORDER BY order_index ASC, id ASC LIMIT %s",
                    (sc, gm, wd, lim),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} list_quests failed: {exc}")
        return []
    return [q for q in (_row_to_quest(r) for r in rows or []) if q is not None]


async def complete_quest(
    quest_id: str,
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
) -> Dict[str, Any]:
    """Mark ``quest_id`` done and promote the next locked quest to active.

    Returns ``{"status": "ok", "completed": quest_id, "next": <quest_id|None>}``.
    Fully fail-safe.
    """
    await init_quest_table()
    sc, gm, wd = _coerce_scope(scope), _coerce_scope(game), _coerce_scope(world)
    qid = _clip(quest_id, _MAX_QUEST_ID_CHARS).lower()
    if not qid:
        return {"status": "error", "message": "empty_quest_id"}
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT order_index FROM vessel_quests "
                    "WHERE scope = %s AND game = %s AND world = %s AND quest_id = %s "
                    "ORDER BY id DESC LIMIT 1",
                    (sc, gm, wd, qid),
                )
                row = await cur.fetchone()
                if row is None:
                    return {"status": "error", "message": "unknown_quest"}
                order_index = (
                    row[0] if isinstance(row, (tuple, list)) else row.get("order_index")
                )
                await cur.execute(
                    "UPDATE vessel_quests SET status = %s, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE scope = %s AND game = %s AND world = %s AND quest_id = %s",
                    (STATUS_DONE, sc, gm, wd, qid),
                )
                # Promote the next quest by order (if none already active).
                await cur.execute(
                    "SELECT COUNT(*) FROM vessel_quests "
                    "WHERE scope = %s AND game = %s AND world = %s AND status = %s",
                    (sc, gm, wd, STATUS_ACTIVE),
                )
                cnt_row = await cur.fetchone()
                active_count = (
                    cnt_row[0]
                    if isinstance(cnt_row, (tuple, list))
                    else (cnt_row.get("count") if cnt_row else 0)
                ) or 0
                next_qid: str | None = None
                if not active_count:
                    await cur.execute(
                        f"SELECT {_QUEST_COLS} FROM vessel_quests "
                        "WHERE scope = %s AND game = %s AND world = %s "
                        "AND status != %s AND order_index > %s "
                        "ORDER BY order_index ASC, id ASC LIMIT 1",
                        (sc, gm, wd, STATUS_DONE, order_index),
                    )
                    nxt = await cur.fetchone()
                    nxt_quest = _row_to_quest(nxt)
                    if nxt_quest is not None:
                        next_qid = nxt_quest.get("quest_id")
                        await cur.execute(
                            "UPDATE vessel_quests SET status = %s, "
                            "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                            (STATUS_ACTIVE, nxt_quest.get("id")),
                        )
                await conn.commit()
        log_info(f"{LOG_PREFIX} completed quest {qid!r}, next={next_qid}")
        return {"status": "ok", "completed": qid, "next": next_qid}
    except Exception as exc:
        log_error(f"{LOG_PREFIX} complete_quest failed: {exc}")
        return {"status": "error", "message": str(exc)}


async def record_kill(
    mob_kind: Any,
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
    amount: int = 1,
) -> Dict[str, Any]:
    """Record ``amount`` kills of ``mob_kind`` against the active quest.

    Increments the per-mob kill counter in the active quest's ``progress`` for
    any ``kind == "kill"`` objective whose ``target`` matches ``mob_kind`` (a
    canonical mob id — structural, never free text). A ``target`` of ``None``
    (any mob) always matches. Returns a small status dict; fully fail-safe and
    a no-op when there is no active quest or no matching objective.
    """
    mob = _clip(mob_kind, _MAX_TARGET_CHARS).lower()
    if not mob:
        return {"status": "noop", "reason": "empty_mob"}
    delta = _coerce_int(amount, default=1)
    if delta < 1:
        delta = 1
    active = await get_active_quest(scope=scope, game=game, world=world)
    if active is None:
        return {"status": "noop", "reason": "no_active_quest"}
    kill_objs = [
        o
        for o in active.get("objectives", [])
        if o.get("kind") == OBJ_KILL and (o.get("target") in (None, mob))
    ]
    if not kill_objs:
        return {"status": "noop", "reason": "no_matching_objective"}
    progress = dict(active.get("progress") or {})
    kills = dict(progress.get("kills") or {})
    kills[mob] = _coerce_int(kills.get(mob), default=0) + delta
    progress["kills"] = kills
    updated = await _write_progress(
        active.get("id"), progress, scope=scope, game=game, world=world
    )
    return {
        "status": "ok" if updated else "error",
        "mob": mob,
        "count": kills[mob],
    }


async def _write_progress(
    quest_id_pk: Any,
    progress: Dict[str, Any],
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
) -> bool:
    """Persist a quest's ``progress`` JSON by primary-key id (fail-safe)."""
    if quest_id_pk is None:
        return False
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE vessel_quests SET progress = %s, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (json.dumps(progress), quest_id_pk),
                )
                await conn.commit()
        return True
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} _write_progress failed: {exc}")
        return False


def evaluate_quest_objectives(
    quest: Dict[str, Any] | None,
    inventory_counts: Dict[str, int] | None = None,
    dimension: Any = None,
    has_base: bool = False,
    has_bed: bool = False,
) -> Dict[str, Any]:
    """Judge which of ``quest``'s objectives are satisfied (pure, structural).

    Matches each objective against the supplied structural facts only — the
    id→count inventory map, the current dimension id, and the base/bed flags,
    plus the persisted per-mob ``progress["kills"]`` counter for ``kill``
    objectives. Never inspects free text. Returns::

        {"complete": bool,
         "satisfied": [<objective>, ...],
         "pending": [<objective>, ...]}

    ``complete`` is ``True`` only when every objective is satisfied (an empty
    objective list counts as complete). Fully fail-safe.
    """
    if not isinstance(quest, dict):
        return {"complete": False, "satisfied": [], "pending": []}
    objectives = quest.get("objectives") or []
    if not objectives:
        return {"complete": True, "satisfied": [], "pending": []}
    counts: Dict[str, int] = (
        inventory_counts if isinstance(inventory_counts, dict) else {}
    )
    dim = str(dimension or "").strip().lower()
    progress = quest.get("progress") or {}
    kills = progress.get("kills") if isinstance(progress, dict) else {}
    if not isinstance(kills, dict):
        kills = {}

    satisfied: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    for obj in objectives:
        if not isinstance(obj, dict):
            continue
        kind = obj.get("kind")
        target = obj.get("target")
        need = _coerce_int(obj.get("count"), default=1)
        ok = False
        if kind == OBJ_HAVE_ITEM and target:
            ok = _coerce_int(counts.get(target), default=0) >= need
        elif kind == OBJ_REACH_DIMENSION and target:
            ok = dim == str(target).strip().lower()
        elif kind == OBJ_HAS_BASE:
            ok = bool(has_base)
        elif kind == OBJ_HAS_BED:
            ok = bool(has_bed) or _coerce_int(counts.get("bed"), default=0) >= 1
        elif kind == OBJ_KILL:
            if target:
                ok = _coerce_int(kills.get(str(target).lower()), default=0) >= need
            else:
                total = sum(_coerce_int(v, default=0) for v in kills.values())
                ok = total >= need
        (satisfied if ok else pending).append(obj)
    return {
        "complete": len(pending) == 0,
        "satisfied": satisfied,
        "pending": pending,
    }
