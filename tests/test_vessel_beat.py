"""Tests for the Rift Vessel autonomous decision beat (``core/vessel_beat.py``).

The decision beat is Synth's world-agnostic autonomous-play loop (AGENTS.md
§5c). These tests cover the **pure** prompt-building helpers — no DB, no bridge,
no LLM — verifying that:

* the prompt is built structurally from the ``WorldState`` contract
  (position, health, inventory, affordances, current/available goals) and never
  from keyword/text matching;
* both a ``WorldState`` dataclass and a plain dict are accepted;
* the exposed-verb namespace (``vessel_<world>_…``) is surfaced in the cue;
* autonomy gating and the beat-interval clamp are fail-safe.
"""

from __future__ import annotations

from typing import Any

from core.vessel_beat import (
    _fmt_bases,
    _fmt_quest,
    build_action_prompt,
    build_damage_appraisal_prompt,
    build_decision_prompt,
    build_goal_prompt,
    build_reflection_prompt,
    build_will_prompt,
    is_action_beat_enabled,
    is_autonomy_enabled,
    is_goal_beat_enabled,
    is_motor_enabled,
    is_reflection_enabled,
    resolve_action_interval,
    resolve_beat_interval,
    resolve_goal_beat_interval,
    resolve_motor_interval,
    resolve_reflection_duration,
    resolve_reflection_min_interval,
    resolve_will_interval,
    resolve_will_quiet_sec,
    world_state_to_dict,
)


# ---------------------------------------------------------------------------
# world_state_to_dict — accepts dataclass or dict
# ---------------------------------------------------------------------------


def test_world_state_to_dict_from_dataclass() -> None:
    from plugins.rift_vessel.vessel_base import WorldState

    ws = WorldState(
        environment="minecraft",
        health=17.0,
        position={"x": 10.4, "y": 64.0, "z": -3.9},
        possible_actions=["say", "move"],
        flags={"connected": True, "is_day": True},
        extra={"inventory": [{"name": "oak_log", "count": 3}]},
    )
    out = world_state_to_dict(ws)
    assert out["environment"] == "minecraft"
    assert out["health"] == 17.0
    assert out["position"] == {"x": 10.4, "y": 64.0, "z": -3.9}
    assert out["flags"]["is_day"] is True
    assert out["extra"]["inventory"][0]["name"] == "oak_log"


def test_world_state_to_dict_from_plain_dict() -> None:
    out = world_state_to_dict({"environment": "minecraft", "health": 20})
    assert out["environment"] == "minecraft"
    assert out["health"] == 20
    # Missing fields degrade to safe empty defaults.
    assert out["possible_actions"] == []
    assert out["flags"] == {}
    assert out["extra"] == {}


def test_world_state_to_dict_defaults_environment() -> None:
    out = world_state_to_dict({})
    assert out["environment"] == "world"


# ---------------------------------------------------------------------------
# build_decision_prompt — structural, keyword-free
# ---------------------------------------------------------------------------


def _rich_world_state() -> dict[str, Any]:
    return {
        "environment": "minecraft",
        "health": 15.0,
        "position": {"x": 128.0, "y": 63.0, "z": -42.0},
        "possible_actions": ["say", "move", "mine", "goto"],
        "flags": {"connected": True, "is_day": False},
        "extra": {
            "time_of_day": 18000,
            "entities": [
                {"name": "Steve", "type": "player", "distance": 4.2},
                {"name": "zombie", "type": "mob", "distance": 9.0},
            ],
            "blocks": [
                {"name": "oak_log", "distance": 3.0},
                {"name": "stone", "distance": 1.5},
            ],
            "inventory": [
                {"name": "oak_log", "count": 5},
                {"name": "cobblestone", "count": 12},
            ],
            "affordances": [
                {
                    "kind": "entity",
                    "target": "Steve",
                    "verb": "follow",
                    "distance": 4.2,
                },
                {"kind": "block", "target": "oak_log", "verb": "mine", "distance": 3.0},
            ],
            "current_goal": {
                "id": 3,
                "session_id": "s1",
                "description": "explore the caves and see what I find",
                "note": "just started digging down",
                "status": "active",
            },
            "recent_goals": [
                {"description": "build a cozy little house by the lake"},
                {"description": "wander and meet the villagers"},
            ],
        },
    }


def test_prompt_contains_world_and_verb_namespace() -> None:
    prompt = build_decision_prompt(_rich_world_state(), "minecraft")
    # The world name is surfaced via the pragmatic in-game framing.
    assert "playing minecraft" in prompt
    # The verb namespace cue must be present so Synth picks vessel_<world>_ verbs.
    assert "vessel_minecraft_" in prompt


def test_prompt_surfaces_structured_state() -> None:
    prompt = build_decision_prompt(_rich_world_state(), "minecraft")
    # Position, health, inventory, entities, blocks, affordances, goals.
    assert "x=128" in prompt and "y=63" in prompt and "z=-42" in prompt
    assert "Health: 15" in prompt
    assert "oak_log" in prompt
    assert "Steve" in prompt
    assert "zombie" in prompt
    # Affordance contract is rendered as verb → target.
    assert "follow → Steve" in prompt
    assert "mine → oak_log" in prompt
    # The self-authored current goal (free text) and its note are surfaced.
    assert "explore the caves and see what I find" in prompt
    assert "just started digging down" in prompt
    # Goals Synth set out to do before are shown as free text, no fixed menu.
    assert "build a cozy little house by the lake" in prompt
    assert "wander and meet the villagers" in prompt
    # Night is derived from the is_day flag, not any keyword in text.
    assert "night" in prompt


def test_prompt_surfaces_equipment() -> None:
    """The world-state block shows what the body is holding — or bare hands."""
    state = _rich_world_state()
    # No equipped_item → explicit bare-hands line.
    assert "- Equipment: none — you are using your bare hands" in build_decision_prompt(
        state, "minecraft"
    )
    state["extra"] = dict(state.get("extra") or {})
    state["extra"]["equipped_item"] = "wooden_axe"
    assert "- Equipment: wooden_axe" in build_decision_prompt(state, "minecraft")


def test_prompt_handles_empty_world_state() -> None:
    prompt = build_decision_prompt({}, "minecraft")
    # Degrades gracefully — no crash, sane placeholders.
    assert "Health: ?" in prompt
    assert "Position: unknown" in prompt
    assert "Nearby entities/NPCs: none" in prompt
    assert "Your current goal: none" in prompt
    assert "vessel_minecraft_" in prompt


def test_prompt_truncates_long_lists() -> None:
    many = {
        "extra": {"inventory": [{"name": f"item{i}", "count": 1} for i in range(20)]}
    }
    prompt = build_decision_prompt(many, "minecraft")
    # Only the first _MAX_LIST_ITEMS are shown, with an ellipsis marker.
    assert "…" in prompt
    assert "item0" in prompt
    assert "item19" not in prompt


# ---------------------------------------------------------------------------
# _fmt_bases — structural rendering of Synth's registered homes
# ---------------------------------------------------------------------------


def test_fmt_bases_empty_returns_no_lines() -> None:
    assert _fmt_bases(None) == []
    assert _fmt_bases([]) == []
    assert _fmt_bases("not-a-list") == []


def test_fmt_bases_renders_name_kind_and_coords() -> None:
    bases = [
        {
            "name": "Lakeside home",
            "kind": "home",
            "anchor": {"x": 12.7, "y": 65.0, "z": -4.2},
        },
        {
            "name": "mine outpost",
            "kind": "outpost",
            "anchor": {"x": 100.0, "y": 12.0, "z": 8.0},
        },
    ]
    lines = _fmt_bases(bases)
    text = "\n".join(lines)
    # Header framing (home context) is present.
    assert "Your bases" in text
    # Coordinates are truncated to ints; a "home" kind is not labelled, a
    # non-home kind is shown in parentheses.
    assert "- Lakeside home at x=12, y=65, z=-4" in text
    assert "- mine outpost (outpost) at x=100, y=12, z=8" in text


def test_fmt_bases_handles_missing_and_partial_anchor() -> None:
    bases = [
        {"name": "floating claim"},  # no anchor at all
        {"name": "flat spot", "anchor": {"x": 5.0, "z": 9.0}},  # no y
    ]
    lines = _fmt_bases(bases)
    text = "\n".join(lines)
    assert "- floating claim" in text  # rendered with no coords, no crash
    assert "- flat spot at x=5, z=9" in text  # y omitted when not numeric
    assert "y=" not in "\n".join(li for li in lines if "flat spot" in li)


def test_fmt_bases_defaults_blank_name_to_base() -> None:
    lines = _fmt_bases([{"name": "   ", "anchor": {"x": 1.0, "y": 2.0, "z": 3.0}}])
    assert any(li.startswith("- base") for li in lines)


# ---------------------------------------------------------------------------
# _fmt_quest — structural rendering of the active directed quest
# ---------------------------------------------------------------------------


def test_fmt_quest_empty_returns_no_lines() -> None:
    assert _fmt_quest(None) == []
    assert _fmt_quest("not-a-dict") == []
    # A quest with no title renders nothing (an empty block destabilises the LLM).
    assert _fmt_quest({"title": "   "}) == []


def test_fmt_quest_renders_title_description_and_framing() -> None:
    lines = _fmt_quest({"title": "Establish your first base", "description": "home"})
    blob = "\n".join(lines)
    assert "Establish your first base" in blob
    assert "direction, not a script" in blob
    assert "home" in blob


def test_fmt_quest_renders_pending_objectives_structurally() -> None:
    quest = {
        "title": "Slay the Ender Dragon",
        "objectives": [
            {"kind": "have_item", "target": "ender_eye", "count": 12},
            {"kind": "reach_dimension", "target": "the_end"},
            {"kind": "has_base"},
            {"kind": "has_bed"},
            {"kind": "kill", "target": "ender_dragon", "count": 1},
        ],
        "progress": {"kills": {"ender_dragon": 0}},
    }
    lines = _fmt_quest(quest)
    blob = "\n".join(lines)
    assert "have 12x ender_eye" in blob
    assert "reach the the_end" in blob
    assert "have a base (home)" in blob
    assert "have a bed to sleep / set respawn" in blob
    assert "defeat 1x ender_dragon (0/1)" in blob


def test_fmt_quest_kill_progress_reflects_counter() -> None:
    quest = {
        "title": "Clear the mobs",
        "objectives": [{"kind": "kill", "target": "zombie", "count": 5}],
        "progress": {"kills": {"zombie": 3}},
    }
    assert any("defeat 5x zombie (3/5)" in li for li in _fmt_quest(quest))


# ---------------------------------------------------------------------------
# Autonomy gating + interval clamp — fail-safe
# ---------------------------------------------------------------------------


def test_is_autonomy_enabled_reads_flag() -> None:
    assert is_autonomy_enabled(lambda k, d: True) is True
    assert is_autonomy_enabled(lambda k, d: False) is False


def test_is_autonomy_enabled_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("config down")

    assert is_autonomy_enabled(_boom) is False


def test_resolve_beat_interval_default_and_clamp() -> None:
    assert resolve_beat_interval(lambda k, d: d, default=45) == 45
    # Clamped to the [10, 3600] range.
    assert resolve_beat_interval(lambda k, d: 1) == 10
    assert resolve_beat_interval(lambda k, d: 999999) == 3600
    assert resolve_beat_interval(lambda k, d: 60) == 60


def test_resolve_beat_interval_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("config down")

    assert resolve_beat_interval(_boom, default=45) == 45


# ---------------------------------------------------------------------------
# Two-speed autonomy: volition (will beat) vs motorics (motor tick)
# ---------------------------------------------------------------------------


def test_build_will_prompt_is_volition_focused() -> None:
    prompt = build_will_prompt(_rich_world_state(), "minecraft")
    # It surfaces the same structured snapshot as the decision prompt…
    # Pragmatic, in-game framing that names the world and curbs monologue prose.
    assert "IN GAME" in prompt
    assert "playing minecraft" in prompt
    assert "monologue" in prompt.lower()
    assert "vessel_minecraft_set_goal" in prompt
    assert "vessel_minecraft_update_goal" in prompt
    # …but frames the turn as *will, not motion* and tells Synth its body
    # will move on its own once the goal is clear (motorics is separate).
    lowered = prompt.lower()
    assert "will, not motion" in lowered
    assert "body will walk that way on its own" in lowered


def test_build_will_prompt_pushes_set_goal_when_no_goal() -> None:
    # Structural branch: with no active goal the will beat must actively push
    # Synth to author one this turn (not merely permit it) so the goal
    # expander has an empty-steps goal to fill. Keyword-free: the branch keys
    # off the structural presence of current_goal, never its text.
    state = {"extra": {}}  # no current_goal at all
    prompt = build_will_prompt(state, "minecraft")
    lowered = prompt.lower()
    assert "you have no goal" in lowered
    assert "you must author one this turn" in lowered
    assert "vessel_minecraft_set_goal" in prompt
    # The discouraging "only call set_goal when you want a *different*
    # objective" caution belongs to the has-goal branch and must be absent.
    assert "different* objective" not in prompt


def test_build_will_prompt_cautions_set_goal_when_goal_exists() -> None:
    # With an active goal the will beat must NOT push a fresh set_goal; it
    # keeps the cautious framing that protects any already-computed steps.
    prompt = build_will_prompt(_rich_world_state(), "minecraft")
    assert "different* objective" in prompt
    assert "do NOT re-state it" in prompt
    # The no-goal push must be absent here.
    assert "this is the moment to choose one" not in prompt.lower()


def test_build_will_prompt_lists_scan_targets_and_requests_structured_target() -> None:
    # The anti-circling fix: the will beat must enumerate the *exact* block and
    # entity ids present on the live scan and imperatively ask Synth to name
    # one as a structural target (target_kind/target_name). This is what lets
    # the motor tick route straight to it instead of drifting in circles.
    prompt = build_will_prompt(_rich_world_state(), "minecraft")
    # Exact scan ids surfaced verbatim (blocks by name, entities by type).
    assert "oak_log" in prompt
    assert "stone" in prompt
    assert "player" in prompt
    assert "mob" in prompt
    # Imperative request for a structural target (kind + exact id).
    assert "target_kind='block'" in prompt
    assert "target_kind='entity'" in prompt
    assert "target_name" in prompt
    # Must insist the id is copied verbatim, never invented (no keyword logic).
    assert "verbatim" in prompt.lower()


def test_build_will_prompt_no_target_cue_when_scan_empty() -> None:
    # With nothing on the scan there is nothing to name → the target cue is
    # omitted (the destination guidance still stands).
    prompt = build_will_prompt({"extra": {}}, "minecraft")
    assert "target_kind='block'" not in prompt
    assert "target_kind='entity'" not in prompt


def test_build_will_prompt_surfaces_not_found_feedback() -> None:
    # When the motor tick recorded a 'not_found' outcome for the last named
    # target, the will beat tells Synth it wasn't here and to re-plan.
    prompt = build_will_prompt(
        {
            "extra": {
                "last_target_result": "not_found",
                "last_target_name": "diamond_ore",
            }
        },
        "minecraft",
    )
    assert "diamond_ore" in prompt
    assert "not here" in prompt.lower()


def test_build_will_prompt_surfaces_unreachable_feedback() -> None:
    prompt = build_will_prompt(
        {
            "extra": {
                "last_target_result": "unreachable",
                "last_target_name": "oak_log",
            }
        },
        "minecraft",
    )
    assert "oak_log" in prompt
    assert "could not get" in prompt.lower()


def test_build_will_prompt_no_feedback_line_on_arrived_or_missing() -> None:
    # 'arrived' and absent feedback add no heads-up line.
    arrived = build_will_prompt(
        {"extra": {"last_target_result": "arrived", "last_target_name": "oak_log"}},
        "minecraft",
    )
    assert "heads up" not in arrived.lower()
    none_prompt = build_will_prompt({"extra": {}}, "minecraft")
    assert "heads up" not in none_prompt.lower()


def test_build_will_prompt_surfaces_death_position_and_reconsider() -> None:
    # When the bridge recorded a numeric death position, the will beat tells
    # Synth exactly where it died and presses it to reconsider its approach
    # instead of resuming the same fatal goal (structural — coords + count).
    prompt = build_will_prompt(
        {
            "extra": {
                "last_death": {"x": -120, "y": 63, "z": 44, "count": 3, "at": 1},
            }
        },
        "minecraft",
    )
    lower = prompt.lower()
    assert "died" in lower
    assert "x=-120" in prompt
    assert "z=44" in prompt
    assert "death #3" in lower
    assert "reconsider" in lower


def test_build_will_prompt_no_death_cue_when_absent_or_malformed() -> None:
    # No death recorded → no death cue. A malformed entry is ignored too.
    none_prompt = build_will_prompt({"extra": {}}, "minecraft")
    assert "you have died here" not in none_prompt.lower()
    bad_prompt = build_will_prompt({"extra": {"last_death": {"count": 1}}}, "minecraft")
    assert "you have died here" not in bad_prompt.lower()


def test_build_decision_prompt_is_alias_of_will_prompt() -> None:
    state = _rich_world_state()
    assert build_decision_prompt(state, "minecraft") == build_will_prompt(
        state, "minecraft"
    )


def test_resolve_will_interval_prefers_will_key() -> None:
    # Explicit will key wins.
    def _cfg(key: str, default: Any) -> Any:
        return {"VESSEL_WILL_INTERVAL_SEC": 90}.get(key, default)

    assert resolve_will_interval(_cfg) == 90


def test_resolve_will_interval_falls_back_to_legacy_beat_key() -> None:
    # When the will key is unset, fall back to the legacy beat key.
    def _cfg(key: str, default: Any) -> Any:
        table = {"VESSEL_WILL_INTERVAL_SEC": None, "VESSEL_BEAT_INTERVAL_SEC": 120}
        return table.get(key, default)

    assert resolve_will_interval(_cfg) == 120


def test_resolve_will_interval_default_and_clamp() -> None:
    assert resolve_will_interval(lambda k, d: None, default=45) == 45
    assert resolve_will_interval(lambda k, d: 1) == 10
    assert resolve_will_interval(lambda k, d: 999999) == 3600


def test_resolve_motor_interval_default_and_clamp() -> None:
    assert resolve_motor_interval(lambda k, d: d, default=3) == 3
    # Clamped to the [1, 60] range — fast but never a busy-loop.
    assert resolve_motor_interval(lambda k, d: 0) == 1
    assert resolve_motor_interval(lambda k, d: 999) == 60
    assert resolve_motor_interval(lambda k, d: 5) == 5


def test_resolve_motor_interval_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("config down")

    assert resolve_motor_interval(_boom, default=3) == 3


def test_is_motor_enabled_defaults_true_and_reads_flag() -> None:
    # Default on when the key is absent.
    assert is_motor_enabled(lambda k, d: d) is True
    assert is_motor_enabled(lambda k, d: False) is False
    assert is_motor_enabled(lambda k, d: True) is True


def test_is_motor_enabled_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("config down")

    assert is_motor_enabled(_boom) is False


def test_resolve_will_quiet_sec_default_and_clamp() -> None:
    assert resolve_will_quiet_sec(lambda k, d: d, default=60) == 60
    # Clamped to [0, 3600]; 0 is allowed (disables the deferral).
    assert resolve_will_quiet_sec(lambda k, d: -5) == 0
    assert resolve_will_quiet_sec(lambda k, d: 0) == 0
    assert resolve_will_quiet_sec(lambda k, d: 999999) == 3600
    assert resolve_will_quiet_sec(lambda k, d: 30) == 30


def test_resolve_will_quiet_sec_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("config down")

    assert resolve_will_quiet_sec(_boom, default=60) == 60


# ---------------------------------------------------------------------------
# Action beat — concrete-doing cognition (middle layer)
# ---------------------------------------------------------------------------


def test_is_action_beat_enabled_defaults_true_and_reads_flag() -> None:
    # Missing → default True; explicit values honoured.
    assert is_action_beat_enabled(lambda k, d: d) is True
    assert is_action_beat_enabled(lambda k, d: False) is False
    assert is_action_beat_enabled(lambda k, d: True) is True


def test_is_action_beat_enabled_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("config down")

    assert is_action_beat_enabled(_boom) is False


def test_resolve_action_interval_default_and_clamp() -> None:
    assert resolve_action_interval(lambda k, d: d, default=20) == 20
    # Clamped to [3, 300].
    assert resolve_action_interval(lambda k, d: 0) == 3
    assert resolve_action_interval(lambda k, d: 999) == 300
    assert resolve_action_interval(lambda k, d: 15) == 15


def test_resolve_action_interval_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("config down")

    assert resolve_action_interval(_boom, default=20) == 20


def test_build_action_prompt_empty_when_no_goal() -> None:
    # No active goal → empty string so the caller skips enqueuing.
    assert build_action_prompt({"extra": {}}, "minecraft") == ""


def test_build_action_prompt_is_action_focused_and_lists_verbs() -> None:
    prompt = build_action_prompt(_rich_world_state(), "minecraft")
    assert prompt != ""
    # Pragmatic, in-game framing that names the world and curbs monologue prose.
    assert "IN GAME" in prompt
    assert "playing minecraft" in prompt
    assert "monologue" in prompt.lower()
    # World-namespaced verbs are surfaced so cognition uses the real actions.
    assert "vessel_minecraft_" in prompt
    # The current goal free text is surfaced verbatim.
    assert "explore the caves" in prompt


def test_build_action_prompt_surfaces_reachable_ids() -> None:
    prompt = build_action_prompt(_rich_world_state(), "minecraft")
    # Exact block/entity ids must appear verbatim so the LLM targets real names.
    assert "oak_log" in prompt


# ---------------------------------------------------------------------------
# Game-knowledge block — reference facts, rendered structurally when present
# ---------------------------------------------------------------------------


def _state_with_knowledge() -> dict[str, Any]:
    state = _rich_world_state()
    state["extra"]["knowledge"] = [
        {
            "title": "Mining tiers",
            "text": "Iron ore needs at least a stone pickaxe or it drops nothing.",
            "url": "https://example/w/Pickaxe",
        },
        {
            "title": "Wood bootstrap",
            "text": "Everything starts with wood: logs -> planks -> sticks.",
            "url": "https://example/w/Wood",
        },
    ]
    return state


def test_build_will_prompt_renders_knowledge_block() -> None:
    prompt = build_will_prompt(_state_with_knowledge(), "minecraft")
    # The header frames the facts as reference, never as a script.
    assert "Game knowledge" in prompt
    # Curated facts are surfaced verbatim as bullets with their titles.
    assert "Mining tiers:" in prompt
    assert "stone pickaxe" in prompt


def test_build_action_prompt_renders_knowledge_block() -> None:
    prompt = build_action_prompt(_state_with_knowledge(), "minecraft")
    assert "Game knowledge" in prompt
    assert "Wood bootstrap:" in prompt


def test_knowledge_block_absent_when_no_knowledge() -> None:
    # No extra["knowledge"] → the whole block is skipped, keeping the beat lean.
    prompt = build_will_prompt(_rich_world_state(), "minecraft")
    assert "Game knowledge" not in prompt


# ---------------------------------------------------------------------------
# craft-material shortfall cue
# ---------------------------------------------------------------------------


def _state_with_craft_deficit() -> dict[str, Any]:
    state = _rich_world_state()
    state["extra"]["craft_deficit"] = {
        "wanted": "crafting_table",
        "missing": [{"item": "oak_planks", "have": 1, "need": 4}],
    }
    return state


def test_build_will_prompt_renders_craft_deficit() -> None:
    prompt = build_will_prompt(_state_with_craft_deficit(), "minecraft")
    assert "wished to build 'crafting_table'" in prompt
    # The actual recipe ingredient + have/need counts are surfaced verbatim.
    assert "1/4 oak_planks" in prompt


def test_build_action_prompt_renders_craft_deficit() -> None:
    prompt = build_action_prompt(_state_with_craft_deficit(), "minecraft")
    assert "wished to build 'crafting_table'" in prompt
    assert "1/4 oak_planks" in prompt


def test_craft_deficit_multiple_ingredients() -> None:
    state = _rich_world_state()
    state["extra"]["craft_deficit"] = {
        "wanted": "furnace",
        "missing": [
            {"item": "cobblestone", "have": 3, "need": 8},
            {"item": "stick", "have": 0, "need": 2},
        ],
    }
    prompt = build_will_prompt(state, "minecraft")
    assert "3/8 cobblestone" in prompt
    assert "0/2 stick" in prompt


def test_craft_deficit_absent_or_malformed() -> None:
    # No cue → nothing rendered.
    assert "wished to build" not in build_will_prompt(_rich_world_state(), "minecraft")
    # Malformed shapes are skipped without raising.
    bad = _rich_world_state()
    bad["extra"]["craft_deficit"] = {"wanted": "", "missing": []}
    assert "wished to build" not in build_will_prompt(bad, "minecraft")
    bad2 = _rich_world_state()
    bad2["extra"]["craft_deficit"] = {"wanted": "chest", "missing": "nope"}
    assert "wished to build" not in build_will_prompt(bad2, "minecraft")


# --- _fmt_goal_deficit — active-goal material shortfall cue ------------------


def _state_with_goal_deficit() -> dict[str, Any]:
    state = _rich_world_state()
    state["extra"]["goal_deficit"] = {
        "items": [
            {"item": "oak_log", "have": 5, "need": 20},
            {"item": "torch", "have": 2, "need": 3},
        ]
    }
    return state


def test_build_will_prompt_renders_goal_deficit() -> None:
    prompt = build_will_prompt(_state_with_goal_deficit(), "minecraft")
    assert "current goal still needs materials" in prompt
    assert "5/20 oak_log" in prompt
    assert "2/3 torch" in prompt


def test_build_action_prompt_renders_goal_deficit() -> None:
    prompt = build_action_prompt(_state_with_goal_deficit(), "minecraft")
    assert "current goal still needs materials" in prompt
    assert "5/20 oak_log" in prompt


def test_goal_deficit_absent_or_malformed() -> None:
    # No cue → nothing rendered.
    assert "current goal still needs" not in build_will_prompt(
        _rich_world_state(), "minecraft"
    )
    bad = _rich_world_state()
    bad["extra"]["goal_deficit"] = {"items": []}
    assert "current goal still needs" not in build_will_prompt(bad, "minecraft")
    bad2 = _rich_world_state()
    bad2["extra"]["goal_deficit"] = {"items": "nope"}
    assert "current goal still needs" not in build_will_prompt(bad2, "minecraft")
    # A satisfied item (have >= need) is skipped.
    sat = _rich_world_state()
    sat["extra"]["goal_deficit"] = {
        "items": [{"item": "oak_log", "have": 20, "need": 20}]
    }
    assert "current goal still needs" not in build_will_prompt(sat, "minecraft")


# ---------------------------------------------------------------------------
# build_damage_appraisal_prompt — post-damage cognitive appraisal
# ---------------------------------------------------------------------------


def _hurt_world_state(**over: Any) -> dict[str, Any]:
    state = _rich_world_state()
    state["health"] = 12.0
    state["extra"]["damage_taken"] = 4.0
    state["extra"]["has_ranged_weapon"] = False
    state["extra"]["ranged_ammo"] = 0
    state["extra"]["best_melee_damage"] = 6.0
    state["extra"].update(over)
    return state


def test_appraisal_combat_framing_lists_attack_verb() -> None:
    prompt = build_damage_appraisal_prompt(_hurt_world_state(), "minecraft")
    # Pragmatic, in-game framing that names the world and curbs monologue prose.
    assert "IN GAME" in prompt
    assert "playing minecraft" in prompt
    # Combat framing when the source is not a player.
    assert "hostile creature hurt you" in prompt
    assert "vessel_minecraft_attack" in prompt
    # Damage magnitude surfaced verbatim.
    assert "about 4 damage" in prompt


def test_appraisal_combat_framing_offers_shoot_when_ranged_ready() -> None:
    prompt = build_damage_appraisal_prompt(
        _hurt_world_state(has_ranged_weapon=True, ranged_ammo=9), "minecraft"
    )
    assert "vessel_minecraft_shoot" in prompt
    assert "9 arrows" in prompt
    assert "Ranged weapon ready: yes" in prompt


def test_appraisal_combat_framing_no_shoot_when_unarmed_ranged() -> None:
    prompt = build_damage_appraisal_prompt(_hurt_world_state(), "minecraft")
    assert "no ranged option" in prompt
    assert "Ranged weapon ready: no" in prompt


def test_appraisal_social_framing_when_player_struck() -> None:
    prompt = build_damage_appraisal_prompt(
        _hurt_world_state(damage_from_player=True), "minecraft"
    )
    # Social framing: a person hit you — do not reflexively swing back.
    assert "person* struck you" in prompt
    assert "Do NOT reflexively swing back" in prompt
    assert "vessel_minecraft_say" in prompt


def test_appraisal_bare_hands_when_no_weapon() -> None:
    prompt = build_damage_appraisal_prompt(
        _hurt_world_state(best_melee_damage=0), "minecraft"
    )
    assert "bare hands" in prompt


def test_appraisal_some_damage_when_magnitude_unknown() -> None:
    # Non-numeric damage magnitude falls back to "some".
    prompt = build_damage_appraisal_prompt(
        _hurt_world_state(damage_taken=None), "minecraft"
    )
    assert "about some damage" in prompt


def test_appraisal_renders_knowledge_block() -> None:
    state = _hurt_world_state()
    state["extra"]["knowledge"] = [
        {
            "title": "Skeletons",
            "text": "Skeletons shoot arrows from range.",
            "url": "https://example/w/Skeleton",
        }
    ]
    prompt = build_damage_appraisal_prompt(state, "minecraft")
    assert "Game knowledge" in prompt
    assert "Skeletons:" in prompt


def test_knowledge_block_skipped_when_entries_have_no_text() -> None:
    state = _rich_world_state()
    state["extra"]["knowledge"] = [{"title": "empty", "text": ""}]
    prompt = build_will_prompt(state, "minecraft")
    # Header is dropped when nothing renderable survives.
    assert "Game knowledge" not in prompt


# ---------------------------------------------------------------------------
# Reflection pause — prompt + config helpers
# ---------------------------------------------------------------------------


def test_build_reflection_prompt_frames_intentional_pause() -> None:
    prompt = build_reflection_prompt(_rich_world_state(), "minecraft")
    # Pragmatic, in-game framing that names the world and curbs monologue prose.
    assert "IN GAME" in prompt
    assert "minecraft" in prompt
    assert "monologue" in prompt.lower()
    assert "You were drifting" in prompt
    assert "private" in prompt.lower()
    # It is a thinking turn — must forbid speaking.
    assert "Do NOT speak" in prompt


def test_build_reflection_prompt_pushes_set_goal_when_no_goal() -> None:
    state = _rich_world_state()
    state["extra"]["current_goal"] = None
    prompt = build_reflection_prompt(state, "minecraft")
    # Neutral, non-restricted framing: the no-goal branch still points at
    # set_goal but keeps the general reflection tone (no forced banner).
    assert "vessel_minecraft_set_goal" in prompt
    assert "no active goal" in prompt.lower()


def test_build_reflection_prompt_pushes_update_goal_when_goal_exists() -> None:
    # A goal exists → make it actionable via update_goal (no plan yet).
    prompt = build_reflection_prompt(_rich_world_state(), "minecraft")
    assert "vessel_minecraft_update_goal" in prompt


def test_build_reflection_prompt_handles_empty_world_state() -> None:
    # Fully guarded: an empty state still yields a usable prompt.
    prompt = build_reflection_prompt({}, "minecraft")
    assert "vessel_minecraft_set_goal" in prompt


def test_is_reflection_enabled_defaults_true_and_reads_flag() -> None:
    assert is_reflection_enabled(lambda k, d: d) is True
    assert is_reflection_enabled(lambda k, d: False) is False
    assert is_reflection_enabled(lambda k, d: True) is True


def test_is_reflection_enabled_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("boom")

    assert is_reflection_enabled(_boom) is True


def test_resolve_reflection_duration_default_and_clamp() -> None:
    assert resolve_reflection_duration(lambda k, d: d, default=15) == 15.0
    # Clamped to [3.0, 300.0].
    assert resolve_reflection_duration(lambda k, d: 0) == 3.0
    assert resolve_reflection_duration(lambda k, d: 9999) == 300.0
    assert resolve_reflection_duration(lambda k, d: 20) == 20.0


def test_resolve_reflection_duration_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("boom")

    assert resolve_reflection_duration(_boom, default=15) == 15.0


def test_resolve_reflection_min_interval_default_and_clamp() -> None:
    assert resolve_reflection_min_interval(lambda k, d: d, default=60) == 60.0
    # Clamped to [10.0, 3600.0].
    assert resolve_reflection_min_interval(lambda k, d: 0) == 10.0
    assert resolve_reflection_min_interval(lambda k, d: 99999) == 3600.0
    assert resolve_reflection_min_interval(lambda k, d: 120) == 120.0


def test_resolve_reflection_min_interval_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("boom")

    assert resolve_reflection_min_interval(_boom, default=60) == 60.0


# ---------------------------------------------------------------------------
# build_goal_prompt — dedicated single-purpose goal beat
# ---------------------------------------------------------------------------


def test_build_goal_prompt_frames_goal_only_turn() -> None:
    prompt = build_goal_prompt(_rich_world_state(), "minecraft")
    # In-game framing, names the world, and forbids speaking (private turn).
    assert "IN GAME" in prompt
    assert "minecraft" in prompt
    assert "Do NOT" in prompt or "private" in prompt.lower()


def test_build_goal_prompt_references_set_goal_when_no_goal() -> None:
    state = _rich_world_state()
    state["extra"]["current_goal"] = None
    prompt = build_goal_prompt(state, "minecraft")
    assert "vessel_minecraft_set_goal" in prompt


def test_build_goal_prompt_references_update_goal_when_goal_exists() -> None:
    prompt = build_goal_prompt(_rich_world_state(), "minecraft")
    assert "vessel_minecraft_update_goal" in prompt


def test_build_goal_prompt_is_world_agnostic() -> None:
    # Verb namespace derives from the world arg — never hardcoded to minecraft.
    state = _rich_world_state()
    state["extra"]["current_goal"] = None
    prompt = build_goal_prompt(state, "skyrim")
    assert "vessel_skyrim_set_goal" in prompt
    assert "vessel_minecraft_set_goal" not in prompt


def test_build_goal_prompt_handles_empty_world_state() -> None:
    # Fully guarded: an empty state still yields a usable prompt.
    prompt = build_goal_prompt({}, "minecraft")
    assert "vessel_minecraft_set_goal" in prompt


def test_is_goal_beat_enabled_defaults_true_and_reads_flag() -> None:
    assert is_goal_beat_enabled(lambda k, d: d) is True
    assert is_goal_beat_enabled(lambda k, d: False) is False
    assert is_goal_beat_enabled(lambda k, d: True) is True


def test_is_goal_beat_enabled_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("boom")

    assert is_goal_beat_enabled(_boom) is True


def test_resolve_goal_beat_interval_default_and_clamp() -> None:
    assert resolve_goal_beat_interval(lambda k, d: d, default=45) == 45
    # Clamped to [10, 3600].
    assert resolve_goal_beat_interval(lambda k, d: 0) == 10
    assert resolve_goal_beat_interval(lambda k, d: 99999) == 3600
    assert resolve_goal_beat_interval(lambda k, d: 120) == 120


def test_resolve_goal_beat_interval_failsafe_on_error() -> None:
    def _boom(key: str, default: Any) -> Any:
        raise RuntimeError("boom")

    assert resolve_goal_beat_interval(_boom, default=45) == 45


def test_fmt_items_renders_known_player_identity() -> None:
    """A connector-supplied identity label is appended next to the entity id."""
    from core.vessel_beat import _fmt_items

    items: list[dict[str, Any]] = [
        {"name": "remuraine", "kind": "player", "known_as": "Scar - your papa"},
        {"name": "sheep", "kind": "mob"},
    ]
    rendered = _fmt_items(items)
    assert "remuraine (Scar - your papa)" in rendered
    assert "sheep" in rendered


def test_fmt_items_renders_distance() -> None:
    """A numeric distance is shown so cognition does not act on blind targets."""
    from core.vessel_beat import _fmt_items

    items: list[dict[str, Any]] = [
        {"name": "sheep", "kind": "mob", "distance": 12.3},
        {
            "name": "remuraine",
            "kind": "player",
            "known_as": "Scar - your papa",
            "distance": 2.0,
        },
        {"name": "oak_log", "kind": "block", "distance": 4.6},
    ]
    rendered = _fmt_items(items)
    assert "sheep (12m)" in rendered
    assert "remuraine (Scar - your papa) (2m)" in rendered
    assert "oak_log (5m)" in rendered


def test_fmt_equipment_renders_held_item_or_bare_hands() -> None:
    """The equipment line surfaces what the body is holding — or that it is
    punching with bare hands — so cognition can reason to craft tools."""
    from core.vessel_beat import _fmt_equipment

    assert _fmt_equipment({"equipped_item": "wooden_axe"}) == (
        "- Equipment: wooden_axe"
    )
    assert _fmt_equipment({"equipped_item": "   "}) == (
        "- Equipment: none — you are using your bare hands"
    )
    assert _fmt_equipment({}) == "- Equipment: none — you are using your bare hands"
    assert _fmt_equipment({"equipped_item": None}) == (
        "- Equipment: none — you are using your bare hands"
    )
    # Bare-handed with enough planks: the structural tool-gap fact is appended.
    gap = _fmt_equipment(
        {
            "equipped_item": None,
            "inventory_counts": {"oak_planks": 68, "oak_log": 31, "dirt": 12},
        }
    )
    assert gap.startswith("- Equipment: none — you are using your bare hands —")
    assert "wooden axe" in gap
    # Too few planks (or none held) → no tool hint.
    assert "wooden axe" not in _fmt_equipment(
        {"equipped_item": None, "inventory_counts": {"oak_planks": 2}}
    )
    assert "wooden axe" not in _fmt_equipment(
        {"equipped_item": None, "inventory_counts": {"dirt": 12}}
    )
