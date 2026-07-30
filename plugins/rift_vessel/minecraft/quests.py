# plugins/rift_vessel/minecraft/quests.py
"""Minecraft virtual-quest progression tech-tree (reference content).

Minecraft has no built-in quest system, so — to give Synth a *direction* toward
the natural end-game (defeating the Ender Dragon) — this module ships a
structural **tech-tree** describing the classic survival progression:

    wood → stone → iron → diamond → (bed / operational base) →
    Nether access → blaze rods + ender pearls → End portal (eyes of ender) →
    Ender Dragon → netherite (post-dragon deepening)

This is **content**, not a script (AGENTS.md §5c, the Scope rule + the
spontaneity rule):

* **Scope rule.** The *mechanism* — "what progression stage am I at, and what is
  a sensible next milestone?" — is world-agnostic and lives in the Rift Vessel
  core (``VesselConnectorBase.get_progression_stage``). The *content* here (the
  concrete Minecraft item ids and the ordered tech-tree) is Minecraft-specific
  and therefore lives in the adapter.

* **Spontaneity rule.** This tree is surfaced to cognition purely as
  *reference context* — "here is roughly where you are and what usually comes
  next" — exactly like the knowledge base. It is **never** an engine that
  auto-executes steps and it never dictates the goal. Synth still authors its
  own goal freely from its own will, and may skip, reorder, personalise or
  ignore the tree entirely. Two different Synths (or the same Synth on two
  different days) should still play differently.

Stage detection is **structural and numeric only** — it inspects the id→count
inventory map and the current dimension id (both plain bridge/game ids), never
free-text goal descriptions or chat, so it stays language-agnostic.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Structural id groups (game ids only — never matched against free text).
# ---------------------------------------------------------------------------
# Grouped by tier so detection can ask "does the inventory contain *any* id of
# this tier?" using plain set membership over game ids. Kept intentionally broad
# (all wood variants, all tool materials) so it works across Minecraft versions
# without keyword parsing.

_WOOD_LOG_IDS: frozenset[str] = frozenset(
    f"{kind}_log"
    for kind in (
        "oak",
        "birch",
        "spruce",
        "jungle",
        "acacia",
        "dark_oak",
        "mangrove",
        "cherry",
    )
) | frozenset(f"{kind}_stem" for kind in ("crimson", "warped"))

_PLANK_IDS: frozenset[str] = frozenset(
    f"{kind}_planks"
    for kind in (
        "oak",
        "birch",
        "spruce",
        "jungle",
        "acacia",
        "dark_oak",
        "mangrove",
        "cherry",
        "bamboo",
        "crimson",
        "warped",
    )
)

# Crafting stations / basics.
_CRAFTING_TABLE_IDS: frozenset[str] = frozenset({"crafting_table"})
_FURNACE_IDS: frozenset[str] = frozenset({"furnace", "blast_furnace", "smoker"})

# Tools by material tier. Membership is enough to say "she has reached this
# tool tier" — we do not care which specific tool.
_WOODEN_TOOL_IDS: frozenset[str] = frozenset(
    f"wooden_{t}" for t in ("pickaxe", "axe", "shovel", "sword", "hoe")
)
_STONE_TOOL_IDS: frozenset[str] = frozenset(
    f"stone_{t}" for t in ("pickaxe", "axe", "shovel", "sword", "hoe")
)
_IRON_TOOL_IDS: frozenset[str] = frozenset(
    f"iron_{t}" for t in ("pickaxe", "axe", "shovel", "sword", "hoe")
)
_DIAMOND_TOOL_IDS: frozenset[str] = frozenset(
    f"diamond_{t}" for t in ("pickaxe", "axe", "shovel", "sword", "hoe")
)
_NETHERITE_TOOL_IDS: frozenset[str] = frozenset(
    f"netherite_{t}" for t in ("pickaxe", "axe", "shovel", "sword", "hoe")
)

# Armour by material tier.
_IRON_ARMOR_IDS: frozenset[str] = frozenset(
    f"iron_{p}" for p in ("helmet", "chestplate", "leggings", "boots")
)
_DIAMOND_ARMOR_IDS: frozenset[str] = frozenset(
    f"diamond_{p}" for p in ("helmet", "chestplate", "leggings", "boots")
)
_NETHERITE_ARMOR_IDS: frozenset[str] = frozenset(
    f"netherite_{p}" for p in ("helmet", "chestplate", "leggings", "boots")
)

# Milestone resources / blocks.
_COBBLESTONE_IDS: frozenset[str] = frozenset({"cobblestone", "cobbled_deepslate"})
_IRON_INGOT_IDS: frozenset[str] = frozenset({"iron_ingot"})
_DIAMOND_IDS: frozenset[str] = frozenset({"diamond"})
_NETHERITE_INGOT_IDS: frozenset[str] = frozenset({"netherite_ingot"})
_BED_IDS: frozenset[str] = frozenset(
    f"{c}_bed"
    for c in (
        "white",
        "orange",
        "magenta",
        "light_blue",
        "yellow",
        "lime",
        "pink",
        "gray",
        "light_gray",
        "cyan",
        "purple",
        "blue",
        "brown",
        "green",
        "red",
        "black",
    )
)
_OBSIDIAN_IDS: frozenset[str] = frozenset({"obsidian"})
_FLINT_AND_STEEL_IDS: frozenset[str] = frozenset({"flint_and_steel"})
_BLAZE_ROD_IDS: frozenset[str] = frozenset({"blaze_rod"})
_BLAZE_POWDER_IDS: frozenset[str] = frozenset({"blaze_powder"})
_ENDER_PEARL_IDS: frozenset[str] = frozenset({"ender_pearl"})
_ENDER_EYE_IDS: frozenset[str] = frozenset({"ender_eye", "eye_of_ender"})

# Dimension ids reported by the bridge (mineflayer / minecraft-data).
_NETHER_DIMENSIONS: frozenset[str] = frozenset(
    {"the_nether", "nether", "minecraft:the_nether"}
)
_END_DIMENSIONS: frozenset[str] = frozenset({"the_end", "end", "minecraft:the_end"})


# ---------------------------------------------------------------------------
# Tech-tree stages (ordered, earliest → end-game).
# ---------------------------------------------------------------------------
# Each stage is a plain descriptor:
#   id        : stable stage identifier (structural)
#   title     : short human label for the reference block
#   reached   : callable(counts, dimension) -> bool — TRUE when the inventory /
#               dimension shows this stage has been *entered/completed*. Purely
#               structural (id membership + numeric counts + dimension id).
#   next      : one-line, plain-language description of the *typical* next
#               milestone, offered as reference — Synth is free to ignore it.
#   query     : structural KB-query seed tokens (game ids) for the next
#               milestone, so the starter-goal / will beat can fetch real
#               recipes for what usually comes next. Never a scripted objective.
#
# ``reached`` is evaluated from the LAST stage backward; the current stage is the
# highest one reached, and the next milestone is the stage immediately after it.


def _has_any(counts: dict[str, int], ids: frozenset[str]) -> bool:
    """True when the inventory holds at least one item of ``ids`` (count > 0)."""
    for item_id in ids:
        if int(counts.get(item_id, 0) or 0) > 0:
            return True
    return False


def _count_any(counts: dict[str, int], ids: frozenset[str]) -> int:
    """Total held across every id in ``ids`` (numeric, structural)."""
    total = 0
    for item_id in ids:
        total += int(counts.get(item_id, 0) or 0)
    return total


# A stage is (id, title, reached_fn, next_hint, query_tokens).
# reached_fn signature: (counts: dict[str, int], dimension: str) -> bool.
_STAGES: tuple[dict[str, Any], ...] = (
    {
        "id": "start",
        "title": "Just arrived — no tools yet",
        "reached": lambda c, d: True,  # everyone is at least here
        "next": (
            "Get your first wood: punch a tree for logs, craft a crafting "
            "table and planks, then wooden tools (a pickaxe first)."
        ),
        "query": ["oak_log", "crafting_table", "wooden_pickaxe", "planks"],
    },
    {
        "id": "wood",
        "title": "Wood age — logs / planks in hand",
        "reached": lambda c, d: _has_any(c, _WOOD_LOG_IDS) or _has_any(c, _PLANK_IDS),
        "next": (
            "Make a crafting table and wooden tools, then mine stone with a "
            "wooden pickaxe to reach the stone age."
        ),
        "query": ["crafting_table", "wooden_pickaxe", "cobblestone", "furnace"],
    },
    {
        "id": "stone",
        "title": "Stone age — cobblestone / stone tools",
        "reached": lambda c, d: (
            _has_any(c, _COBBLESTONE_IDS)
            or _has_any(c, _STONE_TOOL_IDS)
            or _has_any(c, _FURNACE_IDS)
        ),
        "next": (
            "Build a furnace, craft stone tools, then dig down to find and "
            "mine iron ore; smelt it into iron ingots."
        ),
        "query": ["furnace", "stone_pickaxe", "iron_ore", "iron_ingot"],
    },
    {
        "id": "iron",
        "title": "Iron age — iron ingots / iron gear",
        "reached": lambda c, d: (
            _has_any(c, _IRON_INGOT_IDS)
            or _has_any(c, _IRON_TOOL_IDS)
            or _has_any(c, _IRON_ARMOR_IDS)
        ),
        "next": (
            "Craft an iron pickaxe and a set of iron armour, secure a bed and "
            "an operational base, then mine deep for diamonds."
        ),
        "query": ["iron_pickaxe", "iron_chestplate", "bed", "diamond"],
    },
    {
        "id": "diamond",
        "title": "Diamond age — diamonds / diamond gear",
        "reached": lambda c, d: (
            _has_any(c, _DIAMOND_IDS)
            or _has_any(c, _DIAMOND_TOOL_IDS)
            or _has_any(c, _DIAMOND_ARMOR_IDS)
        ),
        "next": (
            "Craft a diamond pickaxe (needed for obsidian) and diamond armour, "
            "gather obsidian and flint-and-steel, and build a Nether portal."
        ),
        "query": [
            "diamond_pickaxe",
            "diamond_chestplate",
            "obsidian",
            "flint_and_steel",
            "nether_portal",
        ],
    },
    {
        "id": "nether",
        "title": "Nether entry — obsidian / portal / in the Nether",
        "reached": lambda c, d: (
            d in _NETHER_DIMENSIONS
            or (_has_any(c, _OBSIDIAN_IDS) and _has_any(c, _FLINT_AND_STEEL_IDS))
        ),
        "next": (
            "In the Nether, find a fortress, kill blazes for blaze rods, and "
            "trade with or hunt for ender pearls."
        ),
        "query": [
            "nether_fortress",
            "blaze_rod",
            "blaze_powder",
            "ender_pearl",
        ],
    },
    {
        "id": "blaze_rods",
        "title": "Blaze rods — fortress loot secured",
        "reached": lambda c, d: (
            _has_any(c, _BLAZE_ROD_IDS) or _has_any(c, _BLAZE_POWDER_IDS)
        ),
        "next": (
            "Combine blaze powder with ender pearls to craft eyes of ender, "
            "then use them to locate the stronghold and its End portal."
        ),
        "query": ["blaze_powder", "ender_pearl", "ender_eye", "stronghold"],
    },
    {
        "id": "ender_eyes",
        "title": "Eyes of ender — ready to find the stronghold",
        "reached": lambda c, d: (
            _has_any(c, _ENDER_EYE_IDS)
            or (
                _has_any(c, _ENDER_PEARL_IDS)
                and (_has_any(c, _BLAZE_ROD_IDS) or _has_any(c, _BLAZE_POWDER_IDS))
            )
        ),
        "next": (
            "Throw eyes of ender to find the stronghold, fill the End portal "
            "frame, and step through to the End — bring full gear, food and "
            "blocks to pillar."
        ),
        "query": ["end_portal", "stronghold", "the_end", "ender_dragon"],
    },
    {
        "id": "end",
        "title": "The End — face the Ender Dragon",
        "reached": lambda c, d: d in _END_DIMENSIONS,
        "next": (
            "Destroy the end crystals on the obsidian pillars, then attack the "
            "Ender Dragon's head when it perches — this is the end-game boss."
        ),
        "query": ["ender_dragon", "end_crystal", "dragon_egg"],
    },
    {
        "id": "netherite",
        "title": "End-game deepening — netherite",
        "reached": lambda c, d: (
            _has_any(c, _NETHERITE_INGOT_IDS)
            or _has_any(c, _NETHERITE_TOOL_IDS)
            or _has_any(c, _NETHERITE_ARMOR_IDS)
        ),
        "next": (
            "Mine ancient debris in the Nether, smelt netherite scrap, and "
            "upgrade your diamond gear to netherite — the strongest set."
        ),
        "query": [
            "ancient_debris",
            "netherite_scrap",
            "netherite_ingot",
            "smithing_table",
        ],
    },
)


# The end-game framing, always appended as reference so Synth keeps the ultimate
# arc in mind without being told to rush there.
_ENDGAME_HINT = (
    "The natural end-game of survival Minecraft is defeating the Ender Dragon "
    "in the End. It is a long journey through the tiers above — a distant "
    "dream, not something to attempt before you are equipped."
)


def _normalise_dimension(dimension: Any) -> str:
    """Lower-cased dimension id, or '' when unknown. Structural only."""
    if not dimension:
        return ""
    try:
        return str(dimension).strip().lower()
    except Exception:  # pragma: no cover - defensive
        return ""


def detect_stage(
    inventory_counts: dict[str, int] | None,
    dimension: Any = None,
) -> dict[str, Any]:
    """Return the current progression stage + the typical next milestone.

    Purely **structural**: it reads the id→count inventory map and the current
    dimension id (plain game ids), never free text. The current stage is the
    highest tech-tree stage whose ``reached`` predicate is satisfied; the next
    milestone is the stage immediately after it (or the end-game deepening once
    everything is reached).

    Returns a dict::

        {
            "stage_id": str,
            "stage_title": str,
            "next_id": str | None,
            "next_hint": str,      # plain-language next milestone (reference)
            "query": list[str],    # structural KB-query seed for the next step
            "endgame": str,        # the Ender-Dragon arc framing
        }

    Fail-safe: any error degrades to the ``start`` stage.
    """
    counts: dict[str, int] = {}
    try:
        if isinstance(inventory_counts, dict):
            counts = inventory_counts
    except Exception:  # pragma: no cover - defensive
        counts = {}
    dim = _normalise_dimension(dimension)

    current_index = 0
    try:
        for idx, stage in enumerate(_STAGES):
            reached_fn = stage.get("reached")
            if callable(reached_fn) and reached_fn(counts, dim):
                current_index = idx
    except Exception:  # pragma: no cover - defensive
        current_index = 0

    current = _STAGES[current_index]
    # The next milestone is this stage's own ``next`` hint / query (it describes
    # what to do to LEAVE the current stage), which is what we want to surface.
    next_index = current_index + 1 if current_index + 1 < len(_STAGES) else None
    next_id = _STAGES[next_index]["id"] if next_index is not None else None

    return {
        "stage_id": str(current.get("id", "start")),
        "stage_title": str(current.get("title", "")),
        "next_id": next_id,
        "next_hint": str(current.get("next", "")),
        "query": list(current.get("query", []) or []),
        "endgame": _ENDGAME_HINT,
    }


def stage_reference_facts(stage: dict[str, Any] | None) -> list[dict[str, str]]:
    """Render a stage into ``knowledge``-style reference entries.

    Returns a list of ``{"title", "text"}`` dicts shaped exactly like the
    knowledge-base entries the connector places in ``extra["knowledge"]`` — so
    the will/action beats render them through the same "reference, not a script"
    block, keeping the spontaneity framing. Empty when ``stage`` is falsy.
    """
    if not isinstance(stage, dict) or not stage:
        return []
    facts: list[dict[str, str]] = []
    stage_title = str(stage.get("stage_title") or "").strip()
    next_hint = str(stage.get("next_hint") or "").strip()
    endgame = str(stage.get("endgame") or "").strip()
    if stage_title:
        facts.append(
            {
                "title": "Where you are (progression)",
                "text": stage_title,
            }
        )
    if next_hint:
        facts.append(
            {
                "title": "A typical next milestone (only if you want it)",
                "text": next_hint,
            }
        )
    if endgame:
        facts.append(
            {
                "title": "The far horizon",
                "text": endgame,
            }
        )
    return facts


def progression_query_tokens(stage: dict[str, Any] | None) -> list[str]:
    """Structural KB-query seed tokens for the stage's next milestone.

    These are game ids (e.g. ``iron_pickaxe``, ``bed``) describing what usually
    comes next, so the starter-goal / will beat can fetch real recipes for the
    next tier from the knowledge base. Never a scripted objective — Synth still
    chooses. Empty when ``stage`` is falsy.
    """
    if not isinstance(stage, dict) or not stage:
        return []
    query = stage.get("query")
    if not isinstance(query, list):
        return []
    return [str(tok).lower() for tok in query if tok]
