"""Rift Vessel goal debrief — world-agnostic goal supervision (core).

This module is the world-agnostic *mechanism* behind the Vessel goal debrief: a
lightweight, periodic postflight check that watches the single active vessel
goal and does two structural things (never a keyword/text parse — see
AGENTS.md §5c and the no-keyword rule):

* **(A) Stall detection + cognition feedback.** A goal that stays ``active``
  without its ``current_step`` / ``updated_at`` moving for several debrief ticks
  is *stalled*: the body keeps wandering but nothing closes. When that happens
  the debrief surfaces a structural stall cue for the next will beat (via
  ``WorldState.extra`` — same pattern as ``last_target_result``) telling Synth
  its goal is stuck so it re-evaluates (is it already completable with what I
  hold? should I change approach?). This touches no game logic and is fully
  world-agnostic.

* **(B) Deterministic auto-completion.** The debrief asks the *connector* —
  through the optional, world-owned
  :meth:`VesselConnectorBase.evaluate_goal_completion` hook — whether the active
  goal is already satisfied by the live world state (e.g. the item it set out to
  make is now in the inventory). The *mechanism* (calling the hook and closing
  the goal via the goal store) lives here in core; the *judgement* (what counts
  as "satisfied" — inventory/target matching) is a world-adapter detail. When
  the hook reports satisfied, the debrief marks the goal ``done`` itself, so a
  goal Synth already fulfilled but forgot to close does not linger forever.

Everything here is pure and side-effect free (config reads via an injected
getter, stall bookkeeping via a caller-owned dict) so it can be unit-tested
without a DB, a connector, or an LLM. The actual DB write (marking ``done``) and
the connector hook call are performed by the interface caller, not here.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

# Config defaults (all overridable via the ``vessel`` component config).
_DEFAULT_ENABLED = True
_DEFAULT_USE_HISTORY = True
_DEFAULT_INTERVAL_SEC = 30
_DEFAULT_STALL_TICKS = 4

_INTERVAL_MIN, _INTERVAL_MAX = 5, 3600
_STALL_TICKS_MIN, _STALL_TICKS_MAX = 2, 100

# Structural token surfaced on ``WorldState.extra`` so the will beat can render
# a stall cue. Mirrors the ``last_target_result`` feedback convention.
STALL_FLAG_KEY = "goal_stalled"


def _as_bool(value: Any, default: bool) -> bool:
    """Coerce a config value to bool without keyword parsing of free text."""
    if isinstance(value, bool):
        return value
    if value in (1, "1", "true", "True", "TRUE", "yes", "on"):
        return True
    if value in (0, "0", "false", "False", "FALSE", "no", "off"):
        return False
    return default


def is_debrief_enabled(cfg: Callable[[str, Any], Any]) -> bool:
    """Whether the goal debrief is enabled (default on). Fail-safe."""
    try:
        return _as_bool(
            cfg("VESSEL_GOAL_DEBRIEF_ENABLED", _DEFAULT_ENABLED), _DEFAULT_ENABLED
        )
    except Exception:
        return _DEFAULT_ENABLED


def is_debrief_history_enabled(cfg: Callable[[str, Any], Any]) -> bool:
    """Whether the debrief also consults in-world *history* (default on).

    When enabled, and the fast inventory/world-state check did not already
    satisfy the goal, the debrief asks the connector's optional
    :meth:`VesselConnectorBase.evaluate_goal_completion_from_history` hook
    whether the session's own ``vessel_activity_log`` shows the goal's concrete
    outcome was actually achieved (e.g. a ``place``/``mine``/``kill`` toward the
    goal's structural target). This closes the gap where a goal is fulfilled by
    an *action that leaves no lasting inventory trace* (placing blocks, killing a
    mob, exploring). Purely structural (id-based matching on logged targets),
    never a text parse. Fail-safe.
    """
    try:
        return _as_bool(
            cfg("VESSEL_GOAL_DEBRIEF_USE_HISTORY", _DEFAULT_USE_HISTORY),
            _DEFAULT_USE_HISTORY,
        )
    except Exception:
        return _DEFAULT_USE_HISTORY


def resolve_debrief_interval(cfg: Callable[[str, Any], Any]) -> int:
    """Seconds between debrief checks, clamped. Fail-safe."""
    try:
        raw = int(cfg("VESSEL_GOAL_DEBRIEF_INTERVAL_SEC", _DEFAULT_INTERVAL_SEC))
    except (TypeError, ValueError, Exception):
        return _DEFAULT_INTERVAL_SEC
    return max(_INTERVAL_MIN, min(raw, _INTERVAL_MAX))


def resolve_stall_ticks(cfg: Callable[[str, Any], Any]) -> int:
    """Consecutive unchanged debrief ticks before a goal is deemed stalled."""
    try:
        raw = int(cfg("VESSEL_GOAL_DEBRIEF_STALL_TICKS", _DEFAULT_STALL_TICKS))
    except (TypeError, ValueError, Exception):
        return _DEFAULT_STALL_TICKS
    return max(_STALL_TICKS_MIN, min(raw, _STALL_TICKS_MAX))


def goal_signature(goal: Dict[str, Any] | None) -> str | None:
    """Return a structural fingerprint of a goal's *progress state*.

    Combines the goal id, ``current_step`` and ``updated_at`` so that any real
    movement (advancing a step, re-aiming, a note write — all bump
    ``updated_at``) changes the signature. Purely structural; never inspects the
    free-text ``description``. Returns ``None`` when there is no goal.
    """
    if not isinstance(goal, dict):
        return None
    gid = goal.get("id")
    if gid is None:
        return None
    return f"{gid}:{goal.get('current_step')}:{goal.get('updated_at')}"


def update_stall_state(
    state: Dict[str, Any],
    goal: Dict[str, Any] | None,
    stall_ticks: int,
) -> bool:
    """Advance the caller-owned stall bookkeeping and report if goal is stalled.

    ``state`` is a small mutable dict the caller keeps across debrief ticks
    (``{"sig": <last signature>, "count": <unchanged tick count>}``). Each call
    compares the current goal's :func:`goal_signature` to the stored one:

    * a new goal / real progress resets the counter to 0;
    * an unchanged signature increments it.

    Returns ``True`` when the counter reaches ``stall_ticks`` (the goal has sat
    unchanged for that many consecutive debrief ticks) — i.e. the goal is
    stalled and the caller should surface the stall cue. Returns ``False`` for
    no goal. Purely structural, no side effects beyond mutating ``state``.
    """
    sig = goal_signature(goal)
    if sig is None:
        state["sig"] = None
        state["count"] = 0
        return False
    if state.get("sig") == sig:
        state["count"] = int(state.get("count") or 0) + 1
    else:
        state["sig"] = sig
        state["count"] = 0
    return int(state.get("count") or 0) >= max(1, int(stall_ticks))
