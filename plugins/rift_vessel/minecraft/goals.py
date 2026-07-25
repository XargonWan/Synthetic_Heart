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

_table_initialized = False


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
                        status VARCHAR(32) DEFAULT '{STATUS_ACTIVE}',
                        {created_col},
                        {updated_col}
                    )
                """)
                # Idempotent add for pre-existing tables (both backends support
                # IF NOT EXISTS on ADD COLUMN in the versions we target).
                try:
                    await cur.execute(
                        "ALTER TABLE minecraft_goals "
                        "ADD COLUMN IF NOT EXISTS destination TEXT"
                    )
                except Exception as col_exc:  # pragma: no cover - defensive
                    log_debug(f"{LOG_PREFIX} destination column add skipped: {col_exc}")
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
                    "destination, status "
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
                    "destination, status "
                    "FROM minecraft_goals ORDER BY id DESC LIMIT %s",
                    (lim,),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} list_recent_goals failed: {exc}")
        return []
    return [g for g in (_row_to_goal(r) for r in rows or []) if g is not None]


async def set_goal(
    description: str,
    session_id: str | None = None,
    note: str | None = None,
    destination: Any = None,
) -> Dict[str, Any]:
    """Adopt a **self-authored** free-text goal as the new active objective.

    ``description`` is whatever Synth decided it wants to do, in its own words --
    there is no catalogue to validate against. ``destination`` is an optional
    ``{x, z}`` (plus ``y``) coordinate cognition chose to head toward when what
    the goal needs is not in the current area (e.g. leaving a treeless desert to
    find a forest); the motor tick walks there structurally. Any currently-active
    goal is demoted to abandoned so there is a single active objective at a time
    (Synth focuses on one thing). Returns a status dict.
    """
    desc = _clip(description, _MAX_GOAL_CHARS)
    if not desc:
        return {"status": "error", "message": "empty_goal"}
    note_txt = _clip(note, _MAX_NOTE_CHARS) or None
    dest = _coerce_destination(destination)
    dest_json = json.dumps(dest) if dest else None
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
                    "(session_id, description, note, destination, status) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (session_id, desc, note_txt, dest_json, STATUS_ACTIVE),
                )
                await conn.commit()
        log_info(f"{LOG_PREFIX} adopted self-authored goal: {desc!r}")
        return {"status": "ok", "description": desc, "destination": dest}
    except Exception as exc:
        log_error(f"{LOG_PREFIX} set_goal failed: {exc}")
        return {"status": "error", "message": str(exc)}


async def update_active_goal(
    note: str | None = None,
    status: str | None = None,
    destination: Any = None,
) -> Dict[str, Any]:
    """Let Synth annotate progress on, complete, drop, or re-aim its active goal.

    ``note`` records how it is going in Synth's own words; ``status`` may be set
    to ``done`` (Synth judged it achieved) or ``abandoned`` (changed its mind).
    ``destination`` re-aims where the body heads: pass a ``{x, z}`` coordinate to
    set/replace the travel target (e.g. Synth realises it must go further to
    change biome), or an explicit empty dict / falsy value to clear it. When
    ``destination`` is left as ``None`` the existing travel target is kept
    untouched. Progress is Synth's own judgement -- there is no automatic item
    counter.
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
                params.append(goal.get("id"))
                await cur.execute(
                    f"UPDATE minecraft_goals SET {', '.join(sets)} WHERE id = %s",
                    tuple(params),
                )
                await conn.commit()
        if new_status == STATUS_DONE:
            log_info(f"{LOG_PREFIX} goal marked done by Synth")
        return {"status": "ok", "goal_status": new_status}
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} update_active_goal failed: {exc}")
        return {"status": "error", "message": str(exc)}


def _row_to_goal(row: Any) -> Dict[str, Any] | None:
    """Normalize a DB row (tuple or dict cursor) into a goal dict."""
    if not row:
        return None
    if isinstance(row, (tuple, list)):
        keys = ("id", "session_id", "description", "note", "destination", "status")
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
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "description": row.get("description"),
        "note": row.get("note"),
        "destination": destination,
        "status": row.get("status"),
    }
