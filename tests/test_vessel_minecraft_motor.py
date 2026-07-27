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
        act_ok: bool = True,
    ) -> None:
        super().__init__()
        self._connected = connected
        self._affordances = affordances or []
        self._world_state = world_state
        self._raise_in_act = raise_in_act
        self._act_ok = act_ok
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
        return VesselActionResult(ok=self._act_ok)


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
async def test_motor_step_no_benign_affordance_marches() -> None:
    # Only a hostile affordance is present → reflexes stay peaceful, and with
    # no target/destination the body marches along a persistent heading rather
    # than a random wander (which drifts in tight loops resembling circling).
    conn = _FakeConnector(
        affordances=[
            {"kind": "entity", "target": "zombie", "verb": "attack", "distance": 2.0}
        ]
    )
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result["acted"] is True
    assert result["action"] == "goto"
    assert result["reason"] == "directional_march"
    assert set(result["destination"]) == {"x", "z"}
    assert conn.calls[-1][0] == "goto"


@pytest.mark.asyncio
async def test_motor_step_empty_affordances_marches() -> None:
    conn = _FakeConnector(affordances=[])
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result["acted"] is True
    assert result["action"] == "goto"
    assert result["reason"] == "directional_march"
    assert conn.calls[-1][0] == "goto"


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
async def test_motor_step_does_not_repeat_same_in_reach_interaction() -> None:
    # Regression: the "freezes inert at one point" bug. A live scan keeps
    # re-surfacing the *same* adjacent benign affordance every tick; without an
    # anti-repeat guard the reflex ``use``/``mine``d it forever and the body
    # never moved. First tick interacts; the second must skip it and fall
    # through to the directional march so the body keeps exploring.
    conn = _FakeConnector(
        affordances=[
            {"kind": "entity", "target": "chest", "verb": "use", "distance": 1.0}
        ]
    )
    first = await conn.motor_step(_ACTIVE_GOAL)
    assert first == {"acted": True, "action": "use", "target": "chest"}

    second = await conn.motor_step(_ACTIVE_GOAL)
    assert second["acted"] is True
    assert second["action"] == "goto"
    assert second["reason"] == "directional_march"
    assert conn.calls[-1][0] == "goto"


@pytest.mark.asyncio
async def test_motor_step_interacts_with_new_in_reach_after_previous() -> None:
    # The anti-repeat guard is per exact id: a *different* adjacent affordance
    # must still be interacted with, so the body isn't wrongly frozen out of
    # grabbing something genuinely new right in front of it.
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "oak_log", "verb": "mine", "distance": 2.0}
        ]
    )
    first = await conn.motor_step(_ACTIVE_GOAL)
    assert first == {"acted": True, "action": "mine", "target": "oak_log"}

    conn._affordances = [
        {"kind": "block", "target": "stone", "verb": "mine", "distance": 2.0}
    ]
    second = await conn.motor_step(_ACTIVE_GOAL)
    assert second == {"acted": True, "action": "mine", "target": "stone"}


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
async def test_motor_step_pending_destination_wins_over_reachable_affordance() -> None:
    # While a chosen destination is still pending, travel wins even over an
    # affordance *within reach*: in a desert sand/sandstone are always a couple
    # of blocks away, so stopping to ``use``/``mine`` them every tick would trap
    # the body on the spot and it would never actually reach the goal. The
    # incidental block right in front is ignored until we have arrived.
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "sandstone", "verb": "use", "distance": 1.0}
        ]
    )
    result = await conn.motor_step(_GOAL_WITH_DEST)
    assert result["acted"] is True
    assert result["action"] == "goto"
    assert result["destination"] == {"x": 100.0, "z": -40.0}
    assert conn.calls == [("goto", {"x": 100.0, "z": -40.0})]


@pytest.mark.asyncio
async def test_motor_step_reachable_affordance_acted_when_arrived() -> None:
    # Once we have arrived at (or have no) destination, a reachable affordance
    # IS acted on — the body works with what is right in front of it.
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "oak_log", "verb": "mine", "distance": 1.0}
        ]
    )
    goal_no_dest = {"id": 9, "description": "gather", "status": "active"}
    result = await conn.motor_step(goal_no_dest)
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
async def test_motor_step_does_not_pace_back_to_consumed_destination() -> None:
    # Regression ("same path back and forth"): a goal's numeric destination is
    # chosen once by the slow will beat and stays static. After arriving, the
    # on-arrival wander drifts the body a few metres past the arrival radius, so
    # a naive reflex would read the SAME static destination as pending again and
    # ``goto`` back to it — pacing between the two spots forever until the slow
    # beat refreshes the goal. Once reached, the destination must be "consumed":
    # later ticks explore *beyond* it (directional march) instead of returning.
    dest = {"x": 100.0, "z": -40.0}
    goal = {
        "id": 42,
        "description": "wander off",
        "status": "active",
        "destination": dest,
    }
    conn = _FakeConnector(
        affordances=[],
        world_state=WorldState(
            environment="minecraft",
            health=20.0,
            # Inside the arrival radius of the destination → counts as arrived.
            position={"x": 101.0, "y": 64.0, "z": -41.0},
            possible_actions=[],
            flags={"connected": True},
            extra={"affordances": []},
        ),
    )
    # Tick 1: arrival → wander locally, and the destination is marked consumed.
    first = await conn.motor_step(goal)
    assert first == {"acted": True, "action": "wander", "reason": "arrived"}

    # Simulate the wander drifting the body a few blocks past the arrival radius,
    # so the SAME static destination is now > _ARRIVAL_RADIUS away again.
    conn._world_state = WorldState(
        environment="minecraft",
        health=20.0,
        position={"x": 108.0, "y": 64.0, "z": -48.0},
        possible_actions=[],
        flags={"connected": True},
        extra={"affordances": []},
    )
    # Tick 2: must NOT goto back to the consumed destination — it explores on.
    second = await conn.motor_step(goal)
    assert second["action"] == "goto"
    assert second.get("reason") == "directional_march"
    assert second.get("destination") != dest


@pytest.mark.asyncio
async def test_motor_step_new_goal_revives_destination_after_consumed() -> None:
    # Consuming a destination is scoped to *that* goal: when the slow will beat
    # hands the body a fresh goal (new key) with a new destination, the reflex
    # travels to it normally — the consumed flag from the old goal never leaks.
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
    old_goal = {
        "id": 1,
        "description": "a",
        "status": "active",
        "destination": {"x": 100.0, "z": -40.0},
    }
    # Arrive at + consume the old goal's destination.
    await conn.motor_step(old_goal)

    # A brand-new goal with a far destination must be travelled to.
    new_goal = {
        "id": 2,
        "description": "b",
        "status": "active",
        "destination": {"x": 900.0, "z": 900.0},
    }
    result = await conn.motor_step(new_goal)
    assert result["action"] == "goto"
    assert result["destination"] == {"x": 900.0, "z": 900.0}


@pytest.mark.asyncio
async def test_motor_step_gives_up_on_unreachable_destination() -> None:
    # Regression ("same road back and forth", live-observed): a will-beat
    # coordinate can be physically unreachable (water/ravine/cliff). The
    # pathfinder keeps closing to a few metres, fails, resets and re-approaches,
    # so ``remaining`` oscillates and the body never enters _ARRIVAL_RADIUS to
    # "arrive" — pacing the same path forever. The progress watchdog must give
    # up on the destination after _STALE_TRAVEL_TICKS without meaningful
    # improvement, consuming it so the body marches on. Purely numeric.
    dest = {"x": 350.0, "z": 160.0}
    goal = {
        "id": 7,
        "description": "go there",
        "status": "active",
        "destination": dest,
    }
    conn = _FakeConnector(affordances=[])

    # Oscillating positions that never get within _ARRIVAL_RADIUS of dest and
    # never meaningfully improve on the first (best) distance — the unreachable
    # signature. First tick fixes best ~9.9; every later tick stays >= best so
    # the stall counter climbs until the watchdog trips.
    positions = [
        {"x": 343.0, "y": 64.0, "z": 153.0},  # remaining ~9.9 (best)
        {"x": 310.0, "y": 64.0, "z": 120.0},  # remaining ~57 (bounced away)
        {"x": 343.0, "y": 64.0, "z": 153.0},  # remaining ~9.9 (no improvement)
        {"x": 312.0, "y": 64.0, "z": 122.0},  # remaining ~54
        {"x": 343.0, "y": 64.0, "z": 153.0},  # remaining ~9.9
        {"x": 311.0, "y": 64.0, "z": 121.0},  # remaining ~55
        {"x": 343.0, "y": 64.0, "z": 153.0},  # remaining ~9.9
        {"x": 340.0, "y": 64.0, "z": 150.0},  # remaining ~14
    ]

    def _state_for(pos: dict[str, float]) -> WorldState:
        return WorldState(
            environment="minecraft",
            health=20.0,
            position=pos,
            possible_actions=[],
            flags={"connected": True},
            extra={"affordances": []},
        )

    last: dict[str, Any] = {}
    for pos in positions:
        conn._world_state = _state_for(pos)
        last = await conn.motor_step(goal)

    # After the watchdog trips, the destination is consumed and the body no
    # longer steers back to the static waypoint — it marches on instead.
    assert conn._consumed_destination_key == conn._goal_key(goal)
    assert last["action"] == "goto"
    assert last.get("reason") == "directional_march"
    assert last.get("destination") != dest


@pytest.mark.asyncio
async def test_motor_step_gives_up_when_body_stops_moving() -> None:
    # Regression ("synth stuck at one spot forever", live-observed): the
    # pathfinder abandons an unwalkable coordinate and the body simply *stops
    # moving* while the motor re-issues the same ``goto`` every tick. The
    # distance number can still oscillate (or stay just outside the arrival
    # ring), so the distance-based watchdog may never trip — but the body's
    # *actual* displacement is ~0. The physical-motion watchdog must notice the
    # body isn't moving and consume the destination so it marches on. Purely
    # numeric (measured motion), no keywords.
    dest = {"x": 350.0, "z": 160.0}
    goal = {
        "id": 99,
        "description": "go there",
        "status": "active",
        "destination": dest,
    }
    conn = _FakeConnector(affordances=[])

    # Body pinned at ~(354.6, 163.6): each tick nudges it < _STUCK_MOVE_EPS
    # (0.75) blocks — the stuck signature. remaining stays ~5.9, always outside
    # _ARRIVAL_RADIUS (4.0) so it never "arrives", yet moved-per-tick is tiny.
    base_x, base_z = 354.6, 163.6
    nudges = [
        (0.0, 0.0),
        (0.2, 0.1),
        (0.1, 0.2),
        (0.3, 0.0),
        (0.0, 0.3),
        (0.2, 0.2),
    ]

    def _state_for(x: float, z: float) -> WorldState:
        return WorldState(
            environment="minecraft",
            health=20.0,
            position={"x": x, "y": 62.0, "z": z},
            possible_actions=[],
            flags={"connected": True},
            extra={"affordances": []},
        )

    last: dict[str, Any] = {}
    for dx, dz in nudges:
        conn._world_state = _state_for(base_x + dx, base_z + dz)
        last = await conn.motor_step(goal)

    # The physical watchdog tripped: destination consumed, body marches on.
    assert conn._consumed_destination_key == conn._goal_key(goal)
    assert last["action"] == "goto"
    assert last.get("reason") == "directional_march"
    assert last.get("destination") != dest


@pytest.mark.asyncio
async def test_motor_step_gives_up_when_body_stuck_on_named_target() -> None:
    # Regression ("synth ancora bloccato", live-observed at (340.5, 60, 161.5),
    # y=60 in a dug pit / water): the goal names a structural block/entity
    # target with NO numeric destination, so the motor loops the goal_target
    # branch — ``goto reason=None remaining=None dest=None`` every 3 s — toward
    # a thing the pathfinder can never reach. That branch carries no distance
    # number, so the earlier dest-only watchdog was blind to it and the body
    # stayed pinned forever. The **global** physical-motion watchdog must notice
    # the body isn't moving and force the directional march regardless of which
    # branch issued the goto. Purely numeric (measured motion), no keywords.
    goal = {
        "id": 123,
        "description": "reach that thing",
        "status": "active",
        "target_kind": "block",
        "target_name": "unreachable_block",
    }
    conn = _FakeConnector(affordances=[])

    # Body wedged at ~(340.5, 161.5): each tick nudges it < _STUCK_MOVE_EPS
    # (0.75) blocks — the stuck signature — while the motor keeps aiming the
    # named target. There is NO destination, so only body displacement can
    # break the loop.
    # Five nudges: tick 1 seeds the position (no prior sample), ticks 2-5 each
    # move < _STUCK_MOVE_EPS, so on the 5th tick _stuck_position_ticks reaches
    # _STUCK_POSITION_TICKS (4) and the watchdog forces the march. Ending on the
    # trip tick asserts the break-out; if the body were still wedged the next
    # tick would trip again on the next stuck run (rotating the heading each
    # time), which is the intended keep-trying-fresh-directions behaviour.
    base_x, base_z = 340.5, 161.5
    nudges = [
        (0.0, 0.0),
        (0.1, 0.1),
        (0.2, 0.0),
        (0.0, 0.2),
        (0.1, 0.2),
    ]

    def _state_for(x: float, z: float) -> WorldState:
        return WorldState(
            environment="minecraft",
            health=20.0,
            position={"x": x, "y": 60.0, "z": z},
            possible_actions=[],
            flags={"connected": True},
            extra={"affordances": []},
        )

    last: dict[str, Any] = {}
    for dx, dz in nudges:
        conn._world_state = _state_for(base_x + dx, base_z + dz)
        last = await conn.motor_step(goal)

    # The global physical watchdog tripped: the named-target goto was suppressed
    # and the body marches on in a fresh direction instead of staying pinned.
    assert last["action"] == "goto"
    assert last.get("reason") == "directional_march"
    assert "target" not in last


@pytest.mark.asyncio
async def test_motor_step_keeps_pursuing_reachable_destination() -> None:
    # The watchdog must NOT trip while the body is genuinely closing the gap:
    # steadily improving distances toward a destination keep the motor steering
    # to it (no premature give-up). Purely numeric.
    dest = {"x": 200.0, "z": 0.0}
    goal = {
        "id": 8,
        "description": "walk over",
        "status": "active",
        "destination": dest,
    }
    conn = _FakeConnector(affordances=[])

    def _state_at(x: float) -> WorldState:
        return WorldState(
            environment="minecraft",
            health=20.0,
            position={"x": x, "y": 64.0, "z": 0.0},
            possible_actions=[],
            flags={"connected": True},
            extra={"affordances": []},
        )

    last: dict[str, Any] = {}
    for x in [20.0, 40.0, 60.0, 80.0, 100.0, 120.0, 140.0, 160.0]:
        conn._world_state = _state_at(x)
        last = await conn.motor_step(goal)

    # Still travelling toward the (steadily closer) destination — never consumed.
    assert conn._consumed_destination_key != conn._goal_key(goal)
    assert last["action"] == "goto"
    assert last["destination"] == dest


@pytest.mark.asyncio
async def test_motor_step_explores_forward_after_arrival() -> None:
    # Synth must not stay locked at an arrived destination while the slow will
    # beat is silent. On the arrival tick it wanders locally once (giving
    # cognition a brief chance to re-aim); from the next tick the reached
    # destination is *consumed* and the body marches forward along its
    # persistent heading, inventing its own next waypoint so plans can change on
    # their own. Structural only — no goal text is ever read.
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
    # Arrival tick: wander locally and consume the destination.
    first = await conn.motor_step(_GOAL_WITH_DEST)
    assert first == {"acted": True, "action": "wander", "reason": "arrived"}

    # Next tick: destination consumed → march forward with a self-chosen goto.
    result = await conn.motor_step(_GOAL_WITH_DEST)
    assert result["acted"] is True
    assert result["action"] == "goto"
    assert result["reason"] == "directional_march"
    dest = result["destination"]
    assert set(dest) == {"x", "z"}
    # The new waypoint is at least _MIN_TRAVEL_DISTANCE away from where we sit.
    dx = dest["x"] - 101.0
    dz = dest["z"] - (-41.0)
    assert (dx * dx + dz * dz) ** 0.5 >= MinecraftConnector._MIN_TRAVEL_DISTANCE - 1e-6
    # The last dispatched call is the forward goto.
    assert conn.calls[-1][0] == "goto"


@pytest.mark.asyncio
async def test_motor_step_arrived_with_far_affordance_does_not_chase() -> None:
    # Regression (runtime freeze): once the destination is REACHED, an
    # out-of-reach benign affordance must NOT be chased forever. If it were,
    # the body would loop ``goto <affordance>`` every tick and never fall into
    # the arrival/anti-stall block — so it would appear frozen on the spot,
    # exactly the "must not stay locked at destination" bug. Arrival must win:
    # the reflex wanders locally (and later reprojects), it does not chase the
    # incidental scenery.
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "sand", "verb": "use", "distance": 6.0},
        ],
        world_state=WorldState(
            environment="minecraft",
            health=20.0,
            position={"x": 101.0, "y": 64.0, "z": -41.0},
            possible_actions=[],
            flags={"connected": True},
            extra={
                "affordances": [
                    {
                        "kind": "block",
                        "target": "sand",
                        "verb": "use",
                        "distance": 6.0,
                    },
                ]
            },
        ),
    )
    result = await conn.motor_step(_GOAL_WITH_DEST)
    # Arrived + far affordance → local roam, NOT a goto toward "sand".
    assert result == {"acted": True, "action": "wander", "reason": "arrived"}
    assert conn.calls == [("wander", {})]


@pytest.mark.asyncio
async def test_motor_step_stall_counter_resets_on_new_goal() -> None:
    # A fresh goal (different id/destination) resets the stall counter, so
    # cognition re-aiming the body always gets the full grace window again.
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
    goal_a = {"id": 1, "description": "a", "destination": {"x": 100.0, "z": -40.0}}
    goal_b = {"id": 2, "description": "b", "destination": {"x": 100.0, "z": -40.0}}
    # Idle to the brink of staleness on goal A.
    for _ in range(MinecraftConnector._STALE_ARRIVAL_TICKS - 1):
        await conn.motor_step(goal_a)
    # Switching to goal B resets the counter → still a local wander, not a jump.
    result = await conn.motor_step(goal_b)
    assert result == {"acted": True, "action": "wander", "reason": "arrived"}


@pytest.mark.asyncio
async def test_motor_step_no_destination_marches_along_heading() -> None:
    # A goal without a destination or a structural target marches along a
    # persistent heading. Repeated ticks keep the SAME heading until the leg is
    # reached/stalls, so exploration is a directed line, not a spin in place.
    conn = _FakeConnector(affordances=[])
    first = await conn.motor_step(_ACTIVE_GOAL)
    assert first["action"] == "goto"
    assert first["reason"] == "directional_march"
    # The heading only rotates on arrival/stall; a second tick short of that
    # keeps the same explore heading (structural, no goal text read).
    heading_before = conn._explore_heading
    await conn.motor_step(_ACTIVE_GOAL)
    assert conn._explore_heading == heading_before


@pytest.mark.asyncio
async def test_motor_step_march_falls_back_to_wander_without_position() -> None:
    # If the live position is unknown, reprojection fails → last-resort wander.
    conn = _FakeConnector(
        affordances=[],
        world_state=WorldState(
            environment="minecraft",
            health=20.0,
            position=None,
            possible_actions=[],
            flags={"connected": True},
            extra={"affordances": []},
        ),
    )
    result = await conn.motor_step(_ACTIVE_GOAL)
    assert result == {"acted": True, "action": "wander"}
    assert conn.calls == [("wander", {})]


@pytest.mark.asyncio
async def test_motor_step_block_target_routes_goto_by_name() -> None:
    # A goal that names a structural block target (from the live scan) → the
    # body walks to that exact id via ``goto target=<name>`` instead of
    # marching/wandering. This is the idea→technical-action translation that
    # stops the circling. Structural only: the goal free text is never read.
    conn = _FakeConnector(affordances=[])
    goal = {
        "id": 7,
        "description": "find a forest",
        "target_kind": "block",
        "target_name": "oak_log",
    }
    result = await conn.motor_step(goal)
    assert result == {
        "acted": True,
        "action": "goto",
        "target": "oak_log",
        "target_kind": "block",
        "target_result": "arrived",
    }
    assert conn.calls == [("goto", {"target": "oak_log"})]


@pytest.mark.asyncio
async def test_motor_step_entity_target_routes_goto_by_name() -> None:
    conn = _FakeConnector(affordances=[])
    goal = {
        "id": 8,
        "description": "tame a cow",
        "target_kind": "entity",
        "target_name": "cow",
    }
    result = await conn.motor_step(goal)
    assert result == {
        "acted": True,
        "action": "goto",
        "target": "cow",
        "target_kind": "entity",
        "target_result": "arrived",
    }
    assert conn.calls == [("goto", {"target": "cow"})]


@pytest.mark.asyncio
async def test_motor_step_coordinate_target_kind_is_not_routed_as_name() -> None:
    # A 'coordinate' target kind is NOT a named thing — it must fall through to
    # the directional march (the numeric destination path handles coordinates).
    conn = _FakeConnector(affordances=[])
    goal = {
        "id": 9,
        "description": "go somewhere",
        "target_kind": "coordinate",
        "target_name": "",
    }
    result = await conn.motor_step(goal)
    assert result["action"] == "goto"
    assert result["reason"] == "directional_march"


# ----------------------------------------------------------------------
# Structural 3-state target outcome (arrived / not_found / unreachable)
# ----------------------------------------------------------------------


def _ws_with_scan(
    *,
    blocks: list[dict[str, Any]] | None = None,
    entities: list[dict[str, Any]] | None = None,
) -> WorldState:
    return WorldState(
        environment="minecraft",
        health=20.0,
        position={"x": 0.0, "y": 64.0, "z": 0.0},
        possible_actions=[],
        flags={"connected": True},
        extra={
            "affordances": [],
            "blocks": blocks or [],
            "entities": entities or [],
        },
    )


def test_scan_has_target_block_present() -> None:
    ws = _ws_with_scan(blocks=[{"name": "OAK_LOG"}, {"name": "stone"}])
    assert (
        MinecraftConnector._scan_has_target(ws, {"kind": "block", "name": "oak_log"})
        is True
    )


def test_scan_has_target_block_absent() -> None:
    ws = _ws_with_scan(blocks=[{"name": "stone"}])
    assert (
        MinecraftConnector._scan_has_target(ws, {"kind": "block", "name": "oak_log"})
        is False
    )


def test_scan_has_target_entity_matches_type_or_name() -> None:
    ws = _ws_with_scan(entities=[{"type": "Cow"}, {"name": "sheep"}])
    assert (
        MinecraftConnector._scan_has_target(ws, {"kind": "entity", "name": "cow"})
        is True
    )
    assert (
        MinecraftConnector._scan_has_target(ws, {"kind": "entity", "name": "sheep"})
        is True
    )
    assert (
        MinecraftConnector._scan_has_target(ws, {"kind": "entity", "name": "pig"})
        is False
    )


def test_scan_has_target_fail_safe_on_bad_state() -> None:
    assert MinecraftConnector._scan_has_target(object(), {"name": "x"}) is False
    assert MinecraftConnector._scan_has_target(_ws_with_scan(), {"name": ""}) is False


@pytest.mark.asyncio
async def test_motor_step_records_arrived_on_success() -> None:
    conn = _FakeConnector(affordances=[], act_ok=True)
    conn._world_state = _ws_with_scan(blocks=[{"name": "oak_log"}])
    goal = {"id": 1, "target_kind": "block", "target_name": "oak_log"}
    result = await conn.motor_step(goal)
    assert result["target_result"] == "arrived"
    assert conn._last_target_result == "arrived"
    assert conn._last_target_name == "oak_log"
    assert conn._last_target_kind == "block"


@pytest.mark.asyncio
async def test_motor_step_records_unreachable_when_in_scan_but_fails() -> None:
    # goto fails BUT the exact id is present in the live scan → unreachable.
    conn = _FakeConnector(affordances=[], act_ok=False)
    conn._world_state = _ws_with_scan(blocks=[{"name": "oak_log"}])
    goal = {"id": 1, "target_kind": "block", "target_name": "oak_log"}
    result = await conn.motor_step(goal)
    assert result["target_result"] == "unreachable"
    assert conn._last_target_result == "unreachable"
    assert conn._last_target_name == "oak_log"


@pytest.mark.asyncio
async def test_motor_step_records_not_found_when_absent_from_scan() -> None:
    # goto fails AND the id is NOT in the scan → not_found.
    conn = _FakeConnector(affordances=[], act_ok=False)
    conn._world_state = _ws_with_scan(blocks=[{"name": "stone"}])
    goal = {"id": 1, "target_kind": "block", "target_name": "oak_log"}
    result = await conn.motor_step(goal)
    assert result["target_result"] == "not_found"
    assert conn._last_target_result == "not_found"


def test_record_target_outcome_is_fail_safe() -> None:
    # A bad result object never crashes and leaves prior feedback untouched.
    conn = _FakeConnector()
    conn._last_target_result = "arrived"
    conn._record_target_outcome(_ws_with_scan(), {"kind": "block", "name": "x"}, None)  # type: ignore[arg-type]
    # None has no .ok → treated as failure; x not in empty scan → not_found.
    assert conn._last_target_result == "not_found"


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


# ----------------------------------------------------------------------
# Phase 4: a named BLOCK target already within reach is MINED, not walked to
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_motor_step_mines_named_block_target_when_within_reach() -> None:
    # Cognition named a block target (oak_log) that is already within reach, but
    # the generic in-reach affordance branch was skipped this tick (the block
    # was just interacted with and re-surfaced). The named-target branch must
    # still MINE it rather than walk in place — the "walks up but never picks it
    # up" gap. Structural match only: same kind + exact id + distance ≤ reach.
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "oak_log", "verb": "use", "distance": 1.5}
        ]
    )
    # Force the generic in-reach branch to skip oak_log so control reaches the
    # named-target branch (it dedupes against the last reflex interaction).
    conn._last_reflex_interaction = "block:oak_log"
    goal = {
        "id": 11,
        "description": "gather wood",
        "target_kind": "block",
        "target_name": "oak_log",
    }
    result = await conn.motor_step(goal)
    assert result == {
        "acted": True,
        "action": "mine",
        "target": "oak_log",
        "target_kind": "block",
    }
    assert conn.calls == [("mine", {"target": "oak_log"})]


@pytest.mark.asyncio
async def test_motor_step_walks_to_named_block_target_when_out_of_reach() -> None:
    # A named block target whose affordance is beyond MOTOR_REACH → the reflex
    # walks to it by name (goto), never a premature mine.
    conn = _FakeConnector(
        affordances=[
            {"kind": "block", "target": "oak_log", "verb": "use", "distance": 9.0}
        ]
    )
    conn._last_reflex_interaction = "block:oak_log"
    goal = {
        "id": 12,
        "description": "gather wood",
        "target_kind": "block",
        "target_name": "oak_log",
    }
    result = await conn.motor_step(goal)
    assert result["action"] == "goto"
    assert result["target"] == "oak_log"
    assert ("mine", {"target": "oak_log"}) not in conn.calls


@pytest.mark.asyncio
async def test_motor_step_entity_target_never_mined() -> None:
    # An ENTITY target is NEVER mined (mining is block-only). The named-target
    # branch routes it via goto; the generic in-reach branch (if it fires) may
    # ``use`` it, but never ``mine`` it.
    conn = _FakeConnector(
        affordances=[
            {"kind": "entity", "target": "cow", "verb": "use", "distance": 1.0}
        ]
    )
    conn._last_reflex_interaction = "entity:cow"
    goal = {
        "id": 13,
        "description": "find a cow",
        "target_kind": "entity",
        "target_name": "cow",
    }
    result = await conn.motor_step(goal)
    assert result["action"] == "goto"
    assert ("mine", {"target": "cow"}) not in conn.calls


# ----------------------------------------------------------------------
# Phase 3: structured inventory aggregation (_inventory_counts)
# ----------------------------------------------------------------------


def test_inventory_counts_aggregates_duplicate_stacks() -> None:
    # The raw inventory is a flat list of stacks; the same id can appear in
    # several stacks. ``_inventory_counts`` sums them into an id->total map so
    # cognition can judge "how many oak_log do I still need" without rescanning.
    inventory = [
        {"name": "oak_log", "count": 12},
        {"name": "oak_log", "count": 5},
        {"name": "cobblestone", "count": 64},
    ]
    assert MinecraftConnector._inventory_counts(inventory) == {
        "oak_log": 17,
        "cobblestone": 64,
    }


def test_inventory_counts_is_fail_safe_on_bad_entries() -> None:
    inventory = [
        {"name": "stone", "count": 3},
        "not-a-dict",
        {"count": 9},  # missing name
        {"name": "dirt"},  # missing count → treated as 0
        {"name": "iron_ore", "count": "not-a-number"},  # unparsable → skipped
    ]
    assert MinecraftConnector._inventory_counts(inventory) == {  # type: ignore[list-item]
        "stone": 3,
        "dirt": 0,
    }


def test_inventory_counts_empty() -> None:
    assert MinecraftConnector._inventory_counts([]) == {}
