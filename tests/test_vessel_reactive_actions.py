"""Regression coverage for reactive Vessel world grounding and follow-ups."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from interface.vessel_interface import VesselInterface
from plugins.rift_vessel.vessel_base import WorldState
from plugins.rift_vessel.vessel_plugin import VesselPlugin


def _world_state() -> WorldState:
    return WorldState(
        environment="minecraft",
        health=20,
        position={"x": 10, "y": 64, "z": -3},
        possible_actions=[
            "say",
            "observe",
            "status",
            "scan",
            "inventory",
            "mine",
            "collect_block",
            "goto",
        ],
        flags={"connected": True, "is_day": True},
        extra={
            "inventory_counts": {"stick": 2},
            "entities": [{"name": "Remuraine", "type": "player", "distance": 3}],
            "blocks": [
                {
                    "name": "dark_oak_log",
                    "distance": 2.5,
                    "position": {"x": 12, "y": 64, "z": -3},
                    "unrelated": "dropped",
                }
            ],
            "affordances": [
                {
                    "kind": "block",
                    "target": "dark_oak_log",
                    "verb": "mine",
                    "distance": 2.5,
                    "position": {"x": 12, "y": 64, "z": -3},
                }
            ],
        },
    )


def test_reactive_world_snapshot_keeps_exact_affordances_and_is_bounded() -> None:
    snapshot = VesselInterface._compact_reactive_world_state(_world_state())

    assert snapshot["position"] == {"x": 10, "y": 64, "z": -3}
    assert snapshot["blocks"] == [
        {
            "name": "dark_oak_log",
            "distance": 2.5,
            "position": {"x": 12, "y": 64, "z": -3},
        }
    ]
    assert snapshot["affordances"][0]["target"] == "dark_oak_log"
    assert "unrelated" not in snapshot["blocks"][0]


def test_observation_followup_allows_world_actions_but_not_another_read() -> None:
    snapshot = VesselInterface._compact_reactive_world_state(_world_state())
    allowed = VesselInterface._reactive_followup_actions("minecraft", snapshot)

    assert "vessel_minecraft_mine" in allowed
    assert "vessel_minecraft_collect_block" in allowed
    assert "vessel_minecraft_goto" in allowed
    assert "vessel_minecraft_observe" not in allowed
    assert "vessel_minecraft_say" not in allowed
    assert "vessel_minecraft_inventory" not in allowed


@pytest.mark.asyncio
async def test_reactive_observe_enqueues_one_action_followup(monkeypatch) -> None:
    plugin = VesselPlugin.__new__(VesselPlugin)
    plugin.refresh_config = lambda: None
    plugin._connected_world = lambda: "minecraft"
    plugin._observe_surroundings = AsyncMock(
        return_value={
            "status": "ok",
            "data": {
                "environment": "minecraft",
                "position": {"x": 1, "y": 64, "z": 2},
                "possible_actions": ["observe", "mine", "collect_block"],
                "flags": {"connected": True},
                "blocks": [{"name": "dark_oak_log", "distance": 2}],
                "entities": [],
                "affordances": [
                    {
                        "kind": "block",
                        "target": "dark_oak_log",
                        "verb": "mine",
                        "distance": 2,
                    }
                ],
            },
        }
    )
    followup = AsyncMock()
    monkeypatch.setattr(
        plugin,
        "_get_vessel_interface",
        lambda: SimpleNamespace(enqueue_observation_followup=followup),
    )

    result = await plugin.handle_custom_action(
        "vessel_minecraft_observe",
        {},
        {"vessel_player_chat": True, "interface_path": "vessel/minecraft"},
    )

    assert result["status"] == "ok"
    followup.assert_awaited_once()
    assert followup.await_args.kwargs["environment"] == "minecraft"
    assert followup.await_args.kwargs["interface_path"] == "vessel/minecraft"


@pytest.mark.asyncio
async def test_autonomous_observe_does_not_enqueue_reactive_followup(monkeypatch) -> None:
    plugin = VesselPlugin.__new__(VesselPlugin)
    plugin.refresh_config = lambda: None
    plugin._connected_world = lambda: "minecraft"
    plugin._observe_surroundings = AsyncMock(
        return_value={"status": "ok", "data": {"possible_actions": ["mine"]}}
    )
    followup = AsyncMock()
    monkeypatch.setattr(
        plugin,
        "_get_vessel_interface",
        lambda: SimpleNamespace(enqueue_observation_followup=followup),
    )

    await plugin.handle_custom_action("vessel_minecraft_observe", {}, {})

    followup.assert_not_awaited()
