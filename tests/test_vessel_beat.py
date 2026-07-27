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
    build_action_prompt,
    build_decision_prompt,
    build_will_prompt,
    is_action_beat_enabled,
    is_autonomy_enabled,
    is_motor_enabled,
    resolve_action_interval,
    resolve_beat_interval,
    resolve_motor_interval,
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
    assert "minecraft world" in prompt
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
    assert "minecraft world" in prompt
    assert "vessel_minecraft_set_goal" in prompt
    assert "vessel_minecraft_update_goal" in prompt
    # …but frames the turn as *will, not motion* and tells Synth its body
    # will move on its own once the goal is clear (motorics is separate).
    lowered = prompt.lower()
    assert "will, not motion" in lowered
    assert "move toward what you need on its own" in lowered


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
    # World-namespaced verbs are surfaced so cognition uses the real actions.
    assert "vessel_minecraft_" in prompt
    # The current goal free text is surfaced verbatim.
    assert "explore the caves" in prompt


def test_build_action_prompt_surfaces_reachable_ids() -> None:
    prompt = build_action_prompt(_rich_world_state(), "minecraft")
    # Exact block/entity ids must appear verbatim so the LLM targets real names.
    assert "oak_log" in prompt
