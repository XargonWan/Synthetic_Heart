"""Tests for Minecraft goal-target derivation from free text.

Covers the user-authorized keyword exception in
:mod:`plugins.rift_vessel.minecraft.target_names`: when the will/action beat
authors a free-text goal but omits ``target_kind``/``target_name``, the target
is inferred from the Minecraft item/block/mob names in the description so the
motor reflex has something to head for.
"""

from __future__ import annotations

import pytest

from plugins.rift_vessel.minecraft import target_names as tn
from plugins.rift_vessel.minecraft.minecraft import MinecraftConnector


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        # canonical english ids
        (
            "gather some oak_log for a shelter",
            {"target_kind": "block", "target_name": "oak_log"},
        ),
        (
            "mine diamond_ore deep down",
            {"target_kind": "block", "target_name": "diamond_ore"},
        ),
        ("hunt a cow for food", {"target_kind": "entity", "target_name": "cow"}),
        # spaced form of an id
        ("go mine some iron ore", {"target_kind": "block", "target_name": "iron_ore"}),
        # english shorthand alias
        ("collect wood", {"target_kind": "block", "target_name": "oak_log"}),
        ("find coal", {"target_kind": "block", "target_name": "coal_ore"}),
        # italian aliases
        ("vai a raccogliere legna", {"target_kind": "block", "target_name": "oak_log"}),
        ("scava un po' di pietra", {"target_kind": "block", "target_name": "stone"}),
        ("cerca del ferro", {"target_kind": "block", "target_name": "iron_ore"}),
        ("caccia una mucca", {"target_kind": "entity", "target_name": "cow"}),
        # longest-id preference
        (
            "mine deepslate_iron_ore",
            {"target_kind": "block", "target_name": "deepslate_iron_ore"},
        ),
        # Regression: a crafting/utility block named as an instrumental means
        # ("then craft a crafting table") must NOT become the target — the real
        # gather objective (acacia_log) wins, even though ``crafting_table`` is
        # a longer id and appears in the text.
        (
            "Locate the nearest acacia_leaves block, punch it to harvest the "
            "acacia_log, then immediately craft a wooden pickaxe and a crafting "
            "table to secure a tool tier before nightfall.",
            {"target_kind": "block", "target_name": "acacia_log"},
        ),
    ],
)
def test_derive_target_hits(description: str, expected: dict[str, str]) -> None:
    assert tn.derive_target(description) == expected


@pytest.mark.parametrize(
    "description",
    [
        "",
        "   ",
        "explore the world and enjoy the view",
        "just wander around a bit",
        # crafting/utility blocks are not natural mine objectives → no target
        "craft a crafting table and a furnace",
        "place a chest to store items",
        None,
    ],
)
def test_derive_target_misses(description: str | None) -> None:
    assert tn.derive_target(description) is None


def test_resolve_goal_target_explicit_wins() -> None:
    # Explicit cognition-provided fields must not be overwritten by derivation.
    payload = {
        "description": "gather some oak_log",
        "target_kind": "entity",
        "target_name": "cow",
    }
    kind, name = MinecraftConnector._resolve_goal_target(
        payload, payload["description"]
    )
    assert (kind, name) == ("entity", "cow")


def test_resolve_goal_target_derives_when_missing() -> None:
    payload = {"description": "vai a raccogliere legna di quercia"}
    kind, name = MinecraftConnector._resolve_goal_target(
        payload, payload["description"]
    )
    assert (kind, name) == ("block", "oak_log")


def test_resolve_goal_target_none_when_no_match() -> None:
    payload = {"description": "explore and relax"}
    kind, name = MinecraftConnector._resolve_goal_target(
        payload, payload["description"]
    )
    assert (kind, name) == (None, None)
