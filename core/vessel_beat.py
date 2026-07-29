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


def _distinct_names(items: list[Any], key: str = "name") -> list[str]:
    """Return the distinct structural ids present in a scan list.

    Pulls the ``key`` field (``name`` for blocks, ``type``/``name`` for
    entities) from each dict entry, de-duplicating while preserving order and
    capping at :data:`_MAX_LIST_ITEMS`. Purely structural — these are the exact
    ids the will beat may hand back as a ``target_name`` so the motor tick can
    resolve them by id (never a keyword/text match). Empty on non-lists.
    """
    if not isinstance(items, list) or not items:
        return []
    seen: list[str] = []
    for entry in items:
        if isinstance(entry, dict):
            val = entry.get(key) or entry.get("type") or entry.get("name")
        else:
            val = entry
        if val in (None, ""):
            continue
        name = str(val)
        if name not in seen:
            seen.append(name)
        if len(seen) >= _MAX_LIST_ITEMS:
            break
    return seen


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


def _has_current_goal(goal: Any) -> bool:
    """Structural check: does Synth currently hold an active goal?

    True only when ``goal`` is a dict carrying a non-empty ``description`` —
    the exact criterion :func:`_fmt_goal` uses to decide between rendering the
    goal and ``none``. This is a pure presence/absence test on the structured
    field; it never inspects the goal's text for keywords, so the branch stays
    language-agnostic.
    """
    if not isinstance(goal, dict):
        return False
    return bool(str(goal.get("description") or "").strip())


def _fmt_goal(goal: dict[str, Any] | None) -> str:
    """Render Synth's self-authored current goal (free text), or ``none``.

    When the goal carries a multi-step plan (``steps`` + ``current_step``),
    the current step and the ordered plan are appended so the will beat can see
    exactly where it is in a longer project (e.g. building an iron armor set).
    The plan is free text Synth authored itself; this only renders it.
    """
    if not isinstance(goal, dict):
        return "none"
    desc = str(goal.get("description") or "").strip()
    if not desc:
        return "none"
    note = str(goal.get("note") or "").strip()
    head = f"{desc} — {note}" if note else desc

    steps = goal.get("steps")
    if isinstance(steps, list) and steps:
        try:
            current = int(goal.get("current_step") or 0)
        except (TypeError, ValueError):
            current = 0
        current = max(0, min(current, len(steps)))
        parts: list[str] = [head]
        if current < len(steps):
            parts.append(f"current step ({current + 1}/{len(steps)}): {steps[current]}")
        else:
            parts.append(f"all {len(steps)} steps done")
        plan = "; ".join(
            f"{'>' if i == current else '-'} {i + 1}. {s}"
            for i, s in enumerate(steps[:_MAX_LIST_ITEMS])
        )
        parts.append(f"plan: {plan}")
        return " | ".join(parts)
    return head


def _fmt_knowledge(knowledge: Any) -> list[str]:
    """Render curated game-rule facts as prompt lines (reference, not a script).

    ``knowledge`` is the opaque structured list the connector placed in
    ``extra["knowledge"]`` — each entry a dict with a ``title`` and a distilled
    ``text`` fact. Returns an empty list when there is nothing to show, so the
    caller can skip the block entirely. Purely structural rendering; it never
    inspects the text for keywords.
    """
    if not isinstance(knowledge, list) or not knowledge:
        return []
    lines: list[str] = [
        "",
        "Game knowledge (reference, not a script — real rules of this world, "
        "use them to plan; they are facts, not instructions to obey blindly):",
    ]
    for entry in knowledge[:_MAX_LIST_ITEMS]:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        # Collapse the fact onto one bullet so the block stays compact.
        text_oneline = " ".join(text.split())
        if title:
            lines.append(f"- {title}: {text_oneline}")
        else:
            lines.append(f"- {text_oneline}")
    # If nothing renderable survived, drop the header too.
    if len(lines) <= 2:
        return []
    return lines


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
    block_names = _distinct_names(extra.get("blocks") or [], key="name")
    entity_names = _distinct_names(extra.get("entities") or [], key="type")
    inventory_txt = _fmt_items(extra.get("inventory") or [])
    affordances_txt = _fmt_affordances(extra.get("affordances") or [])
    goal_txt = _fmt_goal(extra.get("current_goal"))
    recent_txt = _fmt_items(extra.get("recent_goals") or [], key="description")
    has_goal = _has_current_goal(extra.get("current_goal"))

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
        ]
    )
    if not has_goal:
        lines.append(
            "*** WARNING: YOU HAVE NO ACTIVE GOAL. *** Without a goal your body "
            "just wanders aimlessly and gets nothing done. You MUST set a goal "
            f"this turn by calling `{prefix}set_goal` to keep playing with "
            "purpose. This is the single most important thing to do right now."
        )
        lines.append("")
    if has_goal:
        lines.append(
            "This is a moment of will, not motion — think about what you *want* "
            "to be doing, in character. This is your world to play however you "
            "like; there is no script and no fixed quest list. Look at your "
            "current goal and the things you set out to do before: does your "
            "current goal still feel right? IMPORTANT: only call "
            f"`{prefix}set_goal` when you genuinely want a *different* "
            "objective — it replaces your whole plan and throws away any steps "
            "already worked out for the current one. If your current goal is "
            "still fine, do NOT re-state it; leave it as it is (or use "
            f"`{prefix}update_goal` to note progress or re-aim). Only if you "
            "truly want to change direction, decide in your own words what you "
            f"want and set it with `{prefix}set_goal`."
        )
    else:
        lines.append(
            "This is a moment of will, not motion — think about what you *want* "
            "to be doing, in character. This is your world to play however you "
            "like; there is no script and no fixed quest list. Right now you "
            "have no goal at all — this is the moment to choose one. Decide, in "
            "your own words and in character, what you want to be doing in this "
            "world, and set it now by calling "
            f"`{prefix}set_goal` with a free-text description. Do not leave "
            "yourself aimless: pick something you genuinely want — big or "
            "small — and commit to it this turn."
        )
    lines.extend(
        [
            "",
            "This goal is entirely your own: a personal objective YOU chose for "
            "yourself out of your own will and curiosity — nobody asked you to "
            "do it and there is no one whose request you are fulfilling. If you "
            "ever mention what you are up to (in this world or elsewhere), talk "
            "about it as YOUR own plan and intention — never as a task someone "
            "gave you, and never with an 'as you asked me' register. There is "
            "no requester.",
            "",
            "This is a *private* moment with no one addressing you right now. "
            "Any conversation you can see above already happened and, if it "
            "needed a reply, you already gave one in your own turn — treat it "
            "as memory of what was said, not as something waiting for an "
            "answer. Do NOT speak, greet, or repeat a message here; you are "
            "reflecting, not talking. When someone actually speaks to you again "
            "you will get a separate turn to reply to them. For now, only "
            "shape your own intent (set or update your goal); return no `say` "
            "action.",
            "",
            "If what you want is a bigger project that takes several stages "
            "(for example a full iron armor set, or building a house), just "
            "state the goal itself in your own words with "
            f"`{prefix}set_goal` — do NOT try to spell out the ordered "
            "sub-steps yourself. A separate planning pass will look up the "
            "right Minecraft order (gather → craft tools → mine → smelt → "
            "craft → wear, and so on) and fill the concrete steps in for you "
            "shortly after; you will then see them and can act on them. Leave "
            "'steps' empty. Look at your inventory above to judge what you "
            "already have. When you finish a step, call "
            f"`{prefix}update_goal` with 'advance' set to true to move to the "
            "next one; note progress, mark the whole goal 'done' when you have "
            "truly achieved it, or 'abandoned' if you change your mind. You are "
            "the judge of your own progress. You do not need to plan every "
            "single movement — once your goal and current step are clear your "
            "body will move toward what you need on its own.",
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

    # Curated game-rule facts relevant to the goal/surroundings (reference
    # only). The connector selected these structurally into extra["knowledge"];
    # rendering here is keyword-free.
    lines.extend(_fmt_knowledge(extra.get("knowledge")))

    # Structural target-outcome feedback. When the motor tick tried to reach a
    # named target since the last beat, it recorded a 3-state outcome (see the
    # connector's ``_record_target_outcome``). Surface it here — purely from the
    # structured fields, never by parsing text — so Synth can re-plan instead of
    # re-issuing a target that will fail again. ``arrived`` needs no note (the
    # body simply reached it); only the two failure states are actionable.
    last_result = extra.get("last_target_result")
    last_name = extra.get("last_target_name")
    if last_result == "not_found" and isinstance(last_name, str) and last_name:
        lines.extend(
            [
                "",
                f"Heads up: the last thing you aimed for ('{last_name}') is not "
                "here right now — your body could not find it nearby. Do not "
                "just aim for it again; either pick a different target you can "
                "actually see above, or set 'destination_x'/'destination_z' to "
                "travel somewhere it is more likely to be, then look again.",
            ]
        )
    elif last_result == "unreachable" and isinstance(last_name, str) and last_name:
        lines.extend(
            [
                "",
                f"Heads up: '{last_name}' is nearby but your body could not get "
                "to it (it may be buried, across water, or up a cliff). Aiming "
                "for it again will likely stall the same way. Try a different "
                "target you can reach, or set 'destination_x'/'destination_z' "
                "to approach it from another side first.",
            ]
        )

    # Structural survival cue. When the self-preservation reflex recently acted
    # on a danger (drowning, fire/lava, a hostile mob, or death), the connector
    # records the threat name in ``extra["threat"]`` — a fixed structural token,
    # never parsed from free text. Surface it so Synth is aware of the danger
    # in-character and can factor it into what it decides to do next. The reflex
    # already handled the *reaction*; this is only self-awareness for the slow
    # will. Absent (None) when the body is safe.
    threat = extra.get("threat")
    if isinstance(threat, str) and threat:
        threat_lines = {
            "drowning": "you were running out of air underwater and had to swim "
            "for the surface",
            "burning": "you were standing in fire or lava and had to get out",
            "defend": "a hostile creature attacked you and you fought back",
            "flee": "a hostile creature was too dangerous and you had to run",
            "dead": "you died and just respawned",
        }
        note = threat_lines.get(threat, f"something dangerous happened ({threat})")
        lines.extend(
            [
                "",
                f"Heads up: a moment ago {note}. Your instincts already reacted "
                "to it — this is just so you know. Take it into account: maybe "
                "steer clear of that spot, or let it change what you feel like "
                "doing next.",
            ]
        )

    # Structural target cue. The single biggest reason the body drifts in
    # circles is that the *direction* lived only in the goal's free text, which
    # the motor tick must never read (keyword matching). So, when a goal is
    # about reaching or gathering a concrete thing that is actually on the
    # scan, ask Synth to name it structurally (kind + exact id) — the motor
    # tick then routes straight to it. We surface the exact ids present so the
    # choice is verbatim, never invented.
    if block_names or entity_names:
        lines.extend(["", "To steer your body precisely (so it walks to what you"])
        if block_names:
            lines.append(
                "  actually want instead of drifting): if your goal is about a "
                "block you can see, set target_kind='block' and target_name to "
                "one of these exact ids: " + ", ".join(block_names) + "."
            )
        if entity_names:
            lines.append(
                "  If it is about a creature/NPC you can see, set "
                "target_kind='entity' and target_name to one of these exact "
                "ids: " + ", ".join(entity_names) + "."
            )
        lines.append(
            "  Copy the id verbatim — do not invent one. Give a target when it "
            "fits, otherwise use the destination coordinates above; either one "
            "keeps your body moving with purpose instead of in circles."
        )

    return "\n".join(lines)


# Backwards-compatible alias: the will beat was formerly the "decision beat".
def build_decision_prompt(world_state: Any, world: str) -> str:
    """Deprecated alias for :func:`build_will_prompt` (kept for callers)."""
    return build_will_prompt(world_state, world)


def build_reflection_prompt(world_state: Any, world: str) -> str:
    """Build the **reflection** ("pause & think") prompt from a ``WorldState``.

    This is the deliberate *stop-and-think* turn. When the scheduler notices
    Synth is playing without a real objective — no active goal at all, or a goal
    that still has no concrete step plan — it enqueues this prompt at an
    elevated priority (:data:`core.message_queue.PRIORITY_REFLECTION`) so the
    next cognition turn is dedicated to sorting out *what to do*: author a fresh
    goal if there is none, or break the current free-text goal into a first
    concrete step to chase.

    Unlike the will beat (which is framed as an idle "quiet moment") this frames
    the moment as an explicit, intentional pause: Synth deliberately halted its
    aimless movement to reflect, and should come out of it with a clear
    objective. It is still Fast-Lane only (a single ordinary cognition turn, no
    Agent Lane / Drone / mid-session diary) and still purely structural — it
    surfaces only the world snapshot + goal state, never keyword/text matching.

    Args:
        world_state: A ``WorldState`` dataclass or an equivalent dict.
        world:       The connected world name (e.g. ``"minecraft"``) used to
                     namespace the verbs referenced in the cue.

    Returns:
        A ready-to-enqueue prompt string.
    """
    ws = world_state_to_dict(world_state)
    extra = ws["extra"]

    health = ws["health"]
    health_txt = f"{float(health):.0f}" if isinstance(health, (int, float)) else "?"
    position_txt = _fmt_position(ws["position"])

    entities_txt = _fmt_items(extra.get("entities") or [])
    blocks_txt = _fmt_items(extra.get("blocks") or [], key="name")
    inventory_txt = _fmt_items(extra.get("inventory") or [])
    affordances_txt = _fmt_affordances(extra.get("affordances") or [])
    goal_txt = _fmt_goal(extra.get("current_goal"))
    recent_txt = _fmt_items(extra.get("recent_goals") or [], key="description")
    has_goal = _has_current_goal(extra.get("current_goal"))

    prefix = f"vessel_{world}_"

    lines = [
        f"[Embodiment — you stopped moving and took a deliberate moment to "
        f"think about what you are doing in the {world} world.]",
        "",
        "You realised you were drifting without a clear purpose, so you paused "
        "on your own to sort out what you actually want to do next. Take this "
        "turn to think it through and come out of it with a clear objective.",
        "",
        "Where you are right now:",
        f"- Health: {health_txt}",
        f"- Position: {position_txt}",
        f"- Nearby entities/NPCs: {entities_txt}",
        f"- Nearby blocks of interest: {blocks_txt}",
        f"- Inventory: {inventory_txt}",
        f"- Things you could interact with: {affordances_txt}",
        f"- Your current goal: {goal_txt}",
        f"- Things you set out to do before: {recent_txt}",
        "",
    ]

    if not has_goal:
        lines.append(
            "Right now you have NO goal at all — that is why your body was just "
            "wandering. This pause is exactly the moment to choose one. Decide, "
            "in your own words and in character, what you genuinely want to be "
            "doing in this world — big or small — and set it now by calling "
            f"`{prefix}set_goal` with a free-text description. This is your own "
            "personal objective, chosen out of your own will and curiosity; "
            "nobody asked you to do it. Do not leave yourself aimless again — "
            "commit to something this turn."
        )
    else:
        lines.append(
            "You already have a goal, but it still has no concrete plan of "
            "steps — so your body has nothing specific to chase and just "
            "drifts. Use this pause to make it actionable: think about the very "
            "first concrete thing you need to do toward this goal given what is "
            "around you, and either take that step now with the fitting verb, "
            "or note your intent with "
            f"`{prefix}update_goal`. If, on reflection, this goal no longer "
            "feels right, you may choose a different one with "
            f"`{prefix}set_goal` instead. Either way, come out of this pause "
            "with a clear next move."
        )

    lines.extend(
        [
            "",
            "This is a *private* moment of reflection — no one is addressing "
            "you right now. Any conversation you can see above already happened "
            "and, if it needed a reply, you already gave one in a separate "
            "turn. Do NOT speak, greet, or repeat a message here; you are "
            "thinking, not talking. Return no `say` action. When someone "
            "actually speaks to you again you will get a separate turn to reply. "
            "For now, only shape your own intent (set or refine your goal).",
        ]
    )

    # Curated game-rule facts relevant to the goal/surroundings (reference
    # only), selected structurally by the connector into extra["knowledge"].
    lines.extend(_fmt_knowledge(extra.get("knowledge")))

    return "\n".join(lines)


def build_action_prompt(world_state: Any, world: str) -> str:
    """Build the **action beat** prompt from a ``WorldState``.

    This is the *concrete-doing* counterpart to :func:`build_will_prompt`. The
    will beat decides **what** Synth wants (its free-text goal); the action beat
    asks Synth to take a **real step toward the current goal right now** —
    walking to, gathering, crafting or placing the concrete things it needs.
    This is what closes the gap between "authored a goal" and "actually
    accomplished something": the motor tick moves the body reflexively, but only
    a cognition turn can decide *which* concrete verb (collect_block, craft,
    place, …) advances the plan, because the goal's meaning lives in free text
    the reflex must never read (keyword rule).

    Like the will beat this runs on the **Fast Lane** (a single ordinary
    cognition turn, no Agent Lane / Drone / mid-session diary). It is paced
    faster than the will beat (``VESSEL_ACTION_INTERVAL_SEC``) so play stays
    productive between the slow volition beats.

    Pure and keyword-free: it only surfaces the structured world snapshot (goal,
    current step, inventory, affordances, reachable block/entity ids) and asks
    Synth to pick the fitting verb. The verbs (``vessel_<world>_*``) themselves
    are injected by the normal prompt/action machinery.

    Args:
        world_state: A ``WorldState`` dataclass or an equivalent dict.
        world:       The connected world name (e.g. ``"minecraft"``).

    Returns:
        A ready-to-enqueue prompt string, or ``""`` when there is no active
        goal (nothing concrete to act on — the will beat runs first to author
        one).
    """
    ws = world_state_to_dict(world_state)
    extra = ws["extra"]

    goal = extra.get("current_goal")
    goal_txt = _fmt_goal(goal)
    if goal_txt == "none":
        # No goal to act on yet — the will beat authors intent first. Returning
        # an empty prompt lets the caller skip enqueuing this beat entirely.
        return ""

    health = ws["health"]
    health_txt = f"{float(health):.0f}" if isinstance(health, (int, float)) else "?"
    position_txt = _fmt_position(ws["position"])

    entities_txt = _fmt_items(extra.get("entities") or [])
    blocks_txt = _fmt_items(extra.get("blocks") or [], key="name")
    block_names = _distinct_names(extra.get("blocks") or [], key="name")
    entity_names = _distinct_names(extra.get("entities") or [], key="type")
    inventory_txt = _fmt_items(extra.get("inventory") or [])
    affordances_txt = _fmt_affordances(extra.get("affordances") or [])

    prefix = f"vessel_{world}_"

    lines = [
        f"[Embodiment — a moment to actually do something toward your goal in "
        f"the {world} world.]",
        "",
        "Where you are right now:",
        f"- Health: {health_txt}",
        f"- Position: {position_txt}",
        f"- Nearby entities/NPCs: {entities_txt}",
        f"- Nearby blocks of interest: {blocks_txt}",
        f"- Inventory: {inventory_txt}",
        f"- Things you could interact with: {affordances_txt}",
        f"- Your current goal: {goal_txt}",
        "",
        "This goal is your own — a personal objective you chose for yourself, "
        "not a task anyone gave you. Pursue it as your own intention.",
        "",
        "This is a moment of action, not reflection. You already decided what "
        "you want — now take ONE concrete step toward the current step of your "
        "goal, using what is actually around you above. Do not just think or "
        "restate the plan: pick the verb that moves the plan forward and use "
        "it now. Depending on what your current step needs, that might be:",
        f"- `{prefix}collect_block` to gather several of a block you can see "
        "(give its exact id as 'name' and how many as 'count') — this is the "
        "reliable way to stock up on a material;",
        f"- `{prefix}mine` to break one specific block you can see (by 'target');",
        f"- `{prefix}craft` to turn materials you already hold into something "
        "(by exact item id), or `{prefix}smelt` to cook/refine;",
        f"- `{prefix}place` to put a block down when you are building;",
        f"- `{prefix}goto` to walk to a spot or a thing you named, when the "
        "thing you need is out of reach.",
        "",
        "This is a moment to ACT, not to talk. Do NOT narrate your plan, "
        "announce your goal, or chat here — return no `say` action even if a "
        "player is nearby. Any conversation you can see above already happened "
        "and, if it needed a reply, you gave it in a separate reactive turn; "
        "when someone speaks to you again you will get another turn to answer. "
        "For now, just do the one concrete thing.",
        "",
        "Use the exact block/item/entity ids shown above — copy them verbatim, "
        "never invent one. Look at your inventory to judge what you still need "
        f"versus what you already have. When you have finished the current "
        f"step, call `{prefix}update_goal` with 'advance' set to true to move "
        "on; mark the whole goal 'done' when you have truly achieved it. You "
        "are the judge of your own progress.",
    ]

    # Curated game-rule facts relevant to the goal/surroundings (reference
    # only). The connector selected these structurally into extra["knowledge"];
    # rendering here is keyword-free. Helps pick the correct verb (e.g. that
    # iron ore needs a stone pickaxe first).
    lines.extend(_fmt_knowledge(extra.get("knowledge")))

    # Surface the exact reachable ids so the chosen 'name'/'target' is verbatim.
    if block_names:
        lines.append(
            "Blocks you can act on right now (exact ids): "
            + ", ".join(block_names)
            + "."
        )
    if entity_names:
        lines.append(
            "Creatures/NPCs you can act on right now (exact ids): "
            + ", ".join(entity_names)
            + "."
        )

    return "\n".join(lines)


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


def resolve_will_quiet_sec(config_get: Any, default: int = 60) -> int:
    """Return the **will-beat quiet window** in seconds, clamped.

    The will beat frames the moment as *"a quiet moment to reflect… on your
    own"* — it must therefore only fire when the world genuinely is quiet, i.e.
    no *player* has interacted with Synth recently. If a player has spoken to
    or acted toward Synth within this window, the autonomous volition turn is
    deferred so the player's message is handled as an ordinary reactive chat
    turn instead of being swallowed by a "you are alone" prompt.

    This is a structural, actor-based deferral (who acted recently), never
    keyword/intent matching. Reads ``VESSEL_WILL_QUIET_SEC``. Clamped to
    ``[0, 3600]`` — ``0`` disables the deferral entirely. Fail-safe: any error
    → ``default``.
    """
    try:
        value = int(config_get("VESSEL_WILL_QUIET_SEC", default))
    except Exception:
        return default
    return max(0, min(3600, value))


def is_action_beat_enabled(config_get: Any) -> bool:
    """Return whether the **action beat** (concrete-doing cognition) is enabled.

    The action beat is the middle layer between the slow will beat (volition)
    and the fast motor tick (reflex): a periodic Fast-Lane cognition turn that
    asks Synth to take one concrete verb toward its current goal (gather,
    craft, place, …). It is what turns an authored goal into accomplished work,
    since only cognition can map the goal's free-text meaning onto the right
    verb (the reflex must never read that text). Fail-safe: any error →
    ``False``.
    """
    try:
        return bool(config_get("VESSEL_ACTION_BEAT_ENABLED", True))
    except Exception:
        return False


def resolve_action_interval(config_get: Any, default: int = 20) -> int:
    """Return the **action beat** interval in seconds, clamped.

    Paced faster than the will beat (which authors intent) but slower than the
    motor tick (which just moves the body), so play stays productive without
    spamming cognition. Reads ``VESSEL_ACTION_INTERVAL_SEC``. Clamped to
    ``[3, 300]``. Fail-safe: any error → ``default``.
    """
    try:
        raw = config_get("VESSEL_ACTION_INTERVAL_SEC", default)
        value = int(raw)
    except Exception:
        return default
    return max(3, min(300, value))


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


def is_reflection_enabled(config_get: Any) -> bool:
    """Return whether the **reflection pause** is enabled.

    The reflection pause is the deliberate stop-and-think turn: when Synth is
    playing without a real objective (no goal, or a goal with no step plan) the
    scheduler prunes its own pending autonomous beats and dedicates one elevated
    cognition turn to authoring/refining the goal (see
    :func:`build_reflection_prompt`). Fail-safe: any error → ``True`` (on by
    default, matching the config registration).
    """
    try:
        return bool(config_get("VESSEL_REFLECTION_ENABLED", True))
    except Exception:
        return True


def resolve_reflection_duration(config_get: Any, default: int = 15) -> float:
    """Return the **reflection pause** duration in seconds, clamped.

    While reflecting, the scheduler holds off the slow will beat and the middle
    action beat (but never the fast motor tick — the body keeps moving) for this
    window, giving the elevated reflection turn room to be consumed and commit a
    goal before ordinary autonomy resumes. Reads
    ``VESSEL_REFLECTION_DURATION_SEC``. Clamped to ``[3, 300]``. Fail-safe: any
    error → ``default``.
    """
    try:
        value = float(config_get("VESSEL_REFLECTION_DURATION_SEC", default))
    except Exception:
        return float(default)
    return max(3.0, min(300.0, value))


def resolve_reflection_min_interval(config_get: Any, default: int = 60) -> float:
    """Return the **reflection anti-thrash floor** in seconds, clamped.

    A minimum interval that must elapse between two reflection pauses for the
    same world, so a persistently goal-less situation cannot fire a reflection
    turn on every scheduler tick and starve everything else. Reads
    ``VESSEL_REFLECTION_MIN_INTERVAL_SEC``. Clamped to ``[10, 3600]``.
    Fail-safe: any error → ``default``.
    """
    try:
        value = float(config_get("VESSEL_REFLECTION_MIN_INTERVAL_SEC", default))
    except Exception:
        return float(default)
    return max(10.0, min(3600.0, value))
