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
