# plugins/rift_vessel/minecraft/goals.py
"""Minecraft goal shim — delegates to the generic :mod:`plugins.goals` plugin.

Goals used to live in a Minecraft-specific ``minecraft_goals`` table owned by
this module. They are now persisted by the **generic Goals plugin**
(``plugins/goals/goals.py``), a standalone, scope-aware store that any game,
planner, or the Synth itself can use for personal life goals. This module is a
thin compatibility shim: it keeps the historical ``mc_goals.<fn>(...)`` call
surface used across the Minecraft connector, but every call is forwarded to the
generic store with the Minecraft **scope tuple** pinned
(``scope="vessel"``, ``game="minecraft"``, ``world="none"``).

Design notes
------------
* **No local table.** The ``minecraft_goals`` table is retired; a startup
  migration (``core/migrations.py::_migrate_goals_table``) renames it to
  ``goals`` and backfills the Minecraft scope on legacy rows.
* **Lazy import guard.** The generic store functions are imported at module
  load but guarded so removing the Goals plugin can never break the connector.
* **Fail-safe.** If the Goals plugin/module is unavailable, every function
  degrades to a no-op ("no goal") and never breaks the session or connector.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.logging_utils import log_debug

# Re-export the goal lifecycle + target enums from the generic store so any
# existing ``mc_goals.STATUS_ACTIVE`` / ``mc_goals.TARGET_KIND_BLOCK`` reference
# keeps working unchanged.
try:  # pragma: no cover - import guarded for fail-safe degradation
    from plugins.goals.goals import (
        STATUS_ABANDONED,
        STATUS_ACTIVE,
        STATUS_DONE,
        TARGET_KIND_BLOCK,
        TARGET_KIND_COORDINATE,
        TARGET_KIND_ENTITY,
    )
    from plugins.goals.goals import clear_abandoned_goals as _g_clear_abandoned_goals
    from plugins.goals.goals import clear_all_goals as _g_clear_all_goals
    from plugins.goals.goals import delete_goal as _g_delete_goal
    from plugins.goals.goals import get_active_goal as _g_get_active_goal
    from plugins.goals.goals import init_goal_table as _g_init_goal_table
    from plugins.goals.goals import list_all_goals as _g_list_all_goals
    from plugins.goals.goals import list_recent_goals as _g_list_recent_goals
    from plugins.goals.goals import set_goal as _g_set_goal
    from plugins.goals.goals import update_active_goal as _g_update_active_goal

    _GOALS_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - degrade gracefully
    log_debug(f"[minecraft_goals] generic Goals plugin unavailable: {_exc}")
    STATUS_ACTIVE = "active"
    STATUS_DONE = "done"
    STATUS_ABANDONED = "abandoned"
    TARGET_KIND_BLOCK = "block"
    TARGET_KIND_ENTITY = "entity"
    TARGET_KIND_COORDINATE = "coordinate"
    _GOALS_AVAILABLE = False

LOG_PREFIX = "[minecraft_goals]"

# The fixed part of the scope tuple that keys every Minecraft goal.
_SCOPE = "vessel"
_GAME = "minecraft"
# Sentinel used when no concrete world/server is active (legacy shared scope).
_WORLD_NONE = "none"

# The **active concrete world/server** identity Minecraft goals are scoped by.
# It is set by the Minecraft connector at connect time (from
# ``MinecraftConnector.get_world_identity()`` → a ``host:port`` slug) and reset
# on disconnect, so that goals persist and resume *per concrete server*: when
# Synth logs back into the same server it recalls exactly where it was, while a
# different server keeps its own independent progression. Structural (an opaque
# identity token), never derived from message text. When unset it falls back to
# the legacy shared ``"none"`` scope so nothing breaks before/without a session.
_active_world: str = _WORLD_NONE


def set_active_world(world: str | None) -> None:
    """Set the concrete world/server that Minecraft goals are scoped by.

    Called by the connector at connect (with its ``get_world_identity()`` slug)
    and at disconnect (with ``None`` to reset to the shared scope). Fully
    guarded — a bad value degrades to the legacy ``"none"`` scope.
    """
    global _active_world
    try:
        token = str(world).strip() if world is not None else ""
        _active_world = token or _WORLD_NONE
    except Exception:  # pragma: no cover - defensive
        _active_world = _WORLD_NONE


def get_active_world() -> str:
    """Return the concrete world/server currently scoping Minecraft goals."""
    return _active_world


__all__ = [
    "STATUS_ACTIVE",
    "STATUS_DONE",
    "STATUS_ABANDONED",
    "TARGET_KIND_BLOCK",
    "TARGET_KIND_ENTITY",
    "TARGET_KIND_COORDINATE",
    "init_goal_table",
    "get_active_goal",
    "list_recent_goals",
    "list_all_goals",
    "delete_goal",
    "clear_abandoned_goals",
    "clear_all_goals",
    "set_goal",
    "update_active_goal",
    "set_active_world",
    "get_active_world",
]


async def init_goal_table() -> None:
    """Ensure the generic ``goals`` table exists (idempotent, fail-safe)."""
    if not _GOALS_AVAILABLE:
        return
    try:
        await _g_init_goal_table()
    except Exception as exc:  # pragma: no cover - defensive
        log_debug(f"{LOG_PREFIX} init_goal_table skipped: {exc}")


async def get_active_goal() -> Dict[str, Any] | None:
    """Return the active Minecraft goal from the generic store, or None."""
    if not _GOALS_AVAILABLE:
        return None
    return await _g_get_active_goal(scope=_SCOPE, game=_GAME, world=_active_world)


async def list_recent_goals(limit: int = 10) -> List[Dict[str, Any]]:
    """Return recent Minecraft goals (any status), newest first."""
    if not _GOALS_AVAILABLE:
        return []
    return await _g_list_recent_goals(
        limit=limit, scope=_SCOPE, game=_GAME, world=_active_world
    )


async def list_all_goals(limit: int = 50) -> List[Dict[str, Any]]:
    """Return every Minecraft goal with timestamps for the WebUI Goals view.

    The generic store is scope-agnostic, so this filters to the Minecraft scope
    tuple for the legacy per-world Goals sub-tab. Read-only; fail-safe.
    """
    if not _GOALS_AVAILABLE:
        return []
    all_goals = await _g_list_all_goals(limit=limit)
    # Return every Minecraft goal across *all* concrete servers (scope+game),
    # not just the currently-connected one, so the WebUI Goals sub-tab shows the
    # full per-server progression history even when no session is open.
    return [g for g in all_goals if g.get("scope") == _SCOPE and g.get("game") == _GAME]


async def delete_goal(goal_id: int) -> Dict[str, Any]:
    """Delete a single non-active Minecraft goal by id (fail-safe)."""
    if not _GOALS_AVAILABLE:
        return {"status": "error", "message": "goals_unavailable"}
    return await _g_delete_goal(goal_id)


async def clear_abandoned_goals() -> Dict[str, Any]:
    """Delete abandoned goals (scope-agnostic in the generic store)."""
    if not _GOALS_AVAILABLE:
        return {"status": "error", "message": "goals_unavailable"}
    return await _g_clear_abandoned_goals()


async def clear_all_goals() -> Dict[str, Any]:
    """Delete EVERY Minecraft goal — all statuses and all concrete servers.

    The WebUI Goals "clear all" (clean-attempt reset). Matches what
    :func:`list_all_goals` displays: every row with ``scope='vessel'`` and
    ``game='minecraft'``, regardless of the per-server ``world`` identity.
    Fail-safe.
    """
    if not _GOALS_AVAILABLE:
        return {"status": "error", "message": "goals_unavailable"}
    return await _g_clear_all_goals(scope=_SCOPE, game=_GAME)


async def set_goal(
    description: str,
    session_id: str | None = None,
    note: str | None = None,
    destination: Any = None,
    steps: Any = None,
    target_kind: Any = None,
    target_name: Any = None,
) -> Dict[str, Any]:
    """Adopt a self-authored Minecraft goal in the generic store (fail-safe)."""
    if not _GOALS_AVAILABLE:
        return {"status": "error", "message": "goals_unavailable"}
    return await _g_set_goal(
        description,
        session_id=session_id,
        note=note,
        destination=destination,
        steps=steps,
        target_kind=target_kind,
        target_name=target_name,
        scope=_SCOPE,
        game=_GAME,
        world=_active_world,
    )


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
    """Update the active Minecraft goal in the generic store (fail-safe)."""
    if not _GOALS_AVAILABLE:
        return {"status": "error", "message": "goals_unavailable"}
    return await _g_update_active_goal(
        note=note,
        status=status,
        destination=destination,
        steps=steps,
        current_step=current_step,
        advance=advance,
        target_kind=target_kind,
        target_name=target_name,
        scope=_SCOPE,
        game=_GAME,
        world=_active_world,
    )
