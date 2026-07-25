"""Tests for the Minecraft Vessel fast motorics reflex (``motor_step``).

The motor tick is the *fast, reactive* half of two-speed autonomy (AGENTS.md
§5c): it runs with **no LLM, no cognition turn, no diary**, just stepping the
body toward the active goal. These tests verify the purely **structural** rules
``MinecraftConnector.motor_step`` applies over the affordance contract
(``{kind, target, verb, distance}``) — never any keyword/text matching, and
always fail-safe.

A tiny ``_FakeConnector`` subclasses the real connector and stubs only the
three things ``motor_step`` touches (``_connected``, ``get_world_state`` and
``act``) so no real bridge / DB / network is required.
"""

from __future__ import annotations

from typing import Any

import pytest

from plugins.rift_vessel.minecraft.minecraft import MinecraftConnector
from plugins.rift_vessel.vessel_base import VesselActionResult, WorldState


# ----------------------------------------------------------------------
# Fake connector: stubs _connected, get_world_state and act only.
# ----------------------------------------------------------------------


class _FakeConnector(MinecraftConnector):
    def __init__(
        self,
        *,
        connected: bool = True,
        affordances: list[dict[str, Any]] | None = None,
        world_state: WorldState | None = None,
        raise_in_act: bool = False,
    ) -> None:
        super().__init__()
        self._connected = connected
        self._affordances = affordances or []
        self._world_state = world_state
        self._raise_in_act = raise_in_act
        # Records every (verb, payload) dispatched so tests can assert on it.
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_world_state(self) -> WorldState | None:
        if self._world_state is not None:
            return self._world_state
        if not self._connected:
            return None
        return WorldState(
            environment="minecraft",
            health=20.0,
            position={"x": 0.0, "y": 64.0, "z": 0.0},
            possible_actions=[],
            flags={"connected": True},
            extra={"affordances": self._affordances},
        )

    async def act(self, action: str, payload: dict[str, Any]) -> VesselActionResult:
        if self._raise_in_act:
            raise RuntimeError("bridge down")
        self.calls.append((action, payload or {}))
        return VesselActionResult(ok=True)


_ACTIVE_GOAL = {"id": 1, "description": "explore", "status": "active"}


# ----------------------------------------------------------------------
# Gating rules
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_motor_step_not_connected() -> None:
    conn = _FakeConnector(connected=False)
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result == {"acted": False, "reason": "not_connected"}
    assert conn.calls == []


@pytest.mark.asyncio
async def test_motor_step_no_goal() -> None:
    conn = _FakeConnector(connected=True)
    result = await conn.motor_step(None)
    assert result == {"acted": False, "reason": "no_goal"}
    assert conn.calls == []


@pytest.mark.asyncio
async def test_motor_step_no_world_state() -> None:
    conn = _FakeConnector(connected=True)
    # Force get_world_state to return None even though connected.
    conn._world_state = None
    conn._connected = True

    async def _none() -> None:
        return None

    conn.get_world_state = _none  # type: ignore[assignment,method-assign]
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result == {"acted": False, "reason": "no_world_state"}


# ----------------------------------------------------------------------
# Structural movement rules (verb/distance only — no keyword matching)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_motor_step_no_benign_affordance_wanders() -> None:
    # Only a hostile affordance is present → reflexes stay peaceful, roam.
    conn = _FakeConnector(
        affordances=[
            {"kind": "entity", "target": "zombie", "verb": "attack", "distance": 2.0}
        ]
    )
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result == {"acted": True, "action": "wander"}
    assert conn.calls == [("wander", {})]


@pytest.mark.asyncio
async def test_motor_step_empty_affordances_wanders() -> None:
    conn = _FakeConnector(affordances=[])
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result == {"acted": True, "action": "wander"}
    assert conn.calls == [("wander", {})]


@pytest.mark.asyncio
async def test_motor_step_block_within_reach_mines() -> None:
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "oak_log", "verb": "mine", "distance": 2.0}
        ]
    )
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result == {"acted": True, "action": "mine", "target": "oak_log"}
    assert conn.calls == [("mine", {"target": "oak_log"})]


@pytest.mark.asyncio
async def test_motor_step_nonblock_within_reach_uses() -> None:
    conn = _FakeConnector(
        affordances=[
            {"kind": "entity", "target": "chest", "verb": "use", "distance": 1.0}
        ]
    )
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result == {"acted": True, "action": "use", "target": "chest"}
    assert conn.calls == [("use", {"target": "chest"})]


@pytest.mark.asyncio
async def test_motor_step_out_of_reach_goes_to() -> None:
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "oak_log", "verb": "mine", "distance": 12.0}
        ]
    )
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result == {"acted": True, "action": "goto", "target": "oak_log"}
    assert conn.calls == [("goto", {"target": "oak_log"})]


@pytest.mark.asyncio
async def test_motor_step_takes_nearest_benign_affordance() -> None:
    # Affordances arrive distance-sorted (nearest first); the head wins.
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "stone", "verb": "mine", "distance": 1.0},
            {"kind": "block", "target": "oak_log", "verb": "mine", "distance": 3.0},
        ]
    )
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result["target"] == "stone"
    assert conn.calls == [("mine", {"target": "stone"})]


# ----------------------------------------------------------------------
# Goal-relevant travel (numeric destination — no keyword/text matching)
# ----------------------------------------------------------------------


_GOAL_WITH_DEST = {
    "id": 2,
    "description": "find a forest",
    "status": "active",
    "destination": {"x": 100.0, "z": -40.0},
}


@pytest.mark.asyncio
async def test_motor_step_travels_to_destination_when_no_affordance() -> None:
    # No benign affordance here, but the goal names a place to head → the body
    # steers toward that coordinate instead of wandering randomly.
    conn = _FakeConnector(affordances=[])
    result = await conn.motor_step(_GOAL_WITH_DEST)
    assert result["acted"] is True
    assert result["action"] == "goto"
    assert result["destination"] == {"x": 100.0, "z": -40.0}
    assert conn.calls == [("goto", {"x": 100.0, "z": -40.0})]


@pytest.mark.asyncio
async def test_motor_step_destination_includes_optional_y() -> None:
    conn = _FakeConnector(affordances=[])
    goal = {
        "id": 3,
        "description": "reach the hill",
        "status": "active",
        "destination": {"x": 10.0, "z": 10.0, "y": 72.0},
    }
    result = await conn.motor_step(goal)
    assert conn.calls == [("goto", {"x": 10.0, "z": 10.0, "y": 72.0})]
    assert result["action"] == "goto"


@pytest.mark.asyncio
async def test_motor_step_affordance_wins_over_destination() -> None:
    # A reachable affordance is acted on even if a destination is set — the
    # body works with what is right in front of it first.
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "oak_log", "verb": "mine", "distance": 1.0}
        ]
    )
    result = await conn.motor_step(_GOAL_WITH_DEST)
    assert result == {"acted": True, "action": "mine", "target": "oak_log"}
    assert conn.calls == [("mine", {"target": "oak_log"})]


@pytest.mark.asyncio
async def test_motor_step_pending_destination_wins_over_far_affordance() -> None:
    # A benign affordance that is *out of reach* must NOT hijack travel when a
    # destination is still pending — otherwise ubiquitous incidental scenery
    # (e.g. sand/sandstone in a desert) traps the body in place and the goal is
    # never reached. The reflex heads for the chosen coordinate instead.
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "sand", "verb": "use", "distance": 5.1},
            {"kind": "block", "target": "sandstone", "verb": "use", "distance": 5.0},
        ]
    )
    result = await conn.motor_step(_GOAL_WITH_DEST)
    assert result["acted"] is True
    assert result["action"] == "goto"
    assert result["destination"] == {"x": 100.0, "z": -40.0}
    assert conn.calls == [("goto", {"x": 100.0, "z": -40.0})]


@pytest.mark.asyncio
async def test_motor_step_arrived_at_destination_wanders_locally() -> None:
    # Already at the destination (within arrival radius) and nothing useful
    # here → roam locally to keep searching around the target.
    conn = _FakeConnector(
        affordances=[],
        world_state=WorldState(
            environment="minecraft",
            health=20.0,
            position={"x": 101.0, "y": 64.0, "z": -41.0},
            possible_actions=[],
            flags={"connected": True},
            extra={"affordances": []},
        ),
    )
    result = await conn.motor_step(_GOAL_WITH_DEST)
    assert result == {"acted": True, "action": "wander", "reason": "arrived"}
    assert conn.calls == [("wander", {})]


@pytest.mark.asyncio
async def test_motor_step_no_destination_still_wanders() -> None:
    # A goal without a destination behaves as before: last-resort wander.
    conn = _FakeConnector(affordances=[])
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result == {"acted": True, "action": "wander"}
    assert conn.calls == [("wander", {})]


# ----------------------------------------------------------------------
# Destination extraction from action payloads (numeric only)
# ----------------------------------------------------------------------


def test_extract_destination_from_payload() -> None:
    dest = MinecraftConnector._extract_destination(
        {"destination_x": "12", "destination_z": -5}
    )
    assert dest == {"x": 12.0, "z": -5.0}


def test_extract_destination_with_y() -> None:
    dest = MinecraftConnector._extract_destination(
        {"destination_x": 1, "destination_z": 2, "destination_y": 70}
    )
    assert dest == {"x": 1.0, "z": 2.0, "y": 70.0}


def test_extract_destination_missing_returns_none() -> None:
    assert MinecraftConnector._extract_destination({"destination_x": 1}) is None
    assert MinecraftConnector._extract_destination({}) is None


def test_extract_destination_non_numeric_returns_none() -> None:
    assert (
        MinecraftConnector._extract_destination(
            {"destination_x": "north", "destination_z": "east"}
        )
        is None
    )


# ----------------------------------------------------------------------
# Fail-safe
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_motor_step_failsafe_on_error() -> None:
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "oak_log", "verb": "mine", "distance": 1.0}
        ],
        raise_in_act=True,
    )
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result["acted"] is False
    assert result["reason"] == "error"
    assert "bridge down" in result["error"]
