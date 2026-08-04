# plugins/rift_vessel/vessel_bases.py
"""Generic scope-aware base (home) store for the Rift Vessel.

A **base** is a place Synth chose in a world where it builds something,
stores resources, shelters at night, sleeps, or sets its respawn. Having a
base is common to the great majority of game/virtual worlds (a home in
Minecraft, a settlement in a survival world, a room in a social world), so —
per the Scope rule (AGENTS.md section 5c) — the *store* lives in the Rift
Vessel **core**, while the concrete act of *building* a base and what a base
looks like is world-specific and owned by the adapter.

Like :mod:`plugins.goals.goals`, this store is deliberately small: it
**persists and recalls** the bases Synth registers for itself. A world can
have several bases (a home, a mine, an outpost), so the store keeps a list per
scope tuple. There is **no catalogue** of predefined bases — Synth decides what
counts as a base.

Every base carries a three-level structural **scope** identical to the goal
store: ``scope`` / ``game`` / ``world``. Minecraft bases are pinned
``scope="vessel"`` / ``game="minecraft"`` / ``world=<server slug>`` by the
Minecraft shim (:mod:`plugins.rift_vessel.minecraft.bases`).

Design notes
------------
* **No hard-coded bases.** A base is a name + coordinates Synth registered.
* **Coordinates are structural, never text.** A base carries a *point*
  ``{x, y, z}`` (used for the night-retreat reflex) and optionally a *bounding
  box* ``{x1, y1, z1, x2, y2, z2}`` (used to describe the built structure).
  Both are pure numbers — this module never inspects any free text.
* **Persistent.** Bases live in the ``vessel_bases`` table (Postgres + MariaDB).
  Time columns are ``created_at`` / ``updated_at`` — never a bare ``timestamp``
  (a Postgres reserved word, see AGENTS.md section 12).
* **Fail-safe.** Every DB touch is guarded; a failure degrades to "no bases"
  and never breaks the caller.
"""

from __future__ import annotations

import json
import math
import traceback
from typing import Any, Dict, List

from core.db import _get_db_type, get_conn_ctx
from core.logging_utils import log_debug, log_error, log_info

LOG_PREFIX = "[vessel_bases]"

# Base lifecycle states.
STATUS_ACTIVE = "active"
STATUS_ABANDONED = "abandoned"

# Guardrails only — a self-registered base is free text, we just cap lengths so
# a runaway model cannot write an unbounded blob into the DB.
_MAX_NAME_CHARS = 120
_MAX_KIND_CHARS = 32
_MAX_NOTE_CHARS = 1000

# Default scope value for every level. A three-part scope tuple keys a base to a
# domain/game/world; "none" is the neutral value.
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
    """Normalise a single scope level into a trimmed, lower-case token.

    Empty / ``None`` degrades to :data:`SCOPE_NONE`. This never inspects any
    free text — it only sanitises one structural scope level.
    """
    s = str(value or "").strip().lower()
    if not s:
        return SCOPE_NONE
    if len(s) > _MAX_SCOPE_CHARS:
        s = s[:_MAX_SCOPE_CHARS].rstrip()
    return s or SCOPE_NONE


def _coerce_point(value: Any) -> Dict[str, float] | None:
    """Normalise a base anchor point into ``{x, y, z}`` or ``None``.

    Accepts a dict with numeric ``x`` / ``y`` / ``z``. All three axes are
    required for a usable point (the vertical component matters for the retreat
    reflex). Purely numeric — never inspects text.
    """
    if not isinstance(value, dict):
        return None
    out: Dict[str, float] = {}
    for axis in ("x", "y", "z"):
        raw = value.get(axis)
        if raw is None:
            continue
        try:
            out[axis] = float(raw)
        except (TypeError, ValueError):
            continue
    if not {"x", "y", "z"} <= out.keys():
        return None
    return out


def _coerce_box(value: Any) -> Dict[str, float] | None:
    """Normalise a base bounding box into ``{x1,y1,z1,x2,y2,z2}`` or ``None``.

    Accepts a dict with the six numeric corner components. All six are required.
    Purely numeric — never inspects text.
    """
    if not isinstance(value, dict):
        return None
    out: Dict[str, float] = {}
    for axis in ("x1", "y1", "z1", "x2", "y2", "z2"):
        raw = value.get(axis)
        if raw is None:
            continue
        try:
            out[axis] = float(raw)
        except (TypeError, ValueError):
            continue
    if len(out) != 6:
        return None
    return out


async def init_base_table() -> None:
    """Create the ``vessel_bases`` table (idempotent, both backends)."""
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
                    CREATE TABLE IF NOT EXISTS vessel_bases (
                        {id_col},
                        session_id VARCHAR(128),
                        scope VARCHAR(64) DEFAULT '{SCOPE_NONE}',
                        game VARCHAR(64) DEFAULT '{SCOPE_NONE}',
                        world VARCHAR(64) DEFAULT '{SCOPE_NONE}',
                        name VARCHAR(120) NOT NULL,
                        kind VARCHAR(32) DEFAULT 'home',
                        anchor TEXT,
                        box TEXT,
                        note TEXT,
                        status VARCHAR(32) DEFAULT '{STATUS_ACTIVE}',
                        {created_col},
                        {updated_col}
                    )
                """)
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vessel_bases_status
                    ON vessel_bases (status)
                """)
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vessel_bases_scope
                    ON vessel_bases (scope, game, world)
                """)
                await conn.commit()
        _table_initialized = True
        log_info(f"{LOG_PREFIX} DB table initialized")
    except Exception as exc:
        log_error(f"{LOG_PREFIX} failed to init base table: {exc}")


# ----------------------------------------------------------------------
# Persistence helpers (all fail-safe)
# ----------------------------------------------------------------------

# The full column list every read selects (order matters for tuple cursors).
_BASE_COLS = "id, session_id, scope, game, world, name, kind, anchor, box, note, status"


def _row_to_base(row: Any) -> Dict[str, Any] | None:
    """Normalize a DB row (tuple or dict cursor) into a base dict."""
    if not row:
        return None
    if isinstance(row, (tuple, list)):
        keys = (
            "id",
            "session_id",
            "scope",
            "game",
            "world",
            "name",
            "kind",
            "anchor",
            "box",
            "note",
            "status",
        )
        row = dict(zip(keys, row))
    raw_anchor = row.get("anchor")
    anchor: Dict[str, float] | None = None
    if isinstance(raw_anchor, dict):
        anchor = _coerce_point(raw_anchor)
    elif isinstance(raw_anchor, str) and raw_anchor.strip():
        try:
            anchor = _coerce_point(json.loads(raw_anchor))
        except (ValueError, TypeError):
            anchor = None
    raw_box = row.get("box")
    box: Dict[str, float] | None = None
    if isinstance(raw_box, dict):
        box = _coerce_box(raw_box)
    elif isinstance(raw_box, str) and raw_box.strip():
        try:
            box = _coerce_box(json.loads(raw_box))
        except (ValueError, TypeError):
            box = None
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "scope": row.get("scope") or SCOPE_NONE,
        "game": row.get("game") or SCOPE_NONE,
        "world": row.get("world") or SCOPE_NONE,
        "name": row.get("name"),
        "kind": row.get("kind") or "home",
        "anchor": anchor,
        "box": box,
        "note": row.get("note"),
        "status": row.get("status"),
    }


async def list_bases(
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return active bases for a scope tuple, newest first (fail-safe)."""
    await init_base_table()
    sc, gm, wd = _coerce_scope(scope), _coerce_scope(game), _coerce_scope(world)
    try:
        lim = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        lim = 50
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_BASE_COLS} FROM vessel_bases "
                    "WHERE status = %s AND scope = %s AND game = %s AND world = %s "
                    "ORDER BY id DESC LIMIT %s",
                    (STATUS_ACTIVE, sc, gm, wd, lim),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        log_debug(f"{LOG_PREFIX} list_bases failed: {exc}")
        return []
    return [b for b in (_row_to_base(r) for r in rows or []) if b is not None]


async def get_nearest_base(
    position: Any,
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
) -> Dict[str, Any] | None:
    """Return the active base nearest ``position`` for a scope tuple, or None.

    ``position`` is a ``{x, y, z}`` (or ``{x, z}``) mapping. Distance is a pure
    3D (or horizontal, when the base/anchor lacks ``y``) Euclidean metric — no
    text is ever inspected. Bases with no usable anchor point are skipped.
    Returns the nearest base dict with an added ``distance`` key.
    """
    pos = _coerce_point(position) if isinstance(position, dict) else None
    if pos is None and isinstance(position, dict):
        # Allow a horizontal-only position (some callers only have x/z).
        try:
            px = float(position.get("x"))
            pz = float(position.get("z"))
            pos = {"x": px, "y": float(position.get("y") or 0.0), "z": pz}
        except (TypeError, ValueError):
            pos = None
    if pos is None:
        return None
    bases = await list_bases(scope=scope, game=game, world=world)
    best: Dict[str, Any] | None = None
    best_dist = math.inf
    for base in bases:
        anchor = base.get("anchor")
        if not isinstance(anchor, dict):
            continue
        dx = anchor["x"] - pos["x"]
        dy = anchor.get("y", pos["y"]) - pos["y"]
        dz = anchor["z"] - pos["z"]
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist < best_dist:
            best_dist = dist
            best = {**base, "distance": dist}
    return best


async def set_base(
    name: str,
    anchor: Any = None,
    box: Any = None,
    kind: str | None = None,
    note: str | None = None,
    session_id: str | None = None,
    scope: Any = SCOPE_NONE,
    game: Any = SCOPE_NONE,
    world: Any = SCOPE_NONE,
) -> Dict[str, Any]:
    """Register (or re-register) a base Synth chose for itself (fail-safe).

    ``name`` is Synth's own label for the place. ``anchor`` is the base's
    ``{x, y, z}`` point (used by the night-retreat reflex); ``box`` is the
    optional built-structure bounding box ``{x1..z2}``. If a base with the same
    ``name`` already exists for the scope tuple it is **updated in place**
    (coordinates re-recorded); otherwise a new row is inserted. Returns a status
    dict.
    """
    nm = _clip(name, _MAX_NAME_CHARS)
    if not nm:
        return {"status": "error", "message": "empty_name"}
    sc, gm, wd = _coerce_scope(scope), _coerce_scope(game), _coerce_scope(world)
    kd = _clip(kind, _MAX_KIND_CHARS).lower() or "home"
    note_txt = _clip(note, _MAX_NOTE_CHARS) or None
    anchor_pt = _coerce_point(anchor)
    anchor_json = json.dumps(anchor_pt) if anchor_pt else None
    box_val = _coerce_box(box)
    box_json = json.dumps(box_val) if box_val else None
    await init_base_table()
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM vessel_bases "
                    "WHERE status = %s AND scope = %s AND game = %s "
                    "AND world = %s AND name = %s "
                    "ORDER BY id DESC LIMIT 1",
                    (STATUS_ACTIVE, sc, gm, wd, nm),
                )
                row = await cur.fetchone()
                existing_id = None
                if row is not None:
                    existing_id = (
                        row[0] if isinstance(row, (tuple, list)) else row.get("id")
                    )
                if existing_id is not None:
                    await cur.execute(
                        "UPDATE vessel_bases SET kind = %s, anchor = %s, box = %s, "
                        "note = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                        (kd, anchor_json, box_json, note_txt, existing_id),
                    )
                    base_id = existing_id
                else:
                    await cur.execute(
                        "INSERT INTO vessel_bases "
                        "(session_id, scope, game, world, name, kind, anchor, "
                        "box, note, status) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            session_id,
                            sc,
                            gm,
                            wd,
                            nm,
                            kd,
                            anchor_json,
                            box_json,
                            note_txt,
                            STATUS_ACTIVE,
                        ),
                    )
                    base_id = getattr(cur, "lastrowid", None)
                await conn.commit()
        log_info(
            f"{LOG_PREFIX} registered base {nm!r} "
            f"(scope={sc}/{gm}/{wd}, kind={kd}, anchor={anchor_pt})"
        )
        return {
            "status": "ok",
            "id": base_id,
            "name": nm,
            "kind": kd,
            "anchor": anchor_pt,
            "box": box_val,
            "scope": sc,
            "game": gm,
            "world": wd,
        }
    except Exception as exc:
        log_error(f"{LOG_PREFIX} set_base failed: {exc}\n{traceback.format_exc()}")
        return {"status": "error", "message": str(exc)}


async def update_base(
    base_id: int,
    anchor: Any = None,
    box: Any = None,
    kind: str | None = None,
    note: str | None = None,
    status: str | None = None,
) -> Dict[str, Any]:
    """Update an existing base by id (fail-safe).

    Any argument left ``None`` is untouched; ``status`` may be set to
    ``abandoned`` to retire a base without deleting it. Returns a status dict.
    """
    await init_base_table()
    try:
        bid = int(base_id)
    except (TypeError, ValueError):
        return {"status": "error", "message": "invalid_id"}
    sets: List[str] = ["updated_at = CURRENT_TIMESTAMP"]
    params: List[Any] = []
    if anchor is not None:
        anchor_pt = _coerce_point(anchor)
        sets.append("anchor = %s")
        params.append(json.dumps(anchor_pt) if anchor_pt else None)
    if box is not None:
        box_val = _coerce_box(box)
        sets.append("box = %s")
        params.append(json.dumps(box_val) if box_val else None)
    if kind is not None:
        sets.append("kind = %s")
        params.append(_clip(kind, _MAX_KIND_CHARS).lower() or "home")
    if note is not None:
        sets.append("note = %s")
        params.append(_clip(note, _MAX_NOTE_CHARS) or None)
    if status in (STATUS_ACTIVE, STATUS_ABANDONED):
        sets.append("status = %s")
        params.append(status)
    params.append(bid)
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"UPDATE vessel_bases SET {', '.join(sets)} WHERE id = %s",
                    tuple(params),
                )
                updated = getattr(cur, "rowcount", 0) or 0
                await conn.commit()
        return {"status": "ok", "updated_count": updated}
    except Exception as exc:
        log_error(f"{LOG_PREFIX} update_base failed: {exc}")
        return {"status": "error", "message": str(exc)}


async def delete_base(base_id: int) -> Dict[str, Any]:
    """Delete a single base by id (fail-safe)."""
    await init_base_table()
    try:
        bid = int(base_id)
    except (TypeError, ValueError):
        return {"status": "error", "message": "invalid_id"}
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM vessel_bases WHERE id = %s", (bid,))
                deleted = getattr(cur, "rowcount", 0) or 0
                await conn.commit()
        log_info(f"{LOG_PREFIX} deleted base id={bid}")
        return {"status": "ok", "deleted_count": deleted}
    except Exception as exc:
        log_error(f"{LOG_PREFIX} delete_base failed: {exc}")
        return {"status": "error", "message": str(exc)}
