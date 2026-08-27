"""Tests for the Rift Vessel starter-goal progression seeding.

At first login Synth has no goal and often nothing interactable nearby, so the
knowledge-base query used to orient the *first* goal would be empty. The
starter-goal seeding (AGENTS.md §5c) fills that gap by deriving a **structural**
KB query from Synth's real situation — the item ids she holds and the block ids
around her — never a scripted progression catalogue (spontaneity rule) and never
any keyword/text matching.

These tests exercise the purely structural helpers on the Minecraft connector:

* ``MinecraftConnector._progression_query_tokens`` — the static token builder,
* ``MinecraftConnector.get_progression_context`` — the world-agnostic override
  reading the connector's last-known telemetry,

plus the base-class default so a bare connector degrades gracefully.
"""

from __future__ import annotations

from typing import Any

from plugins.rift_vessel.minecraft.minecraft import MinecraftConnector
from plugins.rift_vessel.vessel_base import VesselConnectorBase


# ----------------------------------------------------------------------
# _progression_query_tokens — structural, quantity-ordered, capped, safe
# ----------------------------------------------------------------------


def test_progression_tokens_orders_inventory_by_quantity() -> None:
    counts = {"oak_log": 3, "dirt": 12, "cobblestone": 7}
    tokens = MinecraftConnector._progression_query_tokens(counts, None)
    # Most-held item leads the query; all are lowercased ids.
    assert tokens == ["dirt", "cobblestone", "oak_log"]


def test_progression_tokens_includes_block_names() -> None:
    counts = {"stick": 2}
    blocks = [{"name": "Stone"}, {"name": "iron_ore"}, {"not_a_name": 1}]
    tokens = MinecraftConnector._progression_query_tokens(counts, blocks)
    assert tokens[0] == "stick"
    assert "stone" in tokens
    assert "iron_ore" in tokens


def test_progression_tokens_dedups_and_caps() -> None:
    counts = {f"item_{i}": i for i in range(10)}
    blocks = [{"name": "item_9"}, {"name": "extra_block"}]
    tokens = MinecraftConnector._progression_query_tokens(counts, blocks)
    # Deduped and capped at 6.
    assert len(tokens) == 6
    assert len(set(tokens)) == len(tokens)


def test_progression_tokens_empty_inputs() -> None:
    assert MinecraftConnector._progression_query_tokens(None, None) == []
    assert MinecraftConnector._progression_query_tokens({}, []) == []


def test_progression_tokens_failsafe_on_bad_input() -> None:
    # A non-dict / non-iterable slipping through must degrade to [], not raise.
    bad: Any = 123
    assert MinecraftConnector._progression_query_tokens(bad, bad) == []


# ----------------------------------------------------------------------
# get_progression_context — reads cached telemetry, returns None when empty
# ----------------------------------------------------------------------


def test_get_progression_context_uses_last_telemetry() -> None:
    conn = MinecraftConnector()
    conn._last_inventory_counts = {"oak_log": 4}
    conn._last_blocks = [{"name": "stone"}]
    tokens = conn.get_progression_context()
    assert tokens is not None
    assert "oak_log" in tokens
    assert "stone" in tokens


def test_get_progression_context_none_when_no_telemetry() -> None:
    conn = MinecraftConnector()
    # Defaults from __init__: empty inventory + no blocks.
    assert conn.get_progression_context() is None


# ----------------------------------------------------------------------
# Base-class default — a connector without the override returns None
# ----------------------------------------------------------------------


def test_base_progression_context_default_is_none() -> None:
    class _Bare(VesselConnectorBase):
        async def connect(self, settings, on_event):  # type: ignore[override]
            return True

        async def disconnect(self):  # type: ignore[override]
            return None

        async def get_world_state(self):  # type: ignore[override]
            return None

        async def act(self, action, payload):  # type: ignore[override]
            from plugins.rift_vessel.vessel_base import VesselActionResult

            return VesselActionResult(ok=True)

    assert _Bare().get_progression_context() is None


# ----------------------------------------------------------------------
# is_ephemeral_event — pure-log telemetry vs durable game-experience
# ----------------------------------------------------------------------


def test_ephemeral_event_types_are_pure_log() -> None:
    from plugins.rift_vessel.vessel_base import (
        EPHEMERAL_EVENT_TYPES,
        is_ephemeral_event,
    )

    # Every declared telemetry kind classifies as ephemeral (pure log).
    for kind in ("sighting", "gather", "proximity", "spawn", "movement", "status"):
        assert kind in EPHEMERAL_EVENT_TYPES
        assert is_ephemeral_event(kind) is True


def test_durable_event_types_are_persisted() -> None:
    from plugins.rift_vessel.vessel_base import is_ephemeral_event

    # Game-experience kinds worth remembering are NOT ephemeral.
    for kind in ("chat", "damage", "death", "self_monologue", "disconnect"):
        assert is_ephemeral_event(kind) is False


def test_unknown_event_defaults_to_durable() -> None:
    from plugins.rift_vessel.vessel_base import is_ephemeral_event

    # Safe default: a new/unknown event kind is never silently dropped.
    assert is_ephemeral_event("some_new_event_kind") is False
    assert is_ephemeral_event("") is False
    assert is_ephemeral_event(None) is False
