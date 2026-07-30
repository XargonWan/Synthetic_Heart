# plugins/rift_vessel/minecraft/target_names.py
"""Derive a structural ``{target_kind, target_name}`` from a free-text goal.

This is the **one, explicitly authorized exception** to the project-wide
keyword-free rule (AGENTS.md "No keyword-based implementations"). The
autonomous will beat authors goals as free text (e.g. *"gather some oak logs"*
or *"vai a raccogliere legna di quercia"*) and the weaker vessel-scope model
(harmonyai/qwen) routinely omits the ``target_kind`` / ``target_name`` fields
the ``set_goal`` prompt asks for. Without those, the motor reflex has no target
and only directional-wanders — it never mines the log right next to it.

The user granted an explicit, scoped exception: *"ok puoi rompere la regola per
i nomi degli oggetti di minecraft"* — i.e. matching **Minecraft item/block/mob
names** in the goal text is allowed. The exception is confined to this module.

It is *not* free-form keyword intent detection: the vocabulary is a curated set
of **canonical Minecraft ids** (which the model writes in English regardless of
the conversation language, because those are the actual game ids) plus a small
list of common non-English aliases for the most frequent materials, each
mapping to the exact game id. Nothing here decides *what to do* — it only names
*which thing* an already-authored goal is about, so the reflex can head for it.

Resolution is purely additive/fail-safe: it is only consulted when cognition
did *not* supply the fields, and any miss simply returns ``None`` (leaving the
goal targetless, i.e. the previous behaviour).
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

# --- Canonical block ids Synth commonly names as gather/mine targets. ---------
# Value is the exact Minecraft block id the bridge resolves structurally.
_BLOCK_IDS: Tuple[str, ...] = (
    # wood / logs
    "oak_log",
    "spruce_log",
    "birch_log",
    "jungle_log",
    "acacia_log",
    "dark_oak_log",
    "mangrove_log",
    "cherry_log",
    # stone family
    "stone",
    "cobblestone",
    "deepslate",
    "cobbled_deepslate",
    "andesite",
    "diorite",
    "granite",
    "tuff",
    "gravel",
    "dirt",
    "grass_block",
    "sand",
    "sandstone",
    "clay",
    "obsidian",
    "netherrack",
    # ores
    "coal_ore",
    "iron_ore",
    "copper_ore",
    "gold_ore",
    "redstone_ore",
    "lapis_ore",
    "diamond_ore",
    "emerald_ore",
    "deepslate_coal_ore",
    "deepslate_iron_ore",
    "deepslate_copper_ore",
    "deepslate_gold_ore",
    "deepslate_redstone_ore",
    "deepslate_lapis_ore",
    "deepslate_diamond_ore",
    "deepslate_emerald_ore",
    "nether_gold_ore",
    "nether_quartz_ore",
    "ancient_debris",
)
# NOTE: crafting/utility blocks (``crafting_table``, ``furnace``, ``chest``,
# ``oak_planks``) are deliberately NOT derivable targets. They are *crafted*
# blocks, never natural ``mine`` objectives, and Synth routinely names them as
# instrumental means inside a goal's free text ("then craft a crafting table").
# Because ``_ORDERED_IDS`` matches by descending id length, ``crafting_table``
# (14 chars) used to win over the real objective ``acacia_log`` (10 chars),
# giving the motor reflex a phantom target that does not exist in the world —
# it would ``goto`` it forever and never mine. Excluding them here makes
# ``derive_target`` pick the actual gather/mine objective instead.

# --- Canonical mob/entity ids Synth commonly names (hunt / follow / interact). -
_ENTITY_IDS: Tuple[str, ...] = (
    "cow",
    "pig",
    "sheep",
    "chicken",
    "rabbit",
    "horse",
    "wolf",
    "cat",
    "villager",
    "zombie",
    "skeleton",
    "creeper",
    "spider",
    "enderman",
    "slime",
)

# --- Common non-English aliases → canonical game id. --------------------------
# Deliberately small: only the frequently-used materials. Keys are matched as
# whole words (with the word-boundary matcher below). This is the *only* place
# that maps human language to a game id; everything else relies on the model
# writing the canonical English id.
_ALIASES: Dict[str, str] = {
    # Italian (project's primary human language)
    "legno": "oak_log",
    "legna": "oak_log",
    "tronco": "oak_log",
    "tronchi": "oak_log",
    "quercia": "oak_log",
    "pietra": "stone",
    "sasso": "stone",
    "roccia": "stone",
    "carbone": "coal_ore",
    "ferro": "iron_ore",
    "oro": "gold_ore",
    "diamante": "diamond_ore",
    "diamanti": "diamond_ore",
    "rame": "copper_ore",
    "terra": "dirt",
    "sabbia": "sand",
    "mucca": "cow",
    "maiale": "pig",
    "pecora": "sheep",
    "pollo": "chicken",
    # English shorthands not equal to the id
    "wood": "oak_log",
    "log": "oak_log",
    "logs": "oak_log",
    "coal": "coal_ore",
    "iron": "iron_ore",
    "gold": "gold_ore",
    "diamond": "diamond_ore",
    "diamonds": "diamond_ore",
    "copper": "copper_ore",
    "redstone": "redstone_ore",
    "emerald": "emerald_ore",
}

_BLOCK_SET = frozenset(_BLOCK_IDS)
_ENTITY_SET = frozenset(_ENTITY_IDS)

# Longest ids first so "deepslate_iron_ore" wins over "iron_ore", and multi-word
# forms match before their fragments.
_ORDERED_IDS: List[str] = sorted(
    (*_BLOCK_IDS, *_ENTITY_IDS), key=lambda game_id: len(game_id), reverse=True
)


def _kind_for_id(game_id: str) -> Optional[str]:
    if game_id in _BLOCK_SET:
        return "block"
    if game_id in _ENTITY_SET:
        return "entity"
    return None


def _tokens(text: str) -> List[str]:
    """Lowercase word tokens (letters/digits/underscore runs)."""
    return re.findall(r"[a-z0-9_]+", text.lower())


def _match_canonical_id(text_lower: str) -> Optional[str]:
    """Return the first canonical id that appears as a whole word/phrase."""
    for game_id in _ORDERED_IDS:
        # id may contain underscores (oak_log); match it delimited by
        # non-word chars OR spaces where the underscore reads as a space.
        spaced = game_id.replace("_", " ")
        for needle in (game_id, spaced):
            pattern = r"(?<![a-z0-9_])" + re.escape(needle) + r"(?![a-z0-9_])"
            if re.search(pattern, text_lower):
                return game_id
    return None


def _match_alias(tokens: Iterable[str]) -> Optional[str]:
    for tok in tokens:
        mapped = _ALIASES.get(tok)
        if mapped:
            return mapped
    return None


def derive_target(description: Optional[str]) -> Optional[Dict[str, str]]:
    """Infer ``{"target_kind", "target_name"}`` from a free-text goal.

    Returns ``None`` when nothing recognizable is present (leaving the goal
    targetless — the pre-existing behaviour). Canonical game ids are matched
    first (they win regardless of language because they are the real ids);
    common Italian/English aliases are the fallback. Fully fail-safe.
    """
    if not isinstance(description, str) or not description.strip():
        return None
    text_lower = description.lower()

    game_id = _match_canonical_id(text_lower)
    if game_id is None:
        game_id = _match_alias(_tokens(text_lower))
    if game_id is None:
        return None

    kind = _kind_for_id(game_id)
    if kind is None:
        return None
    return {"target_kind": kind, "target_name": game_id}
