"""Tests for the Rift Vessel game knowledge base (Minecraft adapter).

The knowledge base is *reference material only* (AGENTS.md §5c — the spontaneity
rule): it never scripts what Synth does, it only tells cognition/the
goal-expansion Drone how the world works so sub-steps get ordered correctly
(gather the prerequisite tool before the ore that needs it).

The KB is now backed by the **live minecraft.wiki** via
:mod:`plugins.rift_vessel.minecraft.wiki_client`, with an incremental on-disk
cache and a one-time LLM summary per page. These tests exercise the pieces
**without any network or LLM**:

* :func:`wiki_client.lookup` — cache hit, offline fallback, and the
  ``cache_only`` (will-beat) path that must never fetch or summarise;
* ``MinecraftConnector.lookup_knowledge`` delegates to the client and stays
  fail-safe on odd input;
* the ``lookup_knowledge`` world verb is exposed with a keyword-free schema and
  declares **no** ``external_effects`` (Fast Lane only, constraint #1);
* the ``_act_lookup_knowledge`` handler returns a well-formed
  ``VesselActionResult`` and never raises into the chain.

``wiki_client._CACHE_DIR`` is redirected to a ``tmp_path`` and the live API
functions are monkeypatched, so nothing here touches disk state, the network,
or the Cortex engine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.rift_vessel.minecraft import wiki_client
from plugins.rift_vessel.minecraft.minecraft import MinecraftConnector
from plugins.rift_vessel.vessel_base import VesselActionResult


# ---------------------------------------------------------------------------
# Fixtures — isolate the on-disk cache and block the network/LLM
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the wiki cache to a temp dir so tests never touch real cache."""
    d = tmp_path / "wiki" / "cache"
    monkeypatch.setattr(wiki_client, "_CACHE_DIR", d)
    return d


def _seed_cache(cache_dir: Path, title: str, summary: str) -> None:
    """Write a cached page record the way ``_write_cache`` would."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    slug = wiki_client._slug(title)
    (cache_dir / f"{slug}.json").write_text(
        json.dumps(
            {
                "title": title,
                "url": wiki_client._PAGE_URL + title.replace(" ", "_"),
                "raw_extract": "raw " + summary,
                "summary": summary,
                "fetched_at": 0.0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# wiki_client.lookup — cache hit (no network/LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_serves_cached_summary_without_fetch(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cache(cache_dir, "Iron Ore", "Iron ore needs a stone pickaxe.")

    async def _boom_fetch(*_a: object, **_k: object) -> str:  # pragma: no cover
        raise AssertionError("fetch_page_plaintext must not be called on a cache hit")

    async def _boom_summary(*_a: object, **_k: object) -> str:  # pragma: no cover
        raise AssertionError("_summarize must not be called on a cache hit")

    # Search resolves to the cached title; fetch/summarise must not run.
    async def _search(_q: str, limit: int = 3) -> list[str]:
        return ["Iron Ore"]

    monkeypatch.setattr(wiki_client, "search_wiki", _search)
    monkeypatch.setattr(wiki_client, "fetch_page_plaintext", _boom_fetch)
    monkeypatch.setattr(wiki_client, "_summarize", _boom_summary)

    notes = await wiki_client.lookup("iron_ore", limit=3)
    assert notes == [
        {
            "title": "Iron Ore",
            "text": "Iron ore needs a stone pickaxe.",
            "url": wiki_client._PAGE_URL + "Iron_Ore",
        }
    ]


# ---------------------------------------------------------------------------
# wiki_client.lookup — miss => fetch + summarise + cache (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_fetches_summarises_and_caches_on_miss(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _search(_q: str, limit: int = 3) -> list[str]:
        return ["Diamond"]

    async def _fetch(_title: str, max_chars: int = 6000) -> str:
        return "Diamond ore requires an iron pickaxe."

    async def _summary(_title: str, _extract: str) -> str:
        return "Mine diamond ore with an iron pickaxe or better."

    monkeypatch.setattr(wiki_client, "search_wiki", _search)
    monkeypatch.setattr(wiki_client, "fetch_page_plaintext", _fetch)
    monkeypatch.setattr(wiki_client, "_summarize", _summary)

    notes = await wiki_client.lookup("diamond_ore", limit=1)
    assert notes == [
        {
            "title": "Diamond",
            "text": "Mine diamond ore with an iron pickaxe or better.",
            "url": wiki_client._PAGE_URL + "Diamond",
        }
    ]
    # The summary was cached to disk for next time.
    cached = json.loads((cache_dir / "diamond.json").read_text(encoding="utf-8"))
    assert cached["summary"] == "Mine diamond ore with an iron pickaxe or better."


# ---------------------------------------------------------------------------
# wiki_client.lookup — offline fallback to cached pages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_offline_falls_back_to_cache(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cache(cache_dir, "Stone Pickaxe", "Crafted from cobblestone and sticks.")

    # Simulate offline: search returns nothing.
    async def _empty_search(_q: str, limit: int = 3) -> list[str]:
        return []

    async def _boom_fetch(*_a: object, **_k: object) -> str:  # pragma: no cover
        raise AssertionError("no fetch when falling back to cache")

    monkeypatch.setattr(wiki_client, "search_wiki", _empty_search)
    monkeypatch.setattr(wiki_client, "fetch_page_plaintext", _boom_fetch)

    notes = await wiki_client.lookup("stone_pickaxe", limit=3)
    assert len(notes) == 1
    assert notes[0]["title"] == "Stone Pickaxe"
    assert notes[0]["text"] == "Crafted from cobblestone and sticks."


# ---------------------------------------------------------------------------
# wiki_client.lookup — cache_only never touches network/LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_cache_only_serves_cache_and_never_fetches(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cache(cache_dir, "Wood", "Punch trees to get logs; craft planks.")

    async def _boom_search(*_a: object, **_k: object) -> list[str]:  # pragma: no cover
        raise AssertionError("cache_only must not search the wiki")

    async def _boom_fetch(*_a: object, **_k: object) -> str:  # pragma: no cover
        raise AssertionError("cache_only must not fetch pages")

    monkeypatch.setattr(wiki_client, "search_wiki", _boom_search)
    monkeypatch.setattr(wiki_client, "fetch_page_plaintext", _boom_fetch)

    notes = await wiki_client.lookup("wood", limit=3, cache_only=True)
    assert len(notes) == 1
    assert notes[0]["title"] == "Wood"


@pytest.mark.asyncio
async def test_lookup_cache_only_empty_cache_returns_nothing(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom_search(*_a: object, **_k: object) -> list[str]:  # pragma: no cover
        raise AssertionError("cache_only must not search the wiki")

    monkeypatch.setattr(wiki_client, "search_wiki", _boom_search)

    notes = await wiki_client.lookup("anything", limit=3, cache_only=True)
    assert notes == []


@pytest.mark.asyncio
async def test_lookup_empty_query_returns_nothing(cache_dir: Path) -> None:
    assert await wiki_client.lookup("", limit=3) == []


# ---------------------------------------------------------------------------
# MinecraftConnector.lookup_knowledge — delegates + fail-safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connector_lookup_knowledge_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _lookup(query: str, limit: int = 3, *, cache_only: bool = False):
        captured["query"] = query
        captured["limit"] = limit
        captured["cache_only"] = cache_only
        return [{"title": "T", "text": "fact", "url": "u"}]

    monkeypatch.setattr(wiki_client, "lookup", _lookup)

    connector = MinecraftConnector()
    results = await connector.lookup_knowledge("iron_ore", limit=3)
    assert results == [{"title": "T", "text": "fact", "url": "u"}]
    assert captured == {"query": "iron_ore", "limit": 3, "cache_only": False}


@pytest.mark.asyncio
async def test_connector_lookup_knowledge_passes_cache_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _lookup(query: str, limit: int = 3, *, cache_only: bool = False):
        captured["cache_only"] = cache_only
        return []

    monkeypatch.setattr(wiki_client, "lookup", _lookup)

    connector = MinecraftConnector()
    await connector.lookup_knowledge("iron_ore", limit=5, cache_only=True)
    assert captured["cache_only"] is True


@pytest.mark.asyncio
async def test_connector_lookup_knowledge_bad_limit_is_fail_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _lookup(query: str, limit: int = 3, *, cache_only: bool = False):
        assert isinstance(limit, int)
        return []

    monkeypatch.setattr(wiki_client, "lookup", _lookup)

    connector = MinecraftConnector()
    # A garbage limit must not raise — it falls back to the default.
    results = await connector.lookup_knowledge("iron_ore", limit="nope")  # type: ignore[arg-type]
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_connector_lookup_knowledge_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*_a: object, **_k: object):
        raise RuntimeError("boom")

    monkeypatch.setattr(wiki_client, "lookup", _boom)

    connector = MinecraftConnector()
    # A client failure degrades to an empty list, never propagates.
    assert await connector.lookup_knowledge("iron_ore") == []


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
    assert schema["security_level"] == "low"
    # Constraint #1: vessel verbs never declare external_effects (Fast Lane).
    assert "external_effects" not in schema


def test_climb_verbs_are_exposed_and_fast_lane() -> None:
    """dig_staircase / return_surface are Fast-Lane world verbs (constraint #1).

    They let Synth go underground while keeping a walkable way back up — a
    Minecraft-specific ability mineflayer does not provide — so they live on the
    connector, not the vessel core, and never declare ``external_effects``.
    """
    connector = MinecraftConnector()
    actions = connector.get_world_actions()

    for verb, optional in (
        ("dig_staircase", {"depth", "yaw"}),
        ("return_surface", {"height", "target_y", "item"}),
    ):
        assert verb in actions, f"{verb} should be an exposed world verb"
        schema = actions[verb]
        # Both are self-directed: nothing is strictly required.
        assert schema["required_fields"] == []
        assert optional.issubset(set(schema["optional_fields"]))
        assert schema["security_level"] == "low"
        # Constraint #1: vessel verbs never declare external_effects (Fast Lane).
        assert "external_effects" not in schema


# ---------------------------------------------------------------------------
# _act_lookup_knowledge — handler returns a well-formed result, never raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_act_lookup_knowledge_returns_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _lookup(query: str, limit: int = 3, *, cache_only: bool = False):
        return [{"title": "Iron Ore", "text": "needs a stone pickaxe", "url": "u"}]

    monkeypatch.setattr(wiki_client, "lookup", _lookup)

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
async def test_act_lookup_knowledge_empty_payload_is_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _lookup(query: str, limit: int = 3, *, cache_only: bool = False):
        return []

    monkeypatch.setattr(wiki_client, "lookup", _lookup)

    connector = MinecraftConnector()
    # No query at all is not an error — it degrades to an ok empty-ish result.
    result = await connector._act_lookup_knowledge({})
    assert isinstance(result, VesselActionResult)
    assert result.ok is True
    assert result.data.get("query") == ""
