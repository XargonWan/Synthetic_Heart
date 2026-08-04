# plugins/rift_vessel/minecraft/bases.py
"""Minecraft base (home) shim — delegates to the core :mod:`plugins.rift_vessel.vessel_bases` store.

A *base* (home) is a world-agnostic concept — a place Synth claimed to build up,
store resources in, shelter, sleep, or respawn — so its store lives in the Rift
Vessel **core** (``plugins/rift_vessel/vessel_bases.py``), a scope-aware store any
game can use. This module is a thin compatibility shim mirroring
:mod:`plugins.rift_vessel.minecraft.goals`: it keeps a stable
``mc_bases.<fn>(...)`` call surface for the Minecraft connector while forwarding
every call to the core store with the Minecraft **scope tuple** pinned
(``scope="vessel"``, ``game="minecraft"``, ``world=<active server slug>``).

Design notes
------------
* **No local table.** Bases are persisted in the core ``vessel_bases`` table.
* **Lazy import guard.** The core store is imported at module load but guarded so
  removing it can never break the connector.
* **Fail-safe.** If the core store is unavailable, every function degrades to a
  no-op ("no base") and never breaks the session or connector.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.logging_utils import log_debug

try:  # pragma: no cover - import guarded for fail-safe degradation
    from plugins.rift_vessel.vessel_bases import (
        STATUS_ABANDONED,
        STATUS_ACTIVE,
    )
    from plugins.rift_vessel.vessel_bases import delete_base as _b_delete_base
    from plugins.rift_vessel.vessel_bases import get_nearest_base as _b_get_nearest_base
    from plugins.rift_vessel.vessel_bases import init_base_table as _b_init_base_table
    from plugins.rift_vessel.vessel_bases import list_bases as _b_list_bases
    from plugins.rift_vessel.vessel_bases import set_base as _b_set_base
    from plugins.rift_vessel.vessel_bases import update_base as _b_update_base

    _BASES_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - degrade gracefully
    log_debug(f"[minecraft_bases] core base store unavailable: {_exc}")
    STATUS_ACTIVE = "active"
    STATUS_ABANDONED = "abandoned"
    _BASES_AVAILABLE = False

LOG_PREFIX = "[minecraft_bases]"

# The fixed part of the scope tuple that keys every Minecraft base.
_SCOPE = "vessel"
_GAME = "minecraft"
# Sentinel used when no concrete world/server is active (legacy shared scope).
_WORLD_NONE = "none"

# The active concrete world/server identity Minecraft bases are scoped by. Set
# by the connector at connect (from its ``get_world_identity()`` slug) and reset
# on disconnect, so bases persist and resume *per concrete server* — mirroring
# the goals shim. Structural (an opaque identity token), never derived from text.
_active_world: str = _WORLD_NONE


def set_active_world(world: str | None) -> None:
    """Set the concrete world/server that Minecraft bases are scoped by.

    Called by the connector at connect (with its ``get_world_identity()`` slug)
    and at disconnect (with ``None`` to reset). Fully guarded — a bad value
    degrades to the legacy ``"none"`` scope.
    """
    global _active_world
    try:
        token = str(world).strip() if world is not None else ""
        _active_world = token or _WORLD_NONE
    except Exception:  # pragma: no cover - defensive
        _active_world = _WORLD_NONE


def get_active_world() -> str:
    """Return the concrete world/server currently scoping Minecraft bases."""
    return _active_world


__all__ = [
    "STATUS_ACTIVE",
    "STATUS_ABANDONED",
    "init_base_table",
    "list_bases",
    "get_nearest_base",
    "set_base",
    "update_base",
    "delete_base",
    "set_active_world",
    "get_active_world",
]


async def init_base_table() -> None:
    """Ensure the core ``vessel_bases`` table exists (idempotent, fail-safe)."""
    if not _BASES_AVAILABLE:
        return
    try:
        await _b_init_base_table()
    except Exception as exc:  # pragma: no cover - defensive
        log_debug(f"{LOG_PREFIX} init_base_table skipped: {exc}")


async def list_bases(limit: int = 50) -> List[Dict[str, Any]]:
    """Return active Minecraft bases for the active scope, newest first."""
    if not _BASES_AVAILABLE:
        return []
    return await _b_list_bases(
        scope=_SCOPE, game=_GAME, world=_active_world, limit=limit
    )


async def get_nearest_base(position: Any) -> Dict[str, Any] | None:
    """Return the active Minecraft base nearest ``position``, or None."""
    if not _BASES_AVAILABLE:
        return None
    return await _b_get_nearest_base(
        position, scope=_SCOPE, game=_GAME, world=_active_world
    )


async def set_base(
    name: str,
    anchor: Any = None,
    box: Any = None,
    kind: str | None = None,
    note: str | None = None,
    session_id: str | None = None,
) -> Dict[str, Any]:
    """Register (or re-register) a Minecraft base in the core store (fail-safe)."""
    if not _BASES_AVAILABLE:
        return {"status": "error", "message": "bases_unavailable"}
    return await _b_set_base(
        name,
        anchor=anchor,
        box=box,
        kind=kind,
        note=note,
        session_id=session_id,
        scope=_SCOPE,
        game=_GAME,
        world=_active_world,
    )


async def update_base(
    base_id: int,
    anchor: Any = None,
    box: Any = None,
    kind: str | None = None,
    note: str | None = None,
    status: str | None = None,
) -> Dict[str, Any]:
    """Update an existing Minecraft base by id (fail-safe)."""
    if not _BASES_AVAILABLE:
        return {"status": "error", "message": "bases_unavailable"}
    return await _b_update_base(
        base_id,
        anchor=anchor,
        box=box,
        kind=kind,
        note=note,
        status=status,
    )


async def delete_base(base_id: int) -> Dict[str, Any]:
    """Delete a single Minecraft base by id (fail-safe)."""
    if not _BASES_AVAILABLE:
        return {"status": "error", "message": "bases_unavailable"}
    return await _b_delete_base(base_id)
