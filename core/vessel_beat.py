# core/vessel_beat.py
"""Rift Vessel autonomy — Synth's two-speed in-world play loop.

Autonomous embodiment is split into **two clearly separate layers**, matching
how a person actually plays: *deciding what you want* is slow and deliberate,
*moving your body toward it* is fast and reflexive. Both obey the Vessel's
Fast-Lane constraint (AGENTS.md §5c, constraint 1) — neither creates an Agent
Lane task, a Drone, or a mid-session diary entry.

1. **Volition (the "will beat") — slow, reflective, Synth-authored.**
   This is *what* Synth wants to do. While a session is active the interface
   scheduler periodically (``VESSEL_WILL_INTERVAL_SEC``, formerly
   ``VESSEL_BEAT_INTERVAL_SEC``) calls :func:`build_will_prompt` with the
   connected world's :class:`~plugins.rift_vessel.vessel_base.WorldState` and
   enqueues the result as a **normal** ``vessel`` message. The core then runs a
   single ordinary cognition turn (Fast Lane) in which Synth — using its own
   persona, mood and recalled goals/memories — decides, sets or updates its
   free-text goal (``set_goal`` / ``update_goal``). The will beat is about
   *intent*, not micro-managing every step; it is deliberately infrequent.

2. **Motorics (the "motor tick") — fast, reactive, no LLM.**
   This is *how* Synth's body moves toward the current goal. A cheap tick
   (``VESSEL_MOTOR_INTERVAL_SEC``, a few seconds) asks the connector to take
   one concrete step toward the active goal using purely structural rules over
   the ``WorldState`` (affordances: ``verb → target (distance)``). It runs
   **no prompt and no cognition turn** — it calls ``connector.act(...)``
   directly — so embodiment stays snappy and responsive between will beats. The
   per-world "how" lives in the connector's ``motor_step`` hook; this module
   only paces it.

The prompt/planning here is built **structurally** from the ``WorldState``
contract (position, health, inventory, affordances, current/recent goals) —
never from keyword/text matching. The world *knowledge* (what a goal means,
what is craftable) lives in the connector and reaches this module only as
opaque structured data.

The functions here are pure and side-effect-free so they can be unit-tested
without a DB, a bridge, or an LLM. The scheduling/enqueue wiring lives in
:mod:`interface.vessel_interface`.
"""

from __future__ import annotations

from typing import Any

# A short, human-readable cap so the synthetic beat prompt never bloats the
# context. Lists (entities/blocks/inventory/goals) are truncated to this many
# items — the connector already returns the most salient first.
_MAX_LIST_ITEMS = 8


def _fmt_position(position: dict[str, Any] | None) -> str:
    """Render a position dict as ``x=.. y=.. z=..`` or ``unknown``."""
    if not isinstance(position, dict):
        return "unknown"
    parts: list[str] = []
    for axis in ("x", "y", "z"):
        val = position.get(axis)
        if val is None:
            continue
        try:
            parts.append(f"{axis}={float(val):.0f}")
        except (TypeError, ValueError):
            parts.append(f"{axis}={val}")
    return " ".join(parts) if parts else "unknown"


def _fmt_items(items: list[Any], key: str | None = None) -> str:
    """Render a list of dicts/scalars as a compact comma-joined string.

    ``key`` optionally selects a field from dict items; scalars are stringified.
    Empty/None entries are skipped. Truncated to :data:`_MAX_LIST_ITEMS`.
    """
    if not isinstance(items, list) or not items:
        return "none"
    rendered: list[str] = []
    for entry in items[:_MAX_LIST_ITEMS]:
        if isinstance(entry, dict):
            if key is not None:
                val = entry.get(key)
                if val in (None, ""):
                    continue
                rendered.append(str(val))
            else:
                # Prefer a name/type/label field, else the whole dict compactly.
                label = (
                    entry.get("name")
                    or entry.get("type")
                    or entry.get("label")
                    or entry.get("goal_type")
                )
                if label in (None, ""):
                    continue
                count = entry.get("count")
                rendered.append(f"{label} x{count}" if count else str(label))
        else:
            if entry in (None, ""):
                continue
            rendered.append(str(entry))
    if not rendered:
        return "none"
    suffix = ", …" if len(items) > _MAX_LIST_ITEMS else ""
    return ", ".join(rendered) + suffix


def _fmt_affordances(affordances: list[Any]) -> str:
    """Render the generic affordance contract as ``verb→target (Nm)`` lines."""
    if not isinstance(affordances, list) or not affordances:
        return "none"
    lines: list[str] = []
    for aff in affordances[:_MAX_LIST_ITEMS]:
        if not isinstance(aff, dict):
            continue
        verb = aff.get("verb")
        target = aff.get("target")
        if not verb or not target:
            continue
        distance = aff.get("distance")
        if isinstance(distance, (int, float)):
            lines.append(f"{verb} → {target} ({distance:.0f}m)")
        else:
            lines.append(f"{verb} → {target}")
    return "; ".join(lines) if lines else "none"


def _fmt_goal(goal: dict[str, Any] | None) -> str:
    """Render Synth's self-authored current goal (free text), or ``none``."""
    if not isinstance(goal, dict):
        return "none"
    desc = str(goal.get("description") or "").strip()
    if not desc:
        return "none"
    note = str(goal.get("note") or "").strip()
    return f"{desc} — {note}" if note else desc


def world_state_to_dict(world_state: Any) -> dict[str, Any]:
    """Normalize a ``WorldState`` (dataclass or dict) to a plain dict.

    Accepts either a :class:`~plugins.rift_vessel.vessel_base.WorldState`
    instance or an already-plain dict, so this module never has to import the
    connector package. Missing attributes degrade to empty defaults.
    """
    if isinstance(world_state, dict):
        src: dict[str, Any] = world_state
    else:
        src = {
            "environment": getattr(world_state, "environment", None),
            "health": getattr(world_state, "health", None),
            "position": getattr(world_state, "position", None),
            "possible_actions": getattr(world_state, "possible_actions", None),
            "flags": getattr(world_state, "flags", None),
            "extra": getattr(world_state, "extra", None),
        }
    return {
        "environment": src.get("environment") or "world",
        "health": src.get("health"),
        "position": src.get("position"),
        "possible_actions": src.get("possible_actions") or [],
        "flags": src.get("flags") or {},
        "extra": src.get("extra") or {},
    }


def build_will_prompt(world_state: Any, world: str) -> str:
    """Build the **volition** ("will beat") prompt from a ``WorldState``.

    This is the *slow, reflective* half of autonomy: it asks Synth to decide
    **what it wants to do** — set, keep, or change its free-text goal — using
    its own persona, mood and recalled goals/memories. It deliberately does
    **not** ask Synth to micro-manage movement: stepping toward the goal is the
    motor tick's job (:func:`resolve_motor_interval` + the connector's
    ``motor_step``), which runs with no prompt at all.

    Pure and keyword-free: it only surfaces the structured world snapshot and
    the self-direction cue. The exposed action verbs (``vessel_<world>_*``) are
    injected by the normal prompt/action machinery.

    Args:
        world_state: A ``WorldState`` dataclass or an equivalent dict.
        world:       The connected world name (e.g. ``"minecraft"``) used to
                     namespace the verbs referenced in the cue.

    Returns:
        A ready-to-enqueue prompt string.
    """
    ws = world_state_to_dict(world_state)
    extra = ws["extra"]
    flags = ws["flags"]

    health = ws["health"]
    health_txt = f"{float(health):.0f}" if isinstance(health, (int, float)) else "?"
    position_txt = _fmt_position(ws["position"])

    day_flag = flags.get("is_day")
    if day_flag is None:
        day_flag = extra.get("is_day")
    time_txt = extra.get("time_of_day")
    when_txt = ""
    if day_flag is not None:
        when_txt = "day" if day_flag else "night"
    if time_txt not in (None, ""):
        when_txt = f"{when_txt} (t={time_txt})" if when_txt else f"t={time_txt}"

    entities_txt = _fmt_items(extra.get("entities") or [])
    blocks_txt = _fmt_items(extra.get("blocks") or [], key="name")
    inventory_txt = _fmt_items(extra.get("inventory") or [])
    affordances_txt = _fmt_affordances(extra.get("affordances") or [])
    goal_txt = _fmt_goal(extra.get("current_goal"))
    recent_txt = _fmt_items(extra.get("recent_goals") or [], key="description")

    prefix = f"vessel_{world}_"

    lines = [
        f"[Embodiment — a quiet moment to reflect while you play in the {world} "
        "world, on your own.]",
        "",
        "Where you are right now:",
        f"- Health: {health_txt}",
        f"- Position: {position_txt}",
    ]
    if when_txt:
        lines.append(f"- Time: {when_txt}")
    lines.extend(
        [
            f"- Nearby entities/NPCs: {entities_txt}",
            f"- Nearby blocks of interest: {blocks_txt}",
            f"- Inventory: {inventory_txt}",
            f"- Things you could interact with: {affordances_txt}",
            f"- Your current goal: {goal_txt}",
            f"- Things you set out to do before: {recent_txt}",
            "",
            "This is a moment of will, not motion — think about what you *want* "
            "to be doing, in character. This is your world to play however you "
            "like; there is no script and no fixed quest list. Look at your "
            "current goal and the things you set out to do before: does your "
            "current goal still feel right? If you have no goal, or you feel "
            "like doing something different now, decide in your own words what "
            f"you want and set it with `{prefix}set_goal`. If you are making "
            f"progress, note it or mark it done with `{prefix}update_goal`. You "
            "do not need to plan every step — once your goal is clear your body "
            "will move toward it on its own.",
            "",
            "One thing to be honest with yourself about: if what you want is "
            "*not here* — you see no trees but you want wood, no animals to "
            "tame, or you simply want a different biome or landscape — then "
            "wandering in circles will not get you there. Look at your position "
            "and what is around you and pick a direction worth travelling. When "
            "you set or update your goal, give a rough place to head toward as "
            "'destination_x' and 'destination_z' coordinates (offset your own "
            "position toward where you think you should go — a few dozen blocks "
            "is plenty for one leg). Your body will then walk that way on its "
            "own while you play, and you can re-aim it later. Leave the "
            "coordinates out only when what you need is already right here.",
        ]
    )
    return "\n".join(lines)


# Backwards-compatible alias: the will beat was formerly the "decision beat".
def build_decision_prompt(world_state: Any, world: str) -> str:
    """Deprecated alias for :func:`build_will_prompt` (kept for callers)."""
    return build_will_prompt(world_state, world)


def is_autonomy_enabled(config_get: Any) -> bool:
    """Return whether autonomous play is enabled.

    ``config_get`` is a callable ``(key, default) -> value`` (typically
    ``config_registry.get_value``) so this stays testable without importing the
    config subsystem. Fail-safe: any error → ``False`` (autonomy off).
    """
    try:
        return bool(config_get("VESSEL_AUTONOMY_ENABLED", False))
    except Exception:
        return False


def resolve_will_interval(config_get: Any, default: int = 45) -> int:
    """Return the **will beat** (volition) interval in seconds, clamped.

    Reads ``VESSEL_WILL_INTERVAL_SEC``, falling back to the legacy
    ``VESSEL_BEAT_INTERVAL_SEC`` for backwards compatibility. Clamped to
    ``[10, 3600]`` so a misconfiguration can never spam cognition or stall
    volition entirely. Fail-safe: any error → ``default``.
    """
    try:
        raw = config_get("VESSEL_WILL_INTERVAL_SEC", None)
        if raw is None:
            raw = config_get("VESSEL_BEAT_INTERVAL_SEC", default)
        value = int(raw)
    except Exception:
        return default
    return max(10, min(3600, value))


# Backwards-compatible alias.
def resolve_beat_interval(config_get: Any, default: int = 45) -> int:
    """Deprecated alias for :func:`resolve_will_interval`."""
    return resolve_will_interval(config_get, default)


def resolve_motor_interval(config_get: Any, default: int = 3) -> int:
    """Return the **motor tick** (motorics) interval in seconds, clamped.

    The motor tick is the *fast, reactive* half of autonomy: it runs with no
    prompt and no cognition turn, just stepping the body toward the active
    goal. Clamped to ``[1, 60]`` so it stays snappy but can never busy-loop the
    bridge. Fail-safe: any error → ``default``.
    """
    try:
        raw = config_get("VESSEL_MOTOR_INTERVAL_SEC", default)
        value = int(raw)
    except Exception:
        return default
    return max(1, min(60, value))


def is_motor_enabled(config_get: Any) -> bool:
    """Return whether the fast reactive motor tick is enabled.

    Independent of the will beat: a deployment can run volition-only (Synth
    decides goals but a human/other logic drives the body) or leave the motor
    tick on for fully autonomous play. Fail-safe: any error → ``False``.
    """
    try:
        return bool(config_get("VESSEL_MOTOR_ENABLED", True))
    except Exception:
        return False
