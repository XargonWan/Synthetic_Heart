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
    conn._sp_use_ranged = True
    conn._sp_ranged_min_dist = float(MinecraftConnector._RANGED_MIN_DIST)
    conn._sp_appraisal_enabled = True
    conn._sp_engage_ratio = float(MinecraftConnector._ENGAGE_RATIO)
    conn._sp_weak_mob_power = float(MinecraftConnector._WEAK_MOB_POWER)
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


def _armed(**extra: Any) -> WorldState:
    """A well-equipped body: iron sword + full-ish armor.

    Its structural power comfortably clears the engage ratio against an
    ordinary mob, so the defend/ranged/melee-selection tests exercise the
    fight branch instead of tripping the new power-aware flee. Callers may
    still override ``best_melee_damage``/``armor_points``/``health``.
    """
    base: dict[str, Any] = {"best_melee_damage": 7.0, "armor_points": 15.0}
    base.update(extra)
    return _state(**base)


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
    # A well-armed, healthy body swings at an ordinary mob (power ratio clears
    # the engage threshold). Reachability is handled by the bridge ``attack``.
    plan = conn._survival_threat(
        _armed(
            health=20.0,
            entities=[{"name": "zombie", "hostile": True, "distance": 2.0}],
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
    # Armed body so the reflex defends before escalating on the fail cap.
    hostile_state = _armed(
        health=20.0,
        entities=[{"name": "zombie", "hostile": True, "distance": 2.0}],
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


# ----------------------------------------------------------------------
# Fight-all-aggressive: _aggressive_targets
# ----------------------------------------------------------------------


def test_aggressive_targets_returns_all_in_range_nearest_first() -> None:
    conn = _make_conn()
    state = _state(
        entities=[
            {"name": "zombie", "hostile": True, "distance": 5.0},
            {"name": "skeleton", "hostile": True, "distance": 2.0},
            {"name": "spider", "hostile": True, "distance": 7.0},
        ]
    )
    targets = conn._aggressive_targets(state, conn._sp_hostile_dist)
    # All three are within the default HOSTILE_NEAR_DIST (8), nearest first.
    assert [t["name"] for t in targets] == ["skeleton", "zombie", "spider"]


def test_aggressive_targets_includes_ranged_attacker_out_of_range() -> None:
    conn = _make_conn()
    # A skeleton shooting from far away (beyond near_dist) but actively
    # targeting the bot must still be engaged.
    state = _state(
        entities=[
            {
                "name": "skeleton",
                "hostile": True,
                "distance": 25.0,
                "is_targeting_me": True,
            }
        ]
    )
    targets = conn._aggressive_targets(state, conn._sp_hostile_dist)
    assert len(targets) == 1
    assert targets[0]["name"] == "skeleton"


def test_aggressive_targets_never_includes_player() -> None:
    conn = _make_conn()
    state = _state(
        entities=[
            {
                "name": "Steve",
                "kind": "player",
                "hostile": True,
                "distance": 2.0,
                "is_targeting_me": True,
            }
        ]
    )
    # A human hitting Synth is a social matter, never a reflex melee target.
    assert conn._aggressive_targets(state, conn._sp_hostile_dist) == []


def test_multi_mob_fight_picks_nearest_target() -> None:
    conn = _make_conn()
    plan = conn._survival_threat(
        _armed(
            health=20.0,
            entities=[
                {"name": "zombie", "hostile": True, "distance": 6.0},
                {"name": "skeleton", "hostile": True, "distance": 1.5},
            ],
        )
    )
    assert plan is not None
    assert plan["threat"] == "defend"
    # Nearest of the aggressive set is engaged first.
    assert plan["payload"].get("target") == "skeleton"
    assert plan["reason"].get("targets") == 2


# ----------------------------------------------------------------------
# Ranged vs melee selection
# ----------------------------------------------------------------------


def test_ranged_weapon_used_at_distance() -> None:
    conn = _make_conn()
    plan = conn._survival_threat(
        _armed(
            health=20.0,
            has_ranged_weapon=True,
            ranged_ammo=12,
            entities=[{"name": "skeleton", "hostile": True, "distance": 6.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "defend"
    assert plan["verb"] == "shoot"
    assert plan["reason"].get("ranged") is True


def test_pure_archer_clears_power_gate_and_shoots() -> None:
    # A body carrying ONLY a bow (no melee weapon) must still be judged capable
    # of fighting a mob at range: the usable ranged weapon contributes to the
    # own-power estimate so the power gate passes and the reflex reaches the
    # shoot branch instead of fleeing. Regression for the archer-always-flees
    # gap (own-power previously counted melee damage only).
    conn = _make_conn()
    plan = conn._survival_threat(
        _state(
            health=20.0,
            best_melee_damage=0.0,
            armor_points=10.0,
            has_ranged_weapon=True,
            ranged_ammo=32,
            # No per-mob registry stats (this deployment's minecraft-data does
            # not expose them) → the mob falls back to _DEFAULT_MOB_POWER (12.0).
            entities=[{"name": "skeleton", "hostile": True, "distance": 7.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "defend"
    assert plan["verb"] == "shoot"
    assert plan["reason"].get("ranged") is True
    assert plan["reason"].get("own_power", 0.0) >= plan["reason"].get(
        "mob_power", 999.0
    )


def test_ranged_weapon_not_used_up_close() -> None:
    conn = _make_conn()
    # Within RANGED_MIN_DIST (5.0) melee is preferred even with a bow.
    plan = conn._survival_threat(
        _armed(
            health=20.0,
            has_ranged_weapon=True,
            ranged_ammo=12,
            entities=[{"name": "zombie", "hostile": True, "distance": 2.0}],
        )
    )
    assert plan is not None
    assert plan["verb"] == "attack"


def test_no_ranged_weapon_reachable_mob_melees_not_flees() -> None:
    conn = _make_conn()
    # A melee mob at 6 blocks with NO bow/ammo. The bridge ``attack`` verb
    # walks the body up to reach before swinging, so a mob inside the hostile
    # radius is reachable ON FOOT — the reflex must APPROACH AND ATTACK, not
    # flee just because it is momentarily beyond arm's length. (The old
    # premature ``unreachable -> flee`` branch made her flee every ordinary
    # melee mob and never finish a fight.)
    plan = conn._survival_threat(
        _armed(
            health=20.0,
            has_ranged_weapon=False,
            ranged_ammo=0,
            entities=[{"name": "skeleton", "hostile": True, "distance": 6.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "defend"
    assert plan["verb"] == "attack"


def test_no_ranged_weapon_close_attacker_melees() -> None:
    conn = _make_conn()
    # Same armed body, mob within arm's length — swing at it.
    plan = conn._survival_threat(
        _armed(
            health=20.0,
            has_ranged_weapon=False,
            ranged_ammo=0,
            entities=[{"name": "zombie", "hostile": True, "distance": 2.0}],
        )
    )
    assert plan is not None
    assert plan["verb"] == "attack"


def test_low_health_flees_disarmed_regardless_of_distance() -> None:
    conn = _make_conn()
    # Health is the PRIMARY disengage driver: when the body is actually losing
    # (health at/below the flee threshold) it flees even if it cannot answer a
    # ranged attacker — this is what stops the passive death loop, NOT a raw
    # distance cutoff.
    plan = conn._survival_threat(
        _state(
            health=4.0,
            has_ranged_weapon=False,
            ranged_ammo=0,
            entities=[{"name": "skeleton", "hostile": True, "distance": 6.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "flee"
    assert plan["verb"] == "flee"


# ----------------------------------------------------------------------
# Health-primary escalation (FIGHT_MAX_FAILS = 8)
# ----------------------------------------------------------------------


def test_fight_max_fails_default_is_eight() -> None:
    assert MinecraftConnector._FIGHT_MAX_FAILS == 8


def test_low_health_escalates_before_fail_cap() -> None:
    conn = _make_conn()
    # Health is the PRIMARY escalation driver: below the flee threshold the
    # body flees immediately, without waiting for the fail counter.
    state = _state(
        health=4.0,
        entities=[{"name": "zombie", "hostile": True, "distance": 4.0}],
    )
    plan = conn._survival_threat(state)
    assert plan is not None
    assert plan["threat"] == "flee"
    assert conn._fight_fail_count == 0


def test_target_change_resets_fail_counter() -> None:
    conn = _make_conn()
    # Fight zombie, accrue fails (armed so it engages).
    zombie_state = _armed(
        health=20.0,
        entities=[{"name": "zombie", "hostile": True, "distance": 4.0}],
    )
    conn._survival_threat(zombie_state)
    conn._fight_fail_count = 5
    # A different mob becomes the nearest target → counter resets.
    skeleton_state = _armed(
        health=20.0,
        entities=[{"name": "skeleton", "hostile": True, "distance": 3.0}],
    )
    plan = conn._survival_threat(skeleton_state)
    assert plan is not None
    assert plan["threat"] == "defend"
    assert conn._fight_fail_count == 0


# ----------------------------------------------------------------------
# Power-aware fight/flee (VESSEL_SP_ENGAGE_RATIO / VESSEL_SP_WEAK_MOB_POWER)
# ----------------------------------------------------------------------


def test_engage_ratio_default_is_one() -> None:
    assert MinecraftConnector._ENGAGE_RATIO == 1.0


def test_weak_mob_power_default_is_six() -> None:
    assert MinecraftConnector._WEAK_MOB_POWER == 6.0


def test_disarmed_flees_ordinary_mob() -> None:
    conn = _make_conn()
    # Bare-handed, no armor: an ordinary mob (default moderate power) is not
    # worth trading blows — the body flees instead of dying by attrition.
    plan = conn._survival_threat(
        _state(
            health=20.0,
            entities=[{"name": "zombie", "hostile": True, "distance": 2.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "flee"
    assert plan["reason"].get("disarmed") is True
    assert plan["reason"].get("weak_mob") is False


def test_disarmed_fights_weak_mob() -> None:
    conn = _make_conn()
    # A trivial creature (low structural power, below the weak-mob floor) can
    # be punched out even bare-handed.
    plan = conn._survival_threat(
        _state(
            health=20.0,
            entities=[
                {
                    "name": "silverfish",
                    "hostile": True,
                    "distance": 2.0,
                    "max_health": 3.0,
                    "attack_damage": 1.0,
                }
            ],
        )
    )
    assert plan is not None
    assert plan["threat"] == "defend"
    assert plan["verb"] == "attack"
    assert plan["reason"].get("mob_power", 999.0) < conn._sp_weak_mob_power


def test_armed_high_power_engages_ordinary_mob() -> None:
    conn = _make_conn()
    # Iron sword + full armor + full health clears the engage ratio against an
    # ordinary mob.
    plan = conn._survival_threat(
        _armed(
            health=20.0,
            armor_points=20.0,
            best_melee_damage=8.0,
            entities=[
                {
                    "name": "zombie",
                    "hostile": True,
                    "distance": 2.0,
                    "max_health": 13.0,
                    "attack_damage": 3.0,
                }
            ],
        )
    )
    assert plan is not None
    assert plan["threat"] == "defend"
    assert plan["reason"].get("ratio", 0.0) >= conn._sp_engage_ratio


def test_armed_low_power_flees_strong_mob() -> None:
    conn = _make_conn()
    # A weak weapon and no armor against a powerful mob loses the ratio → flee.
    plan = conn._survival_threat(
        _state(
            health=20.0,
            best_melee_damage=2.0,
            armor_points=0.0,
            entities=[
                {
                    "name": "ravager",
                    "hostile": True,
                    "distance": 2.0,
                    "max_health": 100.0,
                    "attack_damage": 12.0,
                }
            ],
        )
    )
    assert plan is not None
    assert plan["threat"] == "flee"
    assert plan["reason"].get("ratio", 999.0) < conn._sp_engage_ratio


def test_armor_flips_flee_to_engage() -> None:
    conn = _make_conn()
    mob = {
        "name": "zombie",
        "hostile": True,
        "distance": 2.0,
        "max_health": 10.0,
        "attack_damage": 2.0,
    }
    # Same sword, no armor → outmatched → flee.
    bare = conn._survival_threat(
        _state(health=20.0, best_melee_damage=6.0, armor_points=0.0, entities=[mob])
    )
    assert bare is not None
    assert bare["threat"] == "flee"
    # Full armor tips the survivability term over the engage ratio → defend.
    conn2 = _make_conn()
    armored = conn2._survival_threat(
        _state(health=20.0, best_melee_damage=6.0, armor_points=20.0, entities=[mob])
    )
    assert armored is not None
    assert armored["threat"] == "defend"


# ----------------------------------------------------------------------
# Per-mob strategy override (§17) — creeper / enderman
# ----------------------------------------------------------------------


def test_resolver_returns_none_for_generic_mob() -> None:
    from plugins.rift_vessel.vessel_combat_strategy import resolve_combat_strategy

    assert resolve_combat_strategy("minecraft", "zombie") is None


def test_resolver_returns_strategy_for_special_mobs() -> None:
    from plugins.rift_vessel.vessel_combat_strategy import resolve_combat_strategy

    assert resolve_combat_strategy("minecraft", "creeper") is not None
    assert resolve_combat_strategy("minecraft", "enderman") is not None


def test_creeper_keeps_distance_override() -> None:
    conn = _make_conn()
    # A creeper must NEVER be chased into melee — the strategy override forces
    # keep_distance regardless of the body's power.
    plan = conn._survival_threat(
        _armed(
            health=20.0,
            entities=[{"name": "creeper", "hostile": True, "distance": 4.0}],
        )
    )
    assert plan is not None
    assert plan["verb"] == "keep_distance"
    assert plan["reason"].get("strategy") == "creeper_no_chase"


def test_enderman_keeps_distance_override() -> None:
    conn = _make_conn()
    plan = conn._survival_threat(
        _armed(
            health=20.0,
            entities=[{"name": "enderman", "hostile": True, "distance": 4.0}],
        )
    )
    assert plan is not None
    assert plan["verb"] == "keep_distance"
    assert plan["reason"].get("strategy") == "enderman_disengage"


def test_generic_hostile_falls_through_to_power_ratio() -> None:
    conn = _make_conn()
    # A mob with no registered strategy falls through to the power-ratio gate;
    # armed + healthy → defend.
    plan = conn._survival_threat(
        _armed(
            health=20.0,
            entities=[{"name": "spider", "hostile": True, "distance": 2.0}],
        )
    )
    assert plan is not None
    assert plan["threat"] == "defend"
