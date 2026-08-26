"""Tests for the Minecraft virtual-quest progression tech-tree.

Minecraft has no in-game quest system, so ``plugins/rift_vessel/minecraft/quests.py``
ships a **reference** tech-tree that lets Synth know roughly which progression
stage it is at (wood → stone → iron → diamond → Nether → blaze rods → eyes of
ender → the End → netherite) and what a typical next milestone would be. It is
surfaced purely as reference context (AGENTS.md §5c, the spontaneity rule) — it
never scripts a goal.

Stage detection is **structural and numeric only**: it inspects the id→count
inventory map and the current dimension id (plain game ids), never free text.
These tests exercise that purely offline, with no bridge / DB / LLM.
"""

from __future__ import annotations

from plugins.rift_vessel.minecraft import quests


# ----------------------------------------------------------------------
# detect_stage — structural stage detection over inventory + dimension
# ----------------------------------------------------------------------


def test_detect_stage_empty_inventory_is_start() -> None:
    stage = quests.detect_stage({}, None)
    assert stage["stage_id"] == "start"
    assert stage["next_id"] == "wood"
    assert stage["next_hint"]
    assert stage["endgame"]
    assert isinstance(stage["query"], list) and stage["query"]


def test_detect_stage_none_inventory_is_start() -> None:
    stage = quests.detect_stage(None, None)
    assert stage["stage_id"] == "start"


def test_detect_stage_wood_from_logs() -> None:
    stage = quests.detect_stage({"oak_log": 3}, "overworld")
    assert stage["stage_id"] == "wood"
    assert stage["next_id"] == "stone"


def test_detect_stage_wood_from_planks() -> None:
    stage = quests.detect_stage({"birch_planks": 8}, "overworld")
    assert stage["stage_id"] == "wood"


def test_detect_stage_stone_from_cobblestone() -> None:
    stage = quests.detect_stage({"cobblestone": 20}, "overworld")
    assert stage["stage_id"] == "stone"
    assert stage["next_id"] == "iron"


def test_detect_stage_stone_from_furnace() -> None:
    stage = quests.detect_stage({"furnace": 1}, "overworld")
    assert stage["stage_id"] == "stone"


def test_detect_stage_iron_from_ingot() -> None:
    stage = quests.detect_stage({"iron_ingot": 4}, "overworld")
    assert stage["stage_id"] == "iron"
    assert stage["next_id"] == "diamond"


def test_detect_stage_iron_from_armor() -> None:
    stage = quests.detect_stage({"iron_chestplate": 1}, "overworld")
    assert stage["stage_id"] == "iron"


def test_detect_stage_diamond_from_diamond() -> None:
    stage = quests.detect_stage({"diamond": 2}, "overworld")
    assert stage["stage_id"] == "diamond"
    assert stage["next_id"] == "nether"


def test_detect_stage_nether_from_dimension() -> None:
    stage = quests.detect_stage({}, "the_nether")
    assert stage["stage_id"] == "nether"


def test_detect_stage_nether_from_obsidian_and_flint() -> None:
    stage = quests.detect_stage({"obsidian": 10, "flint_and_steel": 1}, "overworld")
    assert stage["stage_id"] == "nether"


def test_detect_stage_blaze_rods() -> None:
    stage = quests.detect_stage({"blaze_rod": 3}, "the_nether")
    assert stage["stage_id"] == "blaze_rods"


def test_detect_stage_ender_eyes_from_eye() -> None:
    stage = quests.detect_stage({"ender_eye": 5}, "overworld")
    assert stage["stage_id"] == "ender_eyes"


def test_detect_stage_ender_eyes_from_pearls_and_rods() -> None:
    stage = quests.detect_stage({"ender_pearl": 3, "blaze_rod": 2}, "overworld")
    assert stage["stage_id"] == "ender_eyes"


def test_detect_stage_end_from_dimension() -> None:
    stage = quests.detect_stage({}, "the_end")
    assert stage["stage_id"] == "end"


def test_detect_stage_netherite_is_highest() -> None:
    # Netherite is the last stage; even with a full kit it stays netherite.
    stage = quests.detect_stage(
        {"netherite_ingot": 1, "diamond": 9, "iron_ingot": 30}, "overworld"
    )
    assert stage["stage_id"] == "netherite"
    assert stage["next_id"] is None


def test_detect_stage_prefers_highest_reached() -> None:
    # Holding wood, stone AND iron → the highest (iron) wins.
    stage = quests.detect_stage(
        {"oak_log": 2, "cobblestone": 10, "iron_ingot": 1}, "overworld"
    )
    assert stage["stage_id"] == "iron"


def test_detect_stage_minecraft_prefixed_dimension() -> None:
    stage = quests.detect_stage({}, "minecraft:the_end")
    assert stage["stage_id"] == "end"


# ----------------------------------------------------------------------
# stage_reference_facts — knowledge-style {title, text} rendering
# ----------------------------------------------------------------------


def test_stage_reference_facts_returns_three_entries() -> None:
    stage = quests.detect_stage({"oak_log": 1}, "overworld")
    facts = quests.stage_reference_facts(stage)
    assert len(facts) == 3
    titles = [f["title"] for f in facts]
    assert "Where you are (progression)" in titles
    assert "A typical next milestone (only if you want it)" in titles
    assert "The far horizon" in titles
    for f in facts:
        assert isinstance(f["title"], str) and f["title"]
        assert isinstance(f["text"], str) and f["text"]


def test_stage_reference_facts_empty_on_falsy() -> None:
    assert quests.stage_reference_facts(None) == []
    assert quests.stage_reference_facts({}) == []


# ----------------------------------------------------------------------
# progression_query_tokens — structural next-milestone KB seed
# ----------------------------------------------------------------------


def test_progression_query_tokens_from_stage() -> None:
    stage = quests.detect_stage({}, None)  # start stage
    tokens = quests.progression_query_tokens(stage)
    assert tokens  # non-empty
    assert all(isinstance(t, str) and t == t.lower() for t in tokens)


def test_progression_query_tokens_empty_on_falsy() -> None:
    assert quests.progression_query_tokens(None) == []
    assert quests.progression_query_tokens({}) == []


# ----------------------------------------------------------------------
# MinecraftConnector.get_progression_stage — override over cached telemetry
# ----------------------------------------------------------------------


def test_connector_progression_stage_reads_cached_telemetry() -> None:
    from plugins.rift_vessel.minecraft.minecraft import MinecraftConnector

    conn = MinecraftConnector()
    conn._last_inventory_counts = {"iron_ingot": 2}
    conn._last_dimension = "overworld"
    stage = conn.get_progression_stage()
    assert stage is not None
    assert stage["stage_id"] == "iron"


def test_connector_progression_stage_dimension_aware() -> None:
    from plugins.rift_vessel.minecraft.minecraft import MinecraftConnector

    conn = MinecraftConnector()
    conn._last_inventory_counts = {}
    conn._last_dimension = "the_end"
    stage = conn.get_progression_stage()
    assert stage is not None
    assert stage["stage_id"] == "end"


def test_connector_progression_stage_default_start() -> None:
    from plugins.rift_vessel.minecraft.minecraft import MinecraftConnector

    conn = MinecraftConnector()
    # Fresh connector: empty inventory, no dimension → start stage.
    stage = conn.get_progression_stage()
    assert stage is not None
    assert stage["stage_id"] == "start"


def test_base_progression_stage_default_is_none() -> None:
    from plugins.rift_vessel.vessel_base import (
        VesselActionResult,
        VesselConnectorBase,
    )

    class _Bare(VesselConnectorBase):
        async def connect(self, settings, on_event):  # type: ignore[override]
            return True

        async def disconnect(self):  # type: ignore[override]
            return None

        async def get_world_state(self):  # type: ignore[override]
            return None

        async def act(self, action, payload):  # type: ignore[override]
            return VesselActionResult(ok=True)

    assert _Bare().get_progression_stage() is None


# ======================================================================
# Core quest store (plugins/rift_vessel/vessel_quests.py)
# ----------------------------------------------------------------------
# The core store is Rift Vessel scope: a scope-aware ordered milestone
# tracker + per-mob kill counter, surfaced as a reference direction (never a
# script). These cover the PURE, DB-free helpers and the objective judge.
# ======================================================================

from plugins.rift_vessel.vessel_quests import (  # noqa: E402
    OBJ_HAS_BASE,
    OBJ_HAS_BED,
    OBJ_HAVE_ITEM,
    OBJ_KILL,
    OBJ_REACH_DIMENSION,
    STATUS_LOCKED,
)
from plugins.rift_vessel.vessel_quests import SCOPE_NONE as _Q_SCOPE_NONE  # noqa: E402
from plugins.rift_vessel.vessel_quests import _clip as _q_clip  # noqa: E402
from plugins.rift_vessel.vessel_quests import (  # noqa: E402
    _coerce_int as _q_coerce_int,
)
from plugins.rift_vessel.vessel_quests import (  # noqa: E402
    _coerce_objective as _q_coerce_objective,
)
from plugins.rift_vessel.vessel_quests import (  # noqa: E402
    _coerce_objectives as _q_coerce_objectives,
)
from plugins.rift_vessel.vessel_quests import (  # noqa: E402
    _coerce_scope as _q_coerce_scope,
)
from plugins.rift_vessel.vessel_quests import (  # noqa: E402
    _row_to_quest as _q_row_to_quest,
)
from plugins.rift_vessel.vessel_quests import (  # noqa: E402
    evaluate_quest_objectives,
)


def test_store_clip_trims_and_caps() -> None:
    assert _q_clip("  hi  ", 10) == "hi"
    assert _q_clip(None, 10) == ""
    assert _q_clip("x" * 50, 8) == "x" * 8


def test_store_coerce_scope_lowercases_and_defaults() -> None:
    assert _q_coerce_scope("Minecraft") == "minecraft"
    assert _q_coerce_scope("  ") == _Q_SCOPE_NONE
    assert _q_coerce_scope(None) == _Q_SCOPE_NONE
    assert len(_q_coerce_scope("a" * 200)) == 64


def test_store_coerce_int_fail_safe() -> None:
    assert _q_coerce_int("3") == 3
    assert _q_coerce_int(None, default=1) == 1
    assert _q_coerce_int("nope", default=5) == 5
    assert _q_coerce_int(-4, default=1) == 1


def test_store_coerce_objective_normalises_valid_kinds() -> None:
    assert _q_coerce_objective(
        {"kind": "have_item", "target": "Iron_Pickaxe", "count": 1}
    ) == {"kind": "have_item", "target": "iron_pickaxe", "count": 1}
    assert _q_coerce_objective({"kind": "has_base"}) == {
        "kind": "has_base",
        "target": None,
        "count": 1,
    }


def test_store_coerce_objective_rejects_unknown_kind() -> None:
    assert _q_coerce_objective({"kind": "eat_cookie"}) is None
    assert _q_coerce_objective("nope") is None
    assert _q_coerce_objective({}) is None


def test_store_coerce_objectives_caps_and_filters() -> None:
    raw = [{"kind": "kill", "target": "zombie"}, "bad", {"kind": "unknown"}]
    assert _q_coerce_objectives(raw) == [
        {"kind": "kill", "target": "zombie", "count": 1}
    ]
    assert _q_coerce_objectives("nope") == []
    many = [{"kind": "kill", "target": f"m{i}"} for i in range(50)]
    assert len(_q_coerce_objectives(many)) == 16


def test_store_row_to_quest_tuple_row() -> None:
    row = (
        7,
        "vessel",
        "minecraft",
        "none",
        "first_base",
        "Establish your first base",
        "desc",
        0,
        STATUS_LOCKED,
        '[{"kind": "has_base"}]',
        "{}",
    )
    q = _q_row_to_quest(row)
    assert q is not None
    assert q["quest_id"] == "first_base"
    assert q["objectives"] == [{"kind": "has_base", "target": None, "count": 1}]
    assert q["progress"] == {}


def test_store_row_to_quest_none_row() -> None:
    assert _q_row_to_quest(None) is None


def test_evaluate_empty_objectives_is_complete() -> None:
    assert evaluate_quest_objectives({"objectives": []})["complete"] is True
    assert evaluate_quest_objectives(None)["complete"] is False


def test_evaluate_have_item() -> None:
    quest = {
        "objectives": [{"kind": OBJ_HAVE_ITEM, "target": "iron_pickaxe", "count": 1}]
    }
    assert evaluate_quest_objectives(quest, {"iron_pickaxe": 1})["complete"] is True
    result = evaluate_quest_objectives(quest, {"iron_pickaxe": 0})
    assert result["complete"] is False
    assert result["pending"]


def test_evaluate_have_item_count_threshold() -> None:
    quest = {"objectives": [{"kind": OBJ_HAVE_ITEM, "target": "blaze_rod", "count": 6}]}
    assert evaluate_quest_objectives(quest, {"blaze_rod": 5})["complete"] is False
    assert evaluate_quest_objectives(quest, {"blaze_rod": 6})["complete"] is True


def test_evaluate_reach_dimension() -> None:
    quest = {"objectives": [{"kind": OBJ_REACH_DIMENSION, "target": "the_end"}]}
    assert evaluate_quest_objectives(quest, {}, dimension="the_end")["complete"] is True
    assert (
        evaluate_quest_objectives(quest, {}, dimension="overworld")["complete"] is False
    )


def test_evaluate_has_base_and_has_bed() -> None:
    base_q = {"objectives": [{"kind": OBJ_HAS_BASE}]}
    assert evaluate_quest_objectives(base_q, {}, has_base=True)["complete"] is True
    assert evaluate_quest_objectives(base_q, {}, has_base=False)["complete"] is False

    bed_q = {"objectives": [{"kind": OBJ_HAS_BED}]}
    assert evaluate_quest_objectives(bed_q, {}, has_bed=True)["complete"] is True
    assert evaluate_quest_objectives(bed_q, {"bed": 1})["complete"] is True
    assert evaluate_quest_objectives(bed_q, {})["complete"] is False


def test_evaluate_kill_by_target() -> None:
    quest = {
        "objectives": [{"kind": OBJ_KILL, "target": "ender_dragon", "count": 1}],
        "progress": {"kills": {"ender_dragon": 1}},
    }
    assert evaluate_quest_objectives(quest, {})["complete"] is True
    quest["progress"] = {"kills": {"zombie": 3}}
    assert evaluate_quest_objectives(quest, {})["complete"] is False


def test_evaluate_kill_any_hostile_sums() -> None:
    quest = {
        "objectives": [{"kind": OBJ_KILL, "count": 3}],
        "progress": {"kills": {"zombie": 2, "skeleton": 1}},
    }
    assert evaluate_quest_objectives(quest, {})["complete"] is True
    quest["progress"] = {"kills": {"zombie": 1}}
    assert evaluate_quest_objectives(quest, {})["complete"] is False


def test_evaluate_multiple_objectives_all_required() -> None:
    quest = {
        "objectives": [
            {"kind": OBJ_HAS_BASE},
            {"kind": OBJ_HAVE_ITEM, "target": "iron_pickaxe", "count": 1},
        ]
    }
    result = evaluate_quest_objectives(quest, {"iron_pickaxe": 1}, has_base=False)
    assert result["complete"] is False
    assert len(result["satisfied"]) == 1
    assert len(result["pending"]) == 1
    assert (
        evaluate_quest_objectives(quest, {"iron_pickaxe": 1}, has_base=True)["complete"]
        is True
    )
