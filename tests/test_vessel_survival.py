"""Tests for the Minecraft Vessel self-preservation reflex.

The self-preservation guard is the *fast, reactive* danger layer that runs at
the top of every motor tick (AGENTS.md §5c) with **no LLM, no cognition turn,
no diary**. It swims to the surface, steps out of fire/lava, fights back or
flees a hostile mob, and respawns after death. These tests verify the purely
**structural** priority rules of ``MinecraftConnector._survival_threat`` and
the ``_nearest_hostile`` helper — numeric thresholds and canonical game ids
only, never keyword/text matching.

The ``build_will_prompt`` threat cue is also exercised here to confirm the slow
will beat surfaces a recently-handled danger structurally.
"""

from __future__ import annotations

from typing import Any

from core.vessel_beat import build_will_prompt
from plugins.rift_vessel.minecraft.minecraft import MinecraftConnector
from plugins.rift_vessel.vessel_base import WorldState


def _make_conn() -> MinecraftConnector:
    """Bare connector with the class-constant self-preservation defaults."""
    conn = MinecraftConnector()
    conn._sp_enabled = True
    conn._sp_low_oxygen = float(MinecraftConnector._LOW_OXYGEN)
    conn._sp_low_health = float(MinecraftConnector._LOW_HEALTH_FLEE)
    conn._sp_hostile_dist = float(MinecraftConnector._HOSTILE_NEAR_DIST)
    conn._sp_fight_back = True
    conn._sp_fight_max_fails = int(MinecraftConnector._FIGHT_MAX_FAILS)
    return conn


def _state(**extra: Any) -> WorldState:
    """Build a WorldState with a populated ``extra`` dict (health lives there)."""
    base: dict[str, Any] = {
        "is_alive": True,
        # At RUNTIME mineflayer ``bot.oxygenLevel`` reports the 0..20 air-bubble
        # scale (20 = full lungs, 0 = out of air) — NOT air ticks. A healthy body
        # reads ~20, so the drowning reflex must only fire near 0 (threshold 6).
        "oxygen": 20,
        "health": 20.0,
        "block_head": "air",
        "block_feet": "grass_block",
        "is_in_water": False,
        "entities": [],
    }
    base.update(extra)
    return WorldState(
        environment="minecraft",
        health=float(base.get("health") or 20.0),
        position={"x": 0.0, "y": 64.0, "z": 0.0},
        possible_actions=[],
        flags={"connected": True},
        extra=base,
    )


# ----------------------------------------------------------------------
# Priority order
# ----------------------------------------------------------------------


def test_healthy_body_has_no_threat() -> None:
    conn = _make_conn()
    assert conn._survival_threat(_state()) is None


def test_dead_takes_top_priority() -> None:
    conn = _make_conn()
    # Even with every other danger present, death wins.
    plan = conn._survival_threat(
        _state(
            is_alive=False,
            oxygen=0,
            block_head="water",
            block_feet="lava",
            entities=[{"name": "zombie", "hostile": True, "distance": 1.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "dead"
    assert plan["verb"] == "respawn"


def test_drowning_beats_burning_and_hostiles() -> None:
    conn = _make_conn()
    plan = conn._survival_threat(
        _state(
            block_head="water",
            is_in_water=True,
            oxygen=2,
            block_feet="lava",
            entities=[{"name": "zombie", "hostile": True, "distance": 1.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "drowning"
    assert plan["verb"] == "goto_surface"


def test_no_drowning_when_oxygen_high() -> None:
    conn = _make_conn()
    # Head underwater but full lungs (20 on the 0..20 bubble scale) → no threat.
    # A healthy submerged bot reads ~20 at runtime; an air-ticks threshold (e.g.
    # 200) would false-fire here since the value never approaches it.
    plan = conn._survival_threat(
        _state(block_head="water", is_in_water=True, oxygen=20)
    )
    assert plan is None


def test_no_drowning_while_wading_head_in_air() -> None:
    conn = _make_conn()
    # Body in water (feet wet) but head is in AIR — merely wading/swimming at
    # the surface, not drowning. The ``is_in_water`` flag must NOT trigger the
    # reflex on its own: only a liquid ``block_head`` counts as submerged.
    plan = conn._survival_threat(_state(block_head="air", is_in_water=True, oxygen=6))
    assert plan is None


def test_no_drowning_when_oxygen_unavailable() -> None:
    conn = _make_conn()
    # The bridge reports -1/None when oxygen is unavailable — that sentinel must
    # never be read as suffocation even with the head submerged.
    plan = conn._survival_threat(
        _state(block_head="water", is_in_water=True, oxygen=-1)
    )
    assert plan is None
    plan_none = conn._survival_threat(
        _state(block_head="water", is_in_water=True, oxygen=None)
    )
    assert plan_none is None


def test_burning_beats_hostiles() -> None:
    conn = _make_conn()
    plan = conn._survival_threat(
        _state(
            block_feet="lava",
            entities=[{"name": "zombie", "hostile": True, "distance": 1.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "burning"
    assert plan["verb"] == "flee"


def test_fire_at_head_also_burns() -> None:
    conn = _make_conn()
    plan = conn._survival_threat(_state(block_head="fire"))
    assert plan is not None
    assert plan["threat"] == "burning"


# ----------------------------------------------------------------------
# Defend vs flee
# ----------------------------------------------------------------------


def test_healthy_hostile_triggers_defend() -> None:
    conn = _make_conn()
    plan = conn._survival_threat(
        _state(
            health=20.0,
            entities=[{"name": "zombie", "hostile": True, "distance": 4.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "defend"
    assert plan["verb"] == "attack"
    assert plan["payload"].get("target") == "zombie"


def test_low_health_hostile_triggers_flee() -> None:
    conn = _make_conn()
    plan = conn._survival_threat(
        _state(
            health=4.0,
            entities=[{"name": "zombie", "hostile": True, "distance": 4.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "flee"
    assert plan["verb"] == "flee"


def test_fight_back_off_always_flees() -> None:
    conn = _make_conn()
    conn._sp_fight_back = False
    plan = conn._survival_threat(
        _state(
            health=20.0,
            entities=[{"name": "zombie", "hostile": True, "distance": 4.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "flee"


def test_repeated_fails_escalate_to_flee() -> None:
    conn = _make_conn()
    hostile_state = _state(
        health=20.0,
        entities=[{"name": "zombie", "hostile": True, "distance": 4.0}],
    )
    # First assessment: defend (also latches the fight target).
    first = conn._survival_threat(hostile_state)
    assert first is not None and first["threat"] == "defend"
    # Simulate the same mob resisting our attacks up to the max.
    conn._fight_fail_count = conn._sp_fight_max_fails
    escalated = conn._survival_threat(hostile_state)
    assert escalated is not None
    assert escalated["threat"] == "flee"


def test_distant_hostile_ignored() -> None:
    conn = _make_conn()
    plan = conn._survival_threat(
        _state(entities=[{"name": "zombie", "hostile": True, "distance": 40.0}])
    )
    assert plan is None


def test_non_hostile_entity_ignored() -> None:
    conn = _make_conn()
    plan = conn._survival_threat(
        _state(entities=[{"name": "cow", "hostile": False, "distance": 2.0}])
    )
    assert plan is None


def test_disabled_via_flag_but_threat_still_computed() -> None:
    # _survival_threat itself does not gate on _sp_enabled (the guard does);
    # the assessment must still classify the danger.
    conn = _make_conn()
    conn._sp_enabled = False
    plan = conn._survival_threat(_state(is_alive=False))
    assert plan is not None and plan["threat"] == "dead"


# ----------------------------------------------------------------------
# _nearest_hostile
# ----------------------------------------------------------------------


def test_nearest_hostile_picks_closest() -> None:
    state = _state(
        entities=[
            {"name": "zombie", "hostile": True, "distance": 9.0},
            {"name": "skeleton", "hostile": True, "distance": 3.0},
            {"name": "cow", "hostile": False, "distance": 1.0},
        ]
    )
    nearest = MinecraftConnector._nearest_hostile(state)
    assert nearest is not None
    assert nearest["name"] == "skeleton"


def test_nearest_hostile_none_when_all_peaceful() -> None:
    state = _state(entities=[{"name": "cow", "hostile": False, "distance": 1.0}])
    assert MinecraftConnector._nearest_hostile(state) is None


def test_nearest_hostile_falls_back_to_kind_mob() -> None:
    # Older bridge without the hostile flag: structural kind == "mob".
    state = _state(entities=[{"name": "zombie", "kind": "mob", "distance": 2.0}])
    nearest = MinecraftConnector._nearest_hostile(state)
    assert nearest is not None
    assert nearest["name"] == "zombie"


def test_nearest_hostile_ignores_bad_distance() -> None:
    state = _state(entities=[{"name": "zombie", "hostile": True, "distance": None}])
    assert MinecraftConnector._nearest_hostile(state) is None


# ----------------------------------------------------------------------
# Will-beat threat cue
# ----------------------------------------------------------------------


def _ws_with_threat(threat: Any) -> WorldState:
    return WorldState(
        environment="minecraft",
        health=20.0,
        position={"x": 0.0, "y": 64.0, "z": 0.0},
        possible_actions=[],
        flags={"connected": True},
        extra={"threat": threat},
    )


def test_will_prompt_surfaces_threat_cue() -> None:
    prompt = build_will_prompt(_ws_with_threat("drowning"), "minecraft")
    assert "Heads up" in prompt
    assert "air" in prompt.lower()


def test_will_prompt_no_cue_when_safe() -> None:
    prompt = build_will_prompt(_ws_with_threat(None), "minecraft")
    # The survival cue is absent when there is no recent threat.
    assert "instincts already reacted" not in prompt


def test_will_prompt_unknown_threat_still_noted() -> None:
    prompt = build_will_prompt(_ws_with_threat("meteor"), "minecraft")
    assert "instincts already reacted" in prompt
    assert "meteor" in prompt
