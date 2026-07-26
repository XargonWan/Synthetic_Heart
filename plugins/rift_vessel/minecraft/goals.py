# plugins/rift_vessel/minecraft/goals.py
"""Minecraft self-directed goal store for the Rift Vessel.

Synth is **not** a bot running a fixed quest list. What it wants to do in a
world is something it decides for itself, in its own words, out of its own
personality and mood -- so this module does **not** ship a catalogue of
predefined objectives to pick from. There is no menu of ``gather_wood`` /
``find_diamonds`` templates: two different Synths, or the same Synth on two
different days, should set completely different goals.

The role of this module is deliberately small: **persist and recall** the
free-form goal Synth has set for itself, so a goal survives across decision
beats and across sessions, and Synth can review, update, or drop it. The
*content* of the goal -- and the judgement of whether it has been reached --
stays with cognition (Synth reads its inventory / surroundings via ``inventory``
/ ``observe`` and decides), never with a hard-coded item counter here.

Design notes
------------
* **No hard-coded objectives.** Goals are plain text authored by Synth. This
  module owns lifecycle and storage only, not what the goals should be.
* **Persistent.** Goals live in the ``minecraft_goals`` table (Postgres +
  MariaDB). Time columns are ``created_at`` / ``updated_at`` -- never a bare
  ``timestamp`` (a Postgres reserved word, see AGENTS.md section 12).
* **Fail-safe.** Every DB touch is guarded; a failure degrades to "no goal" and
  never breaks the session or the connector.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from core.db import _get_db_type, get_conn_ctx
from core.logging_utils import log_debug, log_error, log_info

LOG_PREFIX = "[minecraft_goals]"

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
        raw_list: List[Any] = [steps]
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
    """Create the ``minecraft_goals`` table (idempotent, both backends)."""
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
                    CREATE TABLE IF NOT EXISTS minecraft_goals (
                        {id_col},
                        session_id VARCHAR(128),
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
                # IF NOT EXISTS on ADD COLUMN in the versions we target).
                for col_ddl in (
                    "destination TEXT",
                    "steps TEXT",
                    "current_step INT DEFAULT 0",
                    "target_kind VARCHAR(16)",
                    "target_name VARCHAR(64)",
                ):
                    try:
                        await cur.execute(
                            "ALTER TABLE minecraft_goals "
                            f"ADD COLUMN IF NOT EXISTS {col_ddl}"
                        )
                    except Exception as col_exc:  # pragma: no cover - defensive
                        log_debug(
                            f"{LOG_PREFIX} column add skipped ({col_ddl}): {col_exc}"
                        )
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_minecraft_goals_status
                    ON minecraft_goals (status)
                """)
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_minecraft_goals_session
                    ON minecraft_goals (session_id)
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


async def get_active_goal() -> Dict[str, Any] | None:
    """Return the most recently set active goal, or None."""
    await init_goal_table()
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, session_id, description, note, "
                    "destination, steps, current_step, target_kind, "
                    "target_name, status "
                    "FROM minecraft_goals "
                    "WHERE status = %s ORDER BY id DESC LIMIT 1",
                    (STATUS_ACTIVE,),
                )
                row = await cur.fetchone()
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} get_active_goal failed: {exc}")
        return None
    return _row_to_goal(row)


async def list_recent_goals(limit: int = 10) -> List[Dict[str, Any]]:
    """Return recent goals (any status), newest first -- Synth's own history."""
    await init_goal_table()
    try:
        lim = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        lim = 10
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, session_id, description, note, "
                    "destination, steps, current_step, target_kind, "
                    "target_name, status "
                    "FROM minecraft_goals ORDER BY id DESC LIMIT %s",
                    (lim,),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} list_recent_goals failed: {exc}")
        return []
    return [g for g in (_row_to_goal(r) for r in rows or []) if g is not None]


async def list_all_goals(limit: int = 50) -> List[Dict[str, Any]]:
    """Return goals with timestamps for the WebUI Goals view (fail-safe).

    Like :func:`list_recent_goals` but also carries ``created_at`` /
    ``updated_at`` so the WebUI can render per-goal history cards. The active
    goal (if any) sorts first, then the rest newest-first. Read-only; never
    mutates state and degrades to an empty list on any error.
    """
    await init_goal_table()
    try:
        lim = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        lim = 50
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, session_id, description, note, "
                    "destination, steps, current_step, target_kind, "
                    "target_name, status, created_at, updated_at "
                    "FROM minecraft_goals "
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
        if isinstance(row, (tuple, list)):
            if len(row) > 10:
                created_at = row[10]
            if len(row) > 11:
                updated_at = row[11]
            core_row: Any = row[:10]
        elif isinstance(row, dict):
            created_at = row.get("created_at")
            updated_at = row.get("updated_at")
            core_row = row
        else:
            core_row = row
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
                    "SELECT status FROM minecraft_goals WHERE id = %s",
                    (gid,),
                )
                row = await cur.fetchone()
                if row is None:
                    return {"status": "error", "message": "not_found"}
                status = row[0] if isinstance(row, (tuple, list)) else row.get("status")
                if status == STATUS_ACTIVE:
                    return {"status": "error", "message": "cannot_delete_active"}
                await cur.execute("DELETE FROM minecraft_goals WHERE id = %s", (gid,))
                deleted = getattr(cur, "rowcount", 0) or 0
                await conn.commit()
        log_info(f"{LOG_PREFIX} deleted goal id={gid}")
        return {"status": "ok", "deleted_count": deleted}
    except Exception as exc:
        log_error(f"{LOG_PREFIX} delete_goal failed: {exc}")
        return {"status": "error", "message": str(exc)}


async def clear_abandoned_goals() -> Dict[str, Any]:
    """Delete every abandoned goal (fail-safe).

    Only ``abandoned`` goals are removed; the active and done goals are left
    untouched. Returns a status dict with the number of rows deleted.
    """
    await init_goal_table()
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM minecraft_goals WHERE status = %s",
                    (STATUS_ABANDONED,),
                )
                deleted = getattr(cur, "rowcount", 0) or 0
                await conn.commit()
        log_info(f"{LOG_PREFIX} cleared {deleted} abandoned goal(s)")
        return {"status": "ok", "deleted_count": deleted}
    except Exception as exc:
        log_error(f"{LOG_PREFIX} clear_abandoned_goals failed: {exc}")
        return {"status": "error", "message": str(exc)}


async def set_goal(
    description: str,
    session_id: str | None = None,
    note: str | None = None,
    destination: Any = None,
    steps: Any = None,
    target_kind: Any = None,
    target_name: Any = None,
) -> Dict[str, Any]:
    """Adopt a **self-authored** free-text goal as the new active objective.

    ``description`` is whatever Synth decided it wants to do, in its own words --
    there is no catalogue to validate against. ``steps`` is an optional ordered
    list of free-text sub-steps Synth wrote itself to break a bigger objective
    down (e.g. the stages of building a full iron armor set: get wood, make
    tools, mine iron, smelt it, craft the pieces, wear them). The steps are
    Synth's own decomposition -- there is no hard-coded crafting chain here; the
    module only stores the plan and tracks which step is current so progress
    survives across beats. ``destination`` is an optional ``{x, z}`` (plus ``y``)
    coordinate cognition chose to head toward when what the goal needs is not in
    the current area. Any currently-active goal is demoted to abandoned so there
    is a single active objective at a time (Synth focuses on one thing). Returns
    a status dict.
    """
    desc = _clip(description, _MAX_GOAL_CHARS)
    if not desc:
        return {"status": "error", "message": "empty_goal"}
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
                    "UPDATE minecraft_goals SET status = %s, "
                    "updated_at = CURRENT_TIMESTAMP WHERE status = %s",
                    (STATUS_ABANDONED, STATUS_ACTIVE),
                )
                await cur.execute(
                    "INSERT INTO minecraft_goals "
                    "(session_id, description, note, destination, "
                    "steps, current_step, target_kind, target_name, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        session_id,
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
            f"({len(step_list)} step(s), target={target})"
        )
        return {
            "status": "ok",
            "description": desc,
            "destination": dest,
            "steps": step_list,
            "target": target,
        }
    except Exception as exc:
        log_error(f"{LOG_PREFIX} set_goal failed: {exc}")
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
) -> Dict[str, Any]:
    """Let Synth annotate progress on, complete, drop, re-aim, or re-plan its goal.

    ``note`` records how it is going in Synth's own words; ``status`` may be set
    to ``done`` (Synth judged it achieved) or ``abandoned`` (changed its mind).
    ``destination`` re-aims where the body heads. ``steps`` replaces the whole
    ordered plan (when Synth reworks its own decomposition) and resets the
    current step to 0. ``current_step`` jumps the plan pointer to an explicit
    0-based index (clamped). ``advance`` moves the pointer to the next step --
    this is how Synth records "I finished the current sub-step". Progress is
    Synth's own judgement -- there is no automatic item counter; the code only
    remembers where in its self-authored plan Synth said it is. When a field is
    left as ``None`` / default it is kept untouched.
    """
    goal = await get_active_goal()
    if goal is None:
        return {"status": "error", "message": "no_active_goal"}
    new_status = status if status in (STATUS_DONE, STATUS_ABANDONED) else STATUS_ACTIVE
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
                    f"UPDATE minecraft_goals SET {', '.join(sets)} WHERE id = %s",
                    tuple(params),
                )
                await conn.commit()
        if new_status == STATUS_DONE:
            log_info(f"{LOG_PREFIX} goal marked done by Synth")
        elif advance:
            log_info(f"{LOG_PREFIX} advanced to plan step {new_idx}/{total_steps}")
        return {
            "status": "ok",
            "goal_status": new_status,
            "current_step": new_idx,
            "steps_total": total_steps,
            "target": new_target if change_target else goal.get("target"),
        }
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} update_active_goal failed: {exc}")
        return {"status": "error", "message": str(exc)}


def _row_to_goal(row: Any) -> Dict[str, Any] | None:
    """Normalize a DB row (tuple or dict cursor) into a goal dict."""
    if not row:
        return None
    if isinstance(row, (tuple, list)):
        keys = (
            "id",
            "session_id",
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
