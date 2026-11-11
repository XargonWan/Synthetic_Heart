"""Minecraft's ``explore``: the core's walk (plugins/rift_vessel/exploration.py) on top of the bridge's ``goto``.

No bridge, no database: ``_post`` is a fake world that answers ``status`` (position, mobs, blocks), ``goto`` (moves the
bot, or finds no way) and ``unfollow`` (stops the pathfinder). Time is a number the fake moves, so a walk of a minute
takes no time at all.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from plugins.rift_vessel.exploration import ExplorerMixin
from plugins.rift_vessel.minecraft.minecraft import MinecraftConnector


class FakeWorld:
    """The bridge, from the connector's side."""

    def __init__(self, clock: "Clock") -> None:
        self.clock = clock
        self.pos = {"x": 0.0, "y": 64.0, "z": 0.0}
        self.mobs: list[dict[str, Any]] = []
        self.blocks: list[dict[str, Any]] = [{"name": "oak_log"}, {"name": "crafting_table"}]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.no_way_to: set[tuple[int, int]] = set()
        self.hostile_arrives_after: int | None = None
        self.gotos = 0

    async def post(self, path: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        action, body = payload["action"], payload.get("payload") or {}
        self.calls.append((action, body))
        if action == "status":
            return {"ok": True, "data": {"position": dict(self.pos), "entities": list(self.mobs), "blocks": list(self.blocks),
                                          "health": 20.0, "inventory": []}}
        if action == "goto":
            self.gotos += 1
            x, z = float(body["x"]), float(body["z"])
            if (round(x / 4), round(z / 4)) in self.no_way_to:
                return {"ok": False, "detail": "no path", "data": {}}
            self.clock.t += math.hypot(x - self.pos["x"], z - self.pos["z"]) / 4.3
            self.pos.update({"x": x, "z": z})
            if self.hostile_arrives_after is not None and self.gotos >= self.hostile_arrives_after:
                self.mobs.append({"name": "zombie", "hostile": True, "position": {"x": x + 3, "y": 64.0, "z": z}, "distance": 3.0})
            return {"ok": True, "detail": "arrived", "data": {"position": dict(self.pos)}}
        if action == "unfollow":
            return {"ok": True, "detail": "stopped following", "data": {}}
        return {"ok": True, "data": {}}


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make(world_clock: Clock | None = None) -> tuple[MinecraftConnector, FakeWorld, list[Any]]:
    clock = world_clock or Clock()
    conn = MinecraftConnector()
    world = FakeWorld(clock)
    events: list[Any] = []
    conn._connected = True
    conn._post = world.post  # type: ignore[method-assign]
    conn._on_event = events.append
    conn.explore_clock = clock  # type: ignore[assignment]

    async def sleep(seconds: float) -> None:
        clock.t += seconds
        await asyncio.sleep(0)

    conn.explore_sleep = sleep  # type: ignore[assignment]
    conn._explore_state()["policy"].rng.seed(5)
    return conn, world, events


async def finish(conn: MinecraftConnector) -> None:
    await asyncio.wait_for(conn._explore_state()["task"], 10)


async def test_it_is_a_connector_that_explores_and_the_verb_is_offered_in_the_same_words() -> None:
    conn, _, _ = make()
    assert isinstance(conn, ExplorerMixin) and conn.explores is True
    actions = conn.get_world_actions()
    assert "explore" in actions and "wander" in actions, "wander stays: the motor tick and the model still use it"
    assert "NOT asked anything meanwhile" in actions["explore"]["description"]
    assert "hostile" in actions["explore"]["description"]
    assert actions["explore"]["required_fields"] == [] and actions["explore"]["optional_fields"] == []


async def test_a_walk_is_a_chain_of_gotos_with_no_y_and_a_budget_in_seconds() -> None:
    conn, world, events = make()
    conn.set_explore_budget(60)
    res = await conn.act("explore", {})
    assert res.ok is True and "going for a walk of about 60 seconds" in (res.detail or "")
    assert conn.idle_busy() is True
    await finish(conn)
    gotos = [b for a, b in world.calls if a == "goto"]
    assert len(gotos) >= 3, "a minute is several legs, not one"
    assert all("y" not in g and g["range"] == 3 and g["timeout_ms"] >= 5000 for g in gotos), gotos[0]
    legs = [math.hypot(b["x"], b["z"]) for b in gotos[:1]]
    assert legs[0] >= 14 - 3, "a leg is a trek of tens of blocks, not a step round the spawn"
    assert len(events) == 1 and events[0].event_type == "explore_done"
    d = events[0].data
    assert d["stopped_because"] in ("budget", "nothing_left") and d["distance"] > 20
    assert {x["name"] for x in d["discovered"]} >= {"oak_log", "crafting_table"}, "what it came across, named once"


async def test_it_keeps_going_the_way_it_was_going() -> None:
    """A trek, not a circle: successive legs mostly continue along the heading of the last move. Counted over twelve fixed
    seeds (a single walk is a draw): 82% of 174 legs go on, against 67% with the habit switched off."""
    forward = total = 0
    for seed in range(1, 13):
        conn, world, _ = make()
        conn._explore_state()["policy"].rng.seed(seed)
        conn.set_explore_budget(120)
        await conn.act("explore", {})
        await finish(conn)
        pts = [(0.0, 0.0)] + [(b["x"], b["z"]) for a, b in world.calls if a == "goto"]
        for i in range(2, len(pts)):
            a1 = math.atan2(pts[i - 1][1] - pts[i - 2][1], pts[i - 1][0] - pts[i - 2][0])
            a2 = math.atan2(pts[i][1] - pts[i - 1][1], pts[i][0] - pts[i - 1][0])
            total += 1
            forward += 1 if abs((a2 - a1 + math.pi) % math.tau - math.pi) < math.pi / 2 else 0
    assert total > 120
    assert forward / total >= 0.75, f"{forward} of {total} legs go on the way it was going ({forward / total:.0%})"


async def test_a_hostile_mob_ends_the_walk_and_places_near_it_are_not_chosen() -> None:
    conn, world, events = make()
    world.hostile_arrives_after = 2
    conn.set_explore_budget(120)
    await conn.act("explore", {})
    await finish(conn)
    assert events[0].data["stopped_because"] == "threat", events[0].data
    # and before it appeared, a mob standing off to one side keeps the choices away from it
    conn2, world2, events2 = make()
    world2.mobs = [{"name": "zombie", "hostile": True, "position": {"x": 20.0, "y": 64.0, "z": 0.0}, "distance": 20.0}]
    conn2.set_explore_budget(60)
    await conn2.act("explore", {})
    await finish(conn2)
    for a, b in world2.calls:
        if a == "goto":
            assert math.hypot(b["x"] - 20.0, b["z"]) > conn2.EXPLORE_HOSTILE_RADIUS, f"walked to {b} next to a hostile mob"


async def test_players_and_animals_are_not_a_danger() -> None:
    conn, world, events = make()
    world.mobs = [{"name": "Remuraine", "kind": "player", "hostile": False, "position": {"x": 5.0, "y": 64.0, "z": 0.0}, "distance": 5.0},
                  {"name": "cow", "hostile": False, "position": {"x": 6.0, "y": 64.0, "z": 3.0}, "distance": 6.0}]
    conn.set_explore_budget(30)
    await conn.act("explore", {})
    await finish(conn)
    assert events[0].data["stopped_because"] != "threat"
    assert {x["name"] for x in events[0].data["discovered"]} >= {"Remuraine", "cow"}


async def test_a_place_with_no_way_is_marked_and_not_asked_for_again() -> None:
    conn, world, events = make()
    world.no_way_to = {(round(x / 4), round(z / 4)) for x in range(-40, 41, 4) for z in range(-40, 41, 4)}
    conn.set_explore_budget(60)
    await conn.act("explore", {})
    await finish(conn)
    assert events[0].data["stopped_because"] == "blocked" and world.gotos == 3, "three failures in a row and it stops"
    assert conn.explore_memory().failed, "and it remembers where"


async def test_the_motor_tick_stands_back_while_the_walk_goes_on() -> None:
    """The 3 s motor tick re-issues a goto every few seconds and would replace every leg: it yields (as for a collect)."""
    conn, world, _ = make()
    conn.set_explore_budget(60)
    assert conn.deliberate_action_in_flight is False
    await conn.act("explore", {})
    assert conn.deliberate_action_in_flight is True

    async def state():
        return None

    # the real motor_step reaches the check after the survival guard: give it a quiet world
    from plugins.rift_vessel.vessel_base import WorldState

    async def quiet_state():
        return WorldState(environment="minecraft", health=20.0, position={"x": 0, "y": 64, "z": 0}, possible_actions=[],
                          flags={}, extra={"entities": [], "blocks": [], "affordances": []})

    async def no_guard(_state):
        return None

    conn.get_world_state = quiet_state  # type: ignore[method-assign]
    conn._run_survival_guard = no_guard  # type: ignore[method-assign]
    stepped = await conn.motor_step({"description": "x", "destination": {"x": 100, "z": 100}})
    assert stepped == {"acted": False, "reason": "deliberate_action_in_flight"}
    await finish(conn)
    assert conn.deliberate_action_in_flight is False


async def test_someone_else_speaking_stops_the_walk_but_the_bots_own_words_do_not() -> None:
    conn, world, events = make()
    conn.get_in_world_name = lambda: "Synth"  # type: ignore[method-assign]
    conn.set_explore_budget(200)
    await conn.act("explore", {})
    await asyncio.sleep(0)
    run = conn._explore_state()["run"]
    await conn._dispatch_event({"event_type": "chat", "actor": "Synth", "summary": "Synth: hi", "data": {}})
    assert run.cancelled is None, "its own echo is not a person"
    await conn._dispatch_event({"event_type": "chat", "actor": "Remuraine", "summary": "Remuraine: ehi", "data": {}})
    assert run.cancelled == "a player spoke" and run.silent is True
    await finish(conn)
    assert [e.event_type for e in events if e.event_type == "explore_done"] == [], "the chat is the next prompt, not the walk's end"
    assert any(a == "unfollow" for a, _ in world.calls), "the pathfinder was told to let go"


async def test_the_walk_reads_the_bridge_without_eating_the_damage_delta() -> None:
    """get_world_state advances the 'took damage' baseline; the damage appraisal waits for that delta."""
    conn, world, _ = make()

    async def forbidden():
        raise AssertionError("explore must not read get_world_state")

    conn.get_world_state = forbidden  # type: ignore[method-assign]
    conn._last_health = 20.0
    conn.set_explore_budget(30)
    await conn.act("explore", {})
    await finish(conn)
    assert conn._last_health == 20.0
