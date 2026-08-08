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


def _fmt_craft_deficit(craft_deficit: Any) -> list[str]:
    """Render the craft-material shortfall cue as prompt lines.

    ``craft_deficit`` is the opaque dict the connector placed in
    ``extra["craft_deficit"]`` — ``{"wanted": <item>, "missing": [{"item",
    "have", "need"}, ...]}`` — meaning Synth tried to craft ``wanted`` but was
    short on the listed ingredients. Returns an empty list when there is nothing
    to show, so the caller can skip the block. Purely structural rendering of
    ids + counts; it never inspects anything for keywords.
    """
    if not isinstance(craft_deficit, dict):
        return []
    wanted = str(craft_deficit.get("wanted") or "").strip()
    missing = craft_deficit.get("missing")
    if not wanted or not isinstance(missing, list) or not missing:
        return []
    parts: list[str] = []
    for entry in missing:
        if not isinstance(entry, dict):
            continue
        item = str(entry.get("item") or "").strip()
        if not item:
            continue
        try:
            have = int(entry.get("have") or 0)
            need = int(entry.get("need") or 0)
        except (TypeError, ValueError):
            continue
        if need <= 0:
            continue
        parts.append(f"{have}/{need} {item}")
    if not parts:
        return []
    return [
        "",
        f"You wished to build '{wanted}', but you do not have the materials "
        f"yet — you have {', '.join(parts)} (have/need). Gather the missing "
        "material first (harvest or craft the intermediate item you are short "
        "on), then try building again.",
    ]


def _fmt_goal_deficit(goal_deficit: Any) -> list[str]:
    """Render the active-goal material shortfall as prompt lines.

    ``goal_deficit`` is the opaque dict the connector placed in
    ``extra["goal_deficit"]`` — ``{"items": [{"item", "have", "need"}, ...]}`` —
    meaning the active goal needs items Synth does not yet hold in the required
    quantities. This is the *goal-level* counterpart of ``craft_deficit``: it
    tells Synth exactly what its own goal is short on (e.g. *"gather 20 oak
    logs"* with 5 in hand), so the will/action beats can pick the concrete next
    step (gather more logs) instead of drifting. Returns an empty list when
    there is nothing to show. Purely structural rendering of ids + counts; it
    never inspects anything for keywords.
    """
    if not isinstance(goal_deficit, dict):
        return []
    items = goal_deficit.get("items")
    if not isinstance(items, list) or not items:
        return []
    parts: list[str] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        item = str(entry.get("item") or "").strip()
        if not item:
            continue
        try:
            have = int(entry.get("have") or 0)
            need = int(entry.get("need") or 0)
        except (TypeError, ValueError):
            continue
        if need <= 0:
            continue
        if have >= need:
            continue
        parts.append(f"{have}/{need} {item}")
    if not parts:
        return []
    return [
        "",
        "Your current goal still needs materials you are short on (have/need): "
        f"{', '.join(parts)}. Make gathering/crafting the missing quantities "
        "your concrete next step.",
    ]


def _fmt_bases(bases: Any) -> list[str]:
    """Render Synth's registered bases (homes) as prompt lines.

    ``bases`` is the opaque list the connector placed in ``extra["bases"]`` —
    each entry a dict as returned by
    :func:`plugins.rift_vessel.vessel_bases.list_bases`
    (``{name, kind, anchor{x,y,z}, note, ...}``). Returns an empty list when
    there is nothing to show, so the caller can skip the block entirely. Purely
    structural rendering of names + coordinates; it never inspects any text for
    keywords.
    """
    if not isinstance(bases, list) or not bases:
        return []
    lines: list[str] = [
        "",
        "Your bases (places you built/claimed as home — you can return here to "
        "store things, shelter, sleep or respawn):",
    ]
    for entry in bases[:_MAX_LIST_ITEMS]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip() or "base"
        kind = str(entry.get("kind") or "").strip()
        anchor = entry.get("anchor")
        coord_txt = ""
        if isinstance(anchor, dict):
            ax = anchor.get("x")
            ay = anchor.get("y")
            az = anchor.get("z")
            if isinstance(ax, (int, float)) and isinstance(az, (int, float)):
                ay_txt = f", y={int(ay)}" if isinstance(ay, (int, float)) else ""
                coord_txt = f" at x={int(ax)}{ay_txt}, z={int(az)}"
        kind_txt = f" ({kind})" if kind and kind != "home" else ""
        lines.append(f"- {name}{kind_txt}{coord_txt}")
    if len(lines) <= 2:
        return []
    return lines


def _fmt_quest(quest: Any) -> list[str]:
    """Render the active quest (directed milestone) as prompt lines.

    ``quest`` is the opaque dict the connector placed in ``extra["quest"]`` — a
    single active quest as returned by
    :func:`plugins.rift_vessel.vessel_quests.get_active_quest`
    (``{quest_id, title, description, objectives, progress, ...}``). Returns an
    empty list when there is nothing to show, so the caller can skip the block
    entirely (an empty string block destabilises the LLM). Purely structural
    rendering of the title + its still-pending objectives; it never inspects any
    text for keywords, and it frames the quest as *reference* — a milestone to
    aim for, not a script — so Synth binds its own freely-authored goal to it
    only if it wants to.
    """
    if not isinstance(quest, dict):
        return []
    title = str(quest.get("title") or "").strip()
    if not title:
        return []
    lines: list[str] = [
        "",
        "Your current quest (a milestone you are working toward — treat it as a "
        "direction, not a script; you may bind the goal you author to it):",
        f"- {title}",
    ]
    description = str(quest.get("description") or "").strip()
    if description:
        lines.append(f"  {description}")
    # Surface still-pending objectives structurally (item/dimension/mob ids and
    # counts) so Synth knows what remains for this milestone.
    objectives = quest.get("objectives")
    if isinstance(objectives, list) and objectives:
        progress = quest.get("progress") or {}
        kills = progress.get("kills") if isinstance(progress, dict) else {}
        if not isinstance(kills, dict):
            kills = {}
        obj_lines: list[str] = []
        for obj in objectives[:_MAX_LIST_ITEMS]:
            if not isinstance(obj, dict):
                continue
            kind = str(obj.get("kind") or "").strip()
            target = obj.get("target")
            try:
                count = int(obj.get("count") or 1)
            except (TypeError, ValueError):
                count = 1
            if kind == "have_item" and target:
                obj_lines.append(f"  - have {count}x {target}")
            elif kind == "reach_dimension" and target:
                obj_lines.append(f"  - reach the {target}")
            elif kind == "has_base":
                obj_lines.append("  - have a base (home)")
            elif kind == "has_bed":
                obj_lines.append("  - have a bed to sleep / set respawn")
            elif kind == "kill":
                done = 0
                if target and isinstance(kills.get(str(target).lower()), int):
                    done = int(kills.get(str(target).lower()) or 0)
                what = target or "any hostile"
                obj_lines.append(f"  - defeat {count}x {what} ({done}/{count})")
        if obj_lines:
            lines.append("  objectives:")
            lines.extend(obj_lines)
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
    ``motor_step``), which runs with no prompt at all. The prompt also insists
    the goal be a *meaningful, strategic achievement* (a multi-action outcome
    with real value) and explicitly forbids low-level goals that merely
    describe a movement, an orientation, a single click/interaction or picking
    up one block — those are how the body carries out a goal, never the goal.

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
        f"[Embodiment — you are IN GAME, playing {world} right now. This is a "
        "brief planning beat, not a diary. Be pragmatic and concrete about "
        f"what to do in {world}; do NOT write an introspective monologue, "
        "poetic narration, or philosophical musings about your existence.]",
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
            f"*** YOU HAVE NO GOAL. You MUST author one THIS TURN by calling "
            f"`{prefix}set_goal`. *** Without a goal your body just wanders and "
            "achieves nothing. This is the single most important thing to do "
            "now. Decide, in your own words and in character, what you want to "
            "pursue in this world, and set it."
        )
    else:
        lines.append(
            "This is a moment of will, not motion. Look at your current goal: "
            "does it still feel right? If yes, leave it as it is (or use "
            f"`{prefix}update_goal` to note progress or re-aim) — do NOT "
            f"re-state it. Only call `{prefix}set_goal` if you genuinely want a "
            "*different* objective; it replaces your whole plan and discards "
            "any steps already worked out."
        )
    lines.extend(
        [
            "",
            "A GOAL IS A MEANINGFUL ACHIEVEMENT — an outcome with strategic "
            "value that takes SEVERAL actions to reach. It is a destination, "
            "not a footstep. Name what you want to END UP WITH or accomplish, "
            "never the next movement. Goals are reached by DOING — gathering, "
            "mining, crafting, building — never by watching: an objective you "
            "could only satisfy by observing, scanning or chatting is not a "
            "goal, it is a pause. It is FORBIDDEN to set a goal that is "
            "just a low-level act: moving, turning, facing, a single click or "
            "interaction, picking up one block, or gathering a small pile of "
            "one material. Those are how your body carries out a goal on its "
            "own — never the goal itself. If what you are about to write could "
            "be finished by one or two motor moves, it is TOO SMALL: zoom out "
            "and name the larger result those small acts would serve.",
            "",
            "This goal is entirely your own — a personal objective YOU chose "
            "out of your own will and curiosity; nobody asked you and there is "
            "no requester. Keep it realistic for where you are and what you "
            "actually have: look at your Inventory and Position and pick "
            "something you could genuinely START now with what is around you, "
            "one honest step ahead of your current means — not a far-off "
            "endgame prize. A bigger dream is fine as motivation, but set the "
            "concrete next result that moves you toward it from here.",
            "",
            "State the goal itself in your own words — do NOT spell out ordered "
            "sub-steps yourself. A separate planning pass will work out the "
            f"requirements (you may consult `{prefix}lookup_knowledge`) and "
            "fill the concrete steps in for you shortly after; leave 'steps' "
            f"empty. When you finish a step, call `{prefix}update_goal` with "
            "'advance' true; mark the goal 'done' when truly achieved, or "
            "'abandoned' if you change your mind. You judge your own progress.",
            "",
            "If what you want is *not here* (you want wood but see no trees, or "
            "a different biome), wandering in circles will not get you there: "
            "pick a direction and set 'destination_x'/'destination_z' (offset "
            "your position a few dozen blocks toward where you should go). Your "
            "body will walk that way on its own; re-aim later. Leave them out "
            "only when what you need is already right here.",
            "",
            "This is a *private* moment — no one is addressing you now. Do NOT "
            "speak, greet or repeat a message; only shape your own intent (set "
            "or update your goal) and return no `say` action.",
        ]
    )

    # Curated game-rule facts relevant to the goal/surroundings (reference
    # only). The connector selected these structurally into extra["knowledge"];
    # rendering here is keyword-free.
    lines.extend(_fmt_knowledge(extra.get("knowledge")))

    # Registered bases (homes). The connector resolved these structurally into
    # extra["bases"]; surface them so the will remembers it has a home to build
    # up, store resources in, or return to at night. Rendering is keyword-free.
    lines.extend(_fmt_bases(extra.get("bases")))

    # The active quest (directed milestone) the connector resolved structurally
    # into extra["quest"]. Reference only — a direction to bind the will to, not
    # a script. Skipped entirely when absent (never an empty block).
    lines.extend(_fmt_quest(extra.get("quest")))

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

    # Structural stall cue. The slow goal debrief (core.vessel_goal_debrief)
    # fingerprints the active goal and, when it sits unchanged for too many
    # checks, arms this flag on ``extra`` (a fixed structural token, never
    # parsed text). Surface it so Synth reconsiders instead of grinding a goal
    # that is not moving — very often the goal is already completable with what
    # it holds, and the honest move is to declare it done or switch approach.
    if bool(extra.get("goal_stalled")):
        lines.extend(
            [
                "",
                "Heads up: your current goal has not moved for a while. Step "
                "back and reconsider it: is it actually already done with what "
                "you have on you right now? If so, mark it complete and pick a "
                "new one. If not, change your approach — a different target, a "
                "different place, or a simpler next step — rather than pushing "
                "the same plan that is not working.",
            ]
        )

    # Craft-material shortfall cue. When a craft failed for missing ingredients
    # the connector latches the exact shortfall for a few turns; surface it so
    # Synth knows what intermediate material to gather next instead of retrying
    # the same impossible craft. Structural (item ids + counts), keyword-free.
    lines.extend(_fmt_craft_deficit(extra.get("craft_deficit")))

    # Goal-level material shortfall. The connector computes what the ACTIVE goal
    # still needs (have/need per named product/target) and places it in
    # ``extra["goal_deficit"]``. Surface it so the will beat can pick the
    # concrete next step (gather the missing quantity) instead of drifting —
    # the "runs around with nothing to do" gap. Structural, keyword-free.
    lines.extend(_fmt_goal_deficit(extra.get("goal_deficit")))

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
            "night_shelter": "night fell with hostile creatures around and your "
            "instincts made you take shelter",
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

    # Structural death cue. The connector records the numeric death position and
    # a monotonic death count in ``extra["last_death"]`` (from the bridge's death
    # event — coordinates + a count, never text). When present, tell Synth
    # exactly where it keeps dying and press it to RECONSIDER its approach — the
    # single biggest failure mode is respawning and resuming the identical goal
    # straight back into the same death loop. Purely structural: we read numeric
    # coordinates and a count, never parse any message.
    last_death = extra.get("last_death")
    if isinstance(last_death, dict):
        dx = last_death.get("x")
        dy = last_death.get("y")
        dz = last_death.get("z")
        count = last_death.get("count")
        if isinstance(dx, (int, float)) and isinstance(dz, (int, float)):
            where = f"x={int(dx)}, y={int(dy) if isinstance(dy, (int, float)) else '?'}, z={int(dz)}"
            times = (
                f" — this is death #{int(count)} in this world"
                if isinstance(count, (int, float)) and count
                else ""
            )
            lines.extend(
                [
                    "",
                    f"IMPORTANT — you have DIED here (at {where}){times}. Do NOT "
                    "simply pick up the same goal and walk straight back into "
                    "what killed you: that is how a death loop starts. RECONSIDER "
                    "your approach this turn. Options: pick a SAFER goal you can "
                    "pursue away from that spot; move somewhere else first by "
                    "setting 'destination_x'/'destination_z' a good distance from "
                    f"({int(dx)}, {int(dz)}); or make survival itself the goal "
                    "(gather better gear / build a safe base) before returning. "
                    "Change something real — do not repeat the same plan.",
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
        f"[Embodiment — you are IN GAME, playing {world} right now. You stopped "
        "to pick a concrete objective because you were drifting without one. "
        "Be pragmatic: decide what to DO in "
        f"{world}; do NOT write an introspective monologue, poetic narration, "
        "or philosophical musings about your existence.]",
        "",
        "You were drifting without a clear purpose, so you paused on your own "
        "to sort out what you actually want to do next. Take this turn to work "
        "it out and come out of it with a clear objective.",
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
            "You have no active goal at all — that is why your body was just "
            "wandering aimlessly. Decide, in your own words and in character, "
            "what you genuinely want to pursue in this world, and set it now "
            f"with `{prefix}set_goal` and a free-text description. It is your "
            "own personal objective, chosen out of your own will and curiosity "
            "— nobody asked you. Make it MEAN something: a MEANINGFUL "
            "ACHIEVEMENT with an end, an outcome worth SEVERAL actions — never "
            "a bare activity with no end, never a single low-level move. Say "
            "what the effort is FOR — what you want to END UP WITH — so you "
            "know when you are done. Keep it realistic for what you actually "
            "hold and where you are (see your Inventory and Position above): "
            "something you could genuinely START now, one honest step ahead of "
            "your means, not a far-off prize."
        )
    else:
        lines.append(
            "You already have a goal, but it still has no concrete plan of "
            "steps — so your body has nothing specific to chase and just "
            "drifts. First be honest about the goal itself: is it a real "
            "achievement with an end, or just open-ended wandering/gathering "
            "with no point? If it is vague or trivial, replace it now via "
            f"`{prefix}set_goal` with a purposeful one that says what the "
            "effort is FOR — a meaningful outcome worth several actions — kept "
            "realistic for what you hold and where you are. If the goal is "
            "already meaningful, make it actionable instead: pick the very "
            "first concrete thing you need to do toward it given what is around "
            "you, and take that step now with the fitting verb, or note your "
            f"intent with `{prefix}update_goal`. Come out of this pause with a "
            "clear next move."
        )

    lines.extend(
        [
            "",
            "This is a *private* moment of reflection — no one is addressing "
            "you now. Do NOT speak, greet or repeat a message; only shape your "
            "own intent (set or refine your goal) and return no `say` action.",
        ]
    )

    # Curated game-rule facts relevant to the goal/surroundings (reference
    # only), selected structurally by the connector into extra["knowledge"].
    lines.extend(_fmt_knowledge(extra.get("knowledge")))

    # Registered bases (homes), keyword-free — so a reflection turn can decide
    # to build up / return to an existing home instead of drifting.
    lines.extend(_fmt_bases(extra.get("bases")))

    # The active quest (directed milestone), reference only — so a reflection
    # turn can commit a goal aligned with the current milestone.
    lines.extend(_fmt_quest(extra.get("quest")))

    return "\n".join(lines)


def build_goal_prompt(world_state: Any, world: str) -> str:
    """Build the **goal beat** prompt from a ``WorldState``.

    This is a dedicated *single-purpose* volition turn whose ONLY job is to set
    (or, when one already exists, refine) Synth's free-text goal — nothing else.
    It is structurally similar to the will/reflection beat (same in-character
    persona framing, same structural world snapshot) but deliberately narrow:
    the scheduler that enqueues it restricts the turn's action allowlist to just
    ``{prefix}set_goal`` (and ``{prefix}update_goal`` when a goal already
    exists), so the turn *cannot* fall back to a passive verb
    (observe/status/wait) the way a weaker tool-calling model tends to on the
    broader reflection beat. Because the allowlist is enforced by the beat's
    scheduler (not this prompt), the wording here only needs to be a clean,
    in-character request to name a goal — the persona/profile itself is injected
    by the normal system-prompt machinery.

    Fast-Lane only (a single ordinary cognition turn, no Agent Lane / Drone /
    mid-session diary) and purely structural — it surfaces only the world
    snapshot + goal state, never keyword/text matching.

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
        f"[Embodiment — you are IN GAME, playing {world} right now. This turn "
        "exists for ONE thing only: to decide what you want to pursue and "
        f"commit it as a goal. Be pragmatic and concrete about what to do in "
        f"{world}; do NOT write an introspective monologue, poetic narration, "
        "or philosophical musings about your existence.]",
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
            "You have no active goal. Decide, in your own words and in "
            "character, what you genuinely want to pursue in this world, and "
            f"commit it with `{prefix}set_goal` and a free-text description. It "
            "is your own personal objective, chosen out of your own will and "
            "curiosity — nobody asked you. Make it MEAN something: a MEANINGFUL "
            "ACHIEVEMENT with an end, an outcome worth SEVERAL actions — never "
            "a bare activity with no end, never a single low-level move. "
            "Choose something you will reach by DOING — gathering, mining, "
            "crafting, building — never by watching: an objective you could "
            "only satisfy by observing, scanning or chatting is not a goal, it "
            "is a pause. Say "
            "what the effort is FOR — what you want to END UP WITH — so you "
            "know when you are done. Keep it realistic for what you actually "
            "hold and where you are (see your Inventory and Position above): "
            "something you could genuinely START now, one honest step ahead of "
            "your means, not a far-off prize."
        )
    else:
        lines.append(
            "You already have a goal. Look at it honestly: is it still a real "
            "achievement worth pursuing, or has it become vague or done? If it "
            f"still feels right, note progress or re-aim with `{prefix}update_"
            f"goal`. If you genuinely want a *different* objective, replace it "
            f"with `{prefix}set_goal` and a free-text description — a meaningful "
            "outcome worth several actions, kept realistic for what you hold "
            "and where you are."
        )

    lines.extend(
        [
            "",
            "State the goal itself in your own words — do NOT spell out ordered "
            "sub-steps yourself; a separate planning pass fills those in "
            "shortly after. Leave 'steps' empty.",
            "",
            "This is a *private* moment — no one is addressing you now. Do NOT "
            "speak, greet or repeat a message; only shape your own intent by "
            f"calling `{prefix}set_goal` (or `{prefix}update_goal`) and return "
            "no `say` action.",
        ]
    )

    # Curated game-rule facts relevant to the goal/surroundings (reference
    # only), selected structurally by the connector into extra["knowledge"].
    lines.extend(_fmt_knowledge(extra.get("knowledge")))

    # Registered bases (homes), keyword-free — so the goal can build up / return
    # to an existing home instead of drifting.
    lines.extend(_fmt_bases(extra.get("bases")))

    # The active quest (directed milestone), reference only — so the goal can be
    # committed aligned with the current milestone.
    lines.extend(_fmt_quest(extra.get("quest")))

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
        f"[Embodiment — you are IN GAME, playing {world} right now. Time to "
        f"actually do something toward your goal in {world}. Be pragmatic and "
        "concrete; do NOT write an introspective monologue, poetic narration, "
        "or philosophical musings about your existence.]",
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
        "REMEMBER — survival and progress come from DIRECT ACTION, not from "
        "watching. Observing, checking status, scanning the area and chatting "
        "are BACKGROUND: they never gather stone, never craft a tool, never "
        "smelt ore, never build a shelter. If your current step needs a "
        "material, an object or a structure, an observation or a chat verb is "
        "a stall, not a choice — the only verbs that actually move the plan "
        f"forward are `{prefix}collect_block`, `{prefix}mine`, `{prefix}craft`, "
        f"`{prefix}smelt` and `{prefix}place`. Pick the one that delivers what "
        "your step asks for and use it now. You survive with your hands, not "
        "your eyes.",
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

    # Craft-material shortfall cue (see build_will_prompt). Surface it here too
    # so the action beat gathers the missing intermediate instead of retrying
    # the impossible craft. Structural (item ids + counts), keyword-free.
    lines.extend(_fmt_craft_deficit(extra.get("craft_deficit")))

    # Goal-level material shortfall (see build_will_prompt). Surface it here too
    # so the action beat picks the concrete "gather the missing quantity" step
    # instead of drifting with nothing to do. Structural, keyword-free.
    lines.extend(_fmt_goal_deficit(extra.get("goal_deficit")))

    # Registered bases (homes), keyword-free — so a concrete step can head home
    # to build/store instead of leaving resources scattered.
    lines.extend(_fmt_bases(extra.get("bases")))

    # The active quest (directed milestone), reference only — so the concrete
    # step advances the current milestone when it fits.
    lines.extend(_fmt_quest(extra.get("quest")))

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


def build_damage_appraisal_prompt(world_state: Any, world: str) -> str:
    """Build the **post-damage appraisal** prompt from a ``WorldState``.

    Fired reactively (at an elevated priority) right after Synth *took* damage.
    The fast survival reflex already reacted mechanically (fought back, fled,
    surfaced…); this turn is the *cognitive* appraisal of "I just got hurt —
    what do I actually want to do about it?". It is Synth's chance to make a
    deliberate combat choice — press the attack with its best weapon, switch to
    a ranged shot, disengage and heal, or (if a *player* struck it) respond
    socially rather than reflexively swinging back.

    Routing between a **combat** framing and a **social** framing is decided by
    the caller purely from structural metadata (the damage source kind), never
    from message text — this function only renders whichever framing it is
    told. When ``extra["damage_from_player"]`` is truthy the prompt leans
    social (a person hit you — decide how to respond, do not reflexively
    attack); otherwise it leans combat (a hostile creature hurt you — fight
    smart).

    Pure and keyword-free: it surfaces only the structured combat snapshot
    (health, the damage magnitude, nearby aggressors, ranged readiness, best
    melee damage) and lets Synth choose. The verbs (``vessel_<world>_*``) are
    injected by the normal action machinery.

    Args:
        world_state: A ``WorldState`` dataclass or an equivalent dict.
        world:       The connected world name (e.g. ``"minecraft"``).

    Returns:
        A ready-to-enqueue prompt string.
    """
    ws = world_state_to_dict(world_state)
    extra = ws["extra"]

    health = ws["health"]
    health_txt = f"{float(health):.0f}" if isinstance(health, (int, float)) else "?"
    position_txt = _fmt_position(ws["position"])

    damage = extra.get("damage_taken")
    damage_txt = f"{float(damage):.0f}" if isinstance(damage, (int, float)) else "some"

    entities_txt = _fmt_items(extra.get("entities") or [])
    inventory_txt = _fmt_items(extra.get("inventory") or [])
    affordances_txt = _fmt_affordances(extra.get("affordances") or [])

    has_ranged = bool(extra.get("has_ranged_weapon"))
    ranged_ammo = extra.get("ranged_ammo")
    ammo_txt = str(ranged_ammo) if isinstance(ranged_ammo, (int, float)) else "?"
    best_melee = extra.get("best_melee_damage")
    melee_txt = (
        f"{float(best_melee):.0f}"
        if isinstance(best_melee, (int, float)) and best_melee
        else "bare hands"
    )
    from_player = bool(extra.get("damage_from_player"))

    prefix = f"vessel_{world}_"

    lines = [
        f"[Embodiment — you are IN GAME, playing {world} right now, and you "
        "were just HURT. Take stock and react pragmatically; do NOT write an "
        "introspective monologue or poetic narration.]",
        "",
        "What just happened:",
        f"- You took about {damage_txt} damage.",
        f"- Health now: {health_txt}",
        f"- Position: {position_txt}",
        f"- Around you: {entities_txt}",
        f"- Things you could interact with: {affordances_txt}",
        f"- Inventory: {inventory_txt}",
        f"- Best melee weapon damage you carry: {melee_txt}",
        (
            f"- Ranged weapon ready: yes ({ammo_txt} arrows)"
            if has_ranged
            else "- Ranged weapon ready: no"
        ),
        "",
    ]

    if from_player:
        lines.extend(
            [
                "A *person* struck you — this is a social situation, not just a "
                "fight. Do NOT reflexively swing back. Decide, in character, "
                "how you feel and want to respond: you might speak to them "
                f"(`{prefix}say`), warn them, forgive it, walk away, or — only "
                "if you genuinely choose to — defend yourself. Let your persona "
                "and mood drive the choice; a person is not a monster.",
            ]
        )
    else:
        lines.extend(
            [
                "A hostile creature hurt you. Your instincts already reacted; "
                "this is your moment to fight *smart*, not just flail. Decide "
                "deliberately what to do next:",
                f"- Press the attack with your best weapon (`{prefix}attack`) "
                "if the enemy is close and you can win the trade;",
                (
                    f"- Loose a shot (`{prefix}shoot`) if the enemy is at a "
                    "distance and you have a bow/crossbow with arrows ready "
                    f"(you do — {ammo_txt} arrows);"
                    if has_ranged
                    else "- You have no ranged option right now, so close in "
                    "and melee or disengage;"
                ),
                "- Break off and heal/retreat if your health is low and the "
                "trade is not worth it.",
                "",
                "Choose based on the numbers above (your health, how far the "
                "enemy is, whether you can out-damage it), not on habit. "
                "Survival first, but do not run from a fight you can win.",
            ]
        )

    # Curated game-rule facts (reference only), selected structurally by the
    # connector into extra["knowledge"] — e.g. which mobs shoot from range.
    lines.extend(_fmt_knowledge(extra.get("knowledge")))

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


def is_goal_beat_enabled(config_get: Any) -> bool:
    """Return whether the dedicated **goal beat** is enabled.

    The goal beat is a single-purpose volition turn whose only job is to author
    or refine Synth's free-text goal (see :func:`build_goal_prompt`), with the
    turn's action allowlist hard-restricted to ``set_goal``/``update_goal`` by
    the scheduler that enqueues it. It exists so a weaker tool-calling model
    cannot fall back to a passive verb when a goal is needed. Reads
    ``VESSEL_GOAL_BEAT_ENABLED``. Fail-safe: any error → ``True`` (on by
    default).
    """
    try:
        return bool(config_get("VESSEL_GOAL_BEAT_ENABLED", True))
    except Exception:
        return True


def resolve_goal_beat_interval(config_get: Any, default: int = 45) -> int:
    """Return the **goal beat** interval in seconds, clamped.

    Paces how often the dedicated goal-setting turn may fire while Synth has no
    active goal. Reads ``VESSEL_GOAL_BEAT_INTERVAL_SEC``. Clamped to
    ``[10, 3600]``. Fail-safe: any error → ``default``.
    """
    try:
        raw = config_get("VESSEL_GOAL_BEAT_INTERVAL_SEC", default)
        value = int(raw)
    except Exception:
        return default
    return max(10, min(3600, value))
