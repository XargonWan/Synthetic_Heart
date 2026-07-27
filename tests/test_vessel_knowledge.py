"""Tests for the Rift Vessel curated knowledge base (Minecraft adapter).

The knowledge base is *reference material only* (AGENTS.md §5c — the spontaneity
rule): it never scripts what Synth does, it only tells cognition/the
goal-expansion Drone how the world works so sub-steps get ordered correctly
(gather the prerequisite tool before the ore that needs it).

These tests exercise the **pure**, offline pieces:

* ``get_knowledge_sources`` loads and caches the curated manifest;
* ``lookup_knowledge`` ranks entries by **structural tag overlap** (never
  keyword/regex on free text) and is fail-safe on odd input;
* the ``lookup_knowledge`` world verb is exposed with a keyword-free schema and
  declares **no** ``external_effects`` (Fast Lane only, constraint #1);
* the ``_act_lookup_knowledge`` handler returns a well-formed
  ``VesselActionResult`` and never raises into the chain.

No bridge, DB, network or LLM is touched — ``MinecraftConnector()`` is
instantiated bare and only the knowledge methods are called.
"""

from __future__ import annotations

import pytest

from plugins.rift_vessel.minecraft.minecraft import MinecraftConnector
from plugins.rift_vessel.vessel_base import VesselActionResult


# ---------------------------------------------------------------------------
# get_knowledge_sources — loads + caches the curated manifest
# ---------------------------------------------------------------------------


def test_get_knowledge_sources_loads_entries() -> None:
    connector = MinecraftConnector()
    sources = connector.get_knowledge_sources()
    assert isinstance(sources, list)
    assert sources, "curated knowledge.json should ship at least one entry"
    # Every entry is a dict with a text fact and structural tags.
    for entry in sources:
        assert isinstance(entry, dict)
        assert entry.get("text")
        assert isinstance(entry.get("tags", []), list)


def test_get_knowledge_sources_is_cached() -> None:
    connector = MinecraftConnector()
    first = connector.get_knowledge_sources()
    second = connector.get_knowledge_sources()
    # Same object returned — the manifest is read once and cached.
    assert first is second


# ---------------------------------------------------------------------------
# lookup_knowledge — structural tag-overlap ranking (keyword-free)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_knowledge_matches_ore_by_tag() -> None:
    connector = MinecraftConnector()
    # A structural game id, exactly what a goal's target_name would carry.
    results = await connector.lookup_knowledge("iron_ore")
    assert results, "iron_ore should match the mining-tiers entry via its tags"
    # The top hit must be a knowledge entry that tags iron_ore.
    top = results[0]
    assert "iron_ore" in {str(t).lower() for t in top.get("tags", [])}


@pytest.mark.asyncio
async def test_lookup_knowledge_ranks_higher_tag_overlap_first() -> None:
    connector = MinecraftConnector()
    # Two tokens that both live in the mining-tiers entry's tags.
    results = await connector.lookup_knowledge("iron_ore wooden_pickaxe")
    assert results
    # The entry with the most overlapping tags is ranked first.
    top_overlap = len(
        {"iron_ore", "wooden_pickaxe"}
        & {str(t).lower() for t in results[0].get("tags", [])}
    )
    for entry in results[1:]:
        overlap = len(
            {"iron_ore", "wooden_pickaxe"}
            & {str(t).lower() for t in entry.get("tags", [])}
        )
        assert top_overlap >= overlap


@pytest.mark.asyncio
async def test_lookup_knowledge_empty_query_returns_prefix() -> None:
    connector = MinecraftConnector()
    total = len(connector.get_knowledge_sources())
    results = await connector.lookup_knowledge("", limit=2)
    # An empty query is not an error — it returns the first N reference facts.
    assert len(results) == min(2, total)


@pytest.mark.asyncio
async def test_lookup_knowledge_unknown_token_returns_nothing() -> None:
    connector = MinecraftConnector()
    # A token that is not a tag or a title substring scores zero everywhere.
    results = await connector.lookup_knowledge("zzz_not_a_real_block")
    assert results == []


@pytest.mark.asyncio
async def test_lookup_knowledge_respects_limit() -> None:
    connector = MinecraftConnector()
    results = await connector.lookup_knowledge("pickaxe stone wood craft", limit=1)
    assert len(results) <= 1


@pytest.mark.asyncio
async def test_lookup_knowledge_bad_limit_is_fail_safe() -> None:
    connector = MinecraftConnector()
    # A garbage limit must not raise — it falls back to the default.
    results = await connector.lookup_knowledge("iron_ore", limit="nope")  # type: ignore[arg-type]
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Verb schema — exposed, keyword-free, Fast-Lane only (constraint #1)
# ---------------------------------------------------------------------------


def test_lookup_knowledge_verb_is_exposed_and_fast_lane() -> None:
    connector = MinecraftConnector()
    actions = connector.get_world_actions()
    assert "lookup_knowledge" in actions
    schema = actions["lookup_knowledge"]
    assert schema["required_fields"] == ["query"]
    assert "limit" in schema["optional_fields"]
    # Constraint #1: vessel verbs never declare external_effects (Fast Lane).
    assert "external_effects" not in schema


# ---------------------------------------------------------------------------
# _act_lookup_knowledge — handler returns a well-formed result, never raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_act_lookup_knowledge_returns_notes() -> None:
    connector = MinecraftConnector()
    result = await connector._act_lookup_knowledge({"query": "iron_ore"})
    assert isinstance(result, VesselActionResult)
    assert result.ok is True
    notes = result.data.get("notes")
    assert isinstance(notes, list)
    assert notes, "iron_ore should yield at least one note"
    # Each note is the compact {title, text, url} projection.
    for note in notes:
        assert set(note.keys()) == {"title", "text", "url"}


@pytest.mark.asyncio
async def test_act_lookup_knowledge_empty_payload_is_ok() -> None:
    connector = MinecraftConnector()
    # No query at all is not an error — it degrades to an ok empty-ish result.
    result = await connector._act_lookup_knowledge({})
    assert isinstance(result, VesselActionResult)
    assert result.ok is True
    assert result.data.get("query") == ""
