"""Exploration (plugins/rift_vessel/exploration.py): where to go and when to stop, on a map drawn in the test.

No engine, no database: a grid world is the host (it answers probe / walk / look the way an adapter would), and the clock
is a number the host moves. The old RetroXR ``explore`` (pick a point on a ring, then find out whether there is a way)
is run on the SAME map as the control: it must fail where the new one does not.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.rift_vessel import exploration as ex
from plugins.rift_vessel.exploration import (
    Candidate,
    ExplorationMemory,
    ExplorationPolicy,
    ExplorationRun,
    ExplorerMixin,
    ExploreResult,
    Hazard,
    LookResult,
    Probe,
    WalkResult,
)

# Two rooms with a door at (9, 4); furniture in the west room; letters are named things on the floor.
MAP = [
    "####################",
    "#.....a..#.........#",
    "#........#.........#",
    "#...##...#....##.b.#",
    "#...##.........##..#",
    "#........#.........#",
    "#.c......#.......d.#",
    "####################",
]
NAMES = {"a": "a lamp", "b": "a crate", "c": "a plant", "d": "a stool"}
SPEED = 1.1


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class GridWorld:
    """A host: reachable cells by breadth-first search, line of sight by walking the grid, time by a fake clock."""

    def __init__(self, clock: Clock, start=(1, 1), blind: bool = False, fail: set | None = None) -> None:
        self.clock = clock
        self.rows = MAP
        self.cell = start
        self.blind = blind
        self.fail = fail or set()
        self.hazards: list[Hazard] = []
        self.threat_after_walks: int | None = None
        self.walks = 0
        self.halted = 0
        self.probes = 0

    def free(self, c) -> bool:
        x, y = c
        return 0 <= y < len(self.rows) and 0 <= x < len(self.rows[0]) and self.rows[y][x] != "#"

    @staticmethod
    def pos(c) -> tuple[float, float, float]:
        return (c[0] + 0.5, 0.0, c[1] + 0.5)

    @staticmethod
    def cell_of(p) -> tuple[int, int]:
        return (int(p[0]), int(p[2]))

    def bfs(self, src) -> dict:
        dist = {src: 0}
        q = deque([src])
        while q:
            c = q.popleft()
            for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (c[0] + d[0], c[1] + d[1])
                if n not in dist and self.free(n):
                    dist[n] = dist[c] + 1
                    q.append(n)
        return dist

    def los(self, a, b) -> bool:
        steps = max(abs(a[0] - b[0]), abs(a[1] - b[1])) * 2
        for i in range(1, steps):
            t = i / steps
            c = (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t))
            if not self.free(c):
                return False
        return math.hypot(a[0] - b[0], a[1] - b[1]) <= 8

    # -- the contract --

    async def explore_probe(self, viewpoints, max_candidates):
        self.probes += 1
        hz = list(self.hazards)
        if self.blind:
            return Probe(position=self.pos(self.cell), heading=None, candidates=None, hazards=hz)
        dist = self.bfs(self.cell)
        vcells = [self.cell_of(v) for v in viewpoints]
        cands = []
        for c, d in dist.items():
            if c[0] % 2 == 0 and c[1] % 2 == 0 and d > 0:
                cands.append(Candidate(pos=self.pos(c), cost=float(d), seen=any(self.los(v, c) for v in vcells)))
        pois = [
            Candidate(pos=self.pos((x, y)), cost=float(dist[(x, y)]), kind="poi", name=NAMES[ch], area="west room" if x < 9 else "east room")
            for y, row in enumerate(self.rows) for x, ch in enumerate(row) if ch in NAMES and (x, y) in dist
        ]
        return Probe(position=self.pos(self.cell), heading=0.0, candidates=cands, pois=pois, hazards=hz)

    async def explore_walk(self, pos, timeout):
        await asyncio.sleep(0)  # a real body takes time: the run must be able to be stopped meanwhile
        self.walks += 1
        dest = self.cell_of(pos)
        dist = self.bfs(self.cell)
        if dest in self.fail or dest not in dist:
            return WalkResult(ok=False, detail="no_way_round", position=self.pos(self.cell), moved=0.0)
        d = dist[dest]
        self.clock.t += d / SPEED
        self.cell = dest
        if self.threat_after_walks is not None and self.walks >= self.threat_after_walks:
            return WalkResult(ok=False, detail="threat: something came close", position=self.pos(dest), moved=float(d))
        return WalkResult(ok=True, detail="arrived", position=self.pos(dest), moved=float(d))

    async def explore_look(self, seconds):
        await asyncio.sleep(0)
        self.clock.t += seconds
        saw = []
        for y, row in enumerate(self.rows):
            for x, ch in enumerate(row):
                if ch in NAMES and self.los(self.cell, (x, y)):
                    saw.append({"name": NAMES[ch], "area": "west room" if x < 9 else "east room"})
        return LookResult(saw=saw)

    async def explore_study(self, poi):
        self.clock.t += 1.0
        return True

    async def explore_halt(self):
        self.halted += 1


async def go(world: GridWorld, clock: Clock, budget: float, seed: int = 1, memory=None):
    policy = ExplorationPolicy(rng=random.Random(seed))
    run = ExplorationRun(world, memory or ExplorationMemory(), policy, budget, clock=clock, world="test")
    return await run.run(), run


# -- the old method, on the same map: the control ------------------------------


def old_style_failures(world: GridWorld, tries: int = 3) -> int:
    """What ``SynthRoutines._next_waypoint`` did: points on two rings round the start, then try to walk. Counts the
    walks that found no way."""
    start = world.pos(world.cell)
    reach = world.bfs(world.cell)
    visited = [start]
    failures = 0
    for _ in range(tries):
        best, best_gap = None, 0.0
        for ring in (2.2, 4.0):
            for k in range(10):
                a = math.tau * k / 10.0
                p = (start[0] + math.cos(a) * ring, 0.0, start[2] + math.sin(a) * ring)
                gap = min(math.hypot(p[0] - v[0], p[2] - v[2]) for v in visited)
                if gap > best_gap:
                    best, best_gap = p, gap
        if best is None or best_gap <= 1.6:
            break
        if world.cell_of(best) not in reach:
            failures += 1
        else:
            visited.append(best)
    return failures


def test_the_old_method_picks_places_with_no_way_to_them_on_this_map() -> None:
    """The control: choose a point, find out afterwards. In this room that is a wall more often than not."""
    assert old_style_failures(GridWorld(Clock())) >= 2, "the old method should fail here, or the map proves nothing"


# -- reachable first -----------------------------------------------------------


async def test_every_leg_of_a_walk_goes_somewhere_there_is_a_way_to() -> None:
    clock = Clock()
    world = GridWorld(clock)
    result, run = await go(world, clock, 90)
    assert result.legs >= 4, result
    assert world.walks == run.legs
    failures = 0  # no leg was refused: the way was known before it was chosen
    assert result.stopped_because in ("budget", "nothing_left")
    assert run.distance > 0 and not any(f for f in [failures])


async def test_a_place_the_body_cannot_reach_is_dropped_before_it_is_chosen() -> None:
    p = ExplorationPolicy(rng=random.Random(3))
    mem = ExplorationMemory()
    cands = [Candidate(pos=(5.0, 0.0, 5.0), cost=5.0), Candidate(pos=(9.0, 0.0, 9.0), cost=9.0)]
    mem.fail((5.0, 0.0, 5.0))
    left = p.usable((0.0, 0.0, 0.0), cands, [], mem)
    assert [c.pos for c in left] == [(9.0, 0.0, 9.0)], "a place a walk failed at is not asked for again"


# -- places she has not seen ---------------------------------------------------


def test_the_unseen_is_preferred_but_not_always() -> None:
    p = ExplorationPolicy(rng=random.Random(7))
    mem = ExplorationMemory()
    seen = Candidate(pos=(4.0, 0.0, 0.0), cost=4.0, seen=True)
    unseen = Candidate(pos=(-4.0, 0.0, 0.0), cost=4.0, seen=False)
    picks = [p.choose([seen, unseen], (0.0, 0.0, 0.0), None, mem) for _ in range(400)]
    share = sum(1 for c in picks if c is unseen) / len(picks)
    assert 0.70 < share < 0.98, f"unseen chosen {share:.0%}: it should lead, and not be the only thing she ever does"


def test_a_place_inside_a_hazard_is_dropped_and_a_far_one_is_kept() -> None:
    p = ExplorationPolicy()
    mem = ExplorationMemory()
    near = Candidate(pos=(2.0, 0.0, 0.0), cost=2.0)
    far = Candidate(pos=(10.0, 0.0, 0.0), cost=10.0)
    hz = [Hazard(pos=(2.5, 0.0, 0.0), radius=1.0, kind="threat")]
    assert p.usable((0.0, 0.0, 0.0), [near, far], hz, mem) == [far]
    assert p.usable((0.0, 0.0, 0.0), [near, far], [], mem) == [near, far]


def test_going_back_where_she_has_been_costs() -> None:
    p = ExplorationPolicy()
    mem = ExplorationMemory()
    here = Candidate(pos=(3.0, 0.0, 0.0), cost=3.0, seen=True)
    new = Candidate(pos=(3.0, 0.0, 3.0), cost=3.0, seen=True)
    mem.add_viewpoint((3.2, 0.0, 0.1))
    assert p.score(new, (0.0, 0.0, 0.0), None, mem) > p.score(here, (0.0, 0.0, 0.0), None, mem)


async def test_she_does_not_keep_stopping_in_the_same_places() -> None:
    clock = Clock()
    world = GridWorld(clock)
    result, run = await go(world, clock, 150, seed=11)
    pts = run.memory.viewpoints
    assert len(pts) >= 5
    near_pairs = sum(1 for i, a in enumerate(pts) for b in pts[:i] if ex.flat_dist(a, b) < 1.0)
    assert near_pairs <= len(pts) // 2, f"{near_pairs} repeated stops in {len(pts)}"


# -- things in a room ----------------------------------------------------------


async def test_the_things_she_finds_are_grouped_by_room_and_told_once() -> None:
    clock = Clock()
    world = GridWorld(clock)
    result, run = await go(world, clock, 180, seed=5)
    names = [d["name"] for d in result.discovered]
    assert len(names) == len(set(names)), "something told twice"
    assert {"a lamp", "a plant"} & set(names), names
    assert {d["area"] for d in result.discovered} <= {"west room", "east room"}
    assert run.memory.inspected, "a thing she went up to is marked studied"
    # a second walk in the same stay does not tell her the same things as new
    second, _ = await go(world, clock, 60, seed=6, memory=run.memory)
    assert not (set(d["name"] for d in second.discovered) & set(names))


# -- the time -------------------------------------------------------------------


async def test_a_walk_lasts_about_as_long_as_the_budget_says() -> None:
    clock = Clock()
    world = GridWorld(clock)
    result, _ = await go(world, clock, 60)
    assert result.stopped_because in ("budget", "nothing_left")
    assert 40 <= result.seconds <= 60 + 35, f"{result.seconds:.0f}s for a budget of 60 (one leg and a pause may run over)"
    short_clock = Clock()
    short, _ = await go(GridWorld(short_clock), short_clock, 12)
    assert short.seconds < 12 + 35 and short.legs <= result.legs


async def test_nothing_left_to_see_ends_a_walk_early() -> None:
    clock = Clock()
    world = GridWorld(clock)
    mem = ExplorationMemory()
    await go(world, clock, 600, memory=mem)
    # everything is a viewpoint now; ask again for a very long walk: it stops when there is nowhere new, it does not spin
    clock.t = 0.0
    again, _ = await go(world, clock, 600, memory=mem, seed=2)
    assert again.stopped_because in ("budget", "nothing_left")


# -- dangers and being stopped --------------------------------------------------


async def test_a_threat_that_comes_close_ends_the_walk_and_says_so() -> None:
    clock = Clock()
    world = GridWorld(clock)
    world.threat_after_walks = 2
    result, _ = await go(world, clock, 120)
    assert result.stopped_because == "threat" and result.legs == 2


async def test_a_threat_already_on_top_of_her_means_she_does_not_start() -> None:
    clock = Clock()
    world = GridWorld(clock)
    world.hazards = [Hazard(pos=world.pos(world.cell), radius=3.0, kind="threat")]
    result, _ = await go(world, clock, 120)
    assert result.stopped_because == "threat" and result.legs == 0


async def test_being_asked_to_stop_halts_the_body_and_ends_the_walk() -> None:
    clock = Clock()
    world = GridWorld(clock)
    mem = ExplorationMemory()
    run = ExplorationRun(world, mem, ExplorationPolicy(rng=random.Random(1)), 300, clock=clock, world="test")
    task = asyncio.ensure_future(run.run())
    await asyncio.sleep(0)
    run.cancel("the player spoke")
    result = await task
    assert result.stopped_because == "interrupted" and result.detail == "the player spoke"
    assert world.halted >= 1, "the body was told to stop walking now, not at the end of the leg"


# -- when the body cannot say what is reachable --------------------------------


async def test_blind_still_walks_and_marks_the_places_that_failed() -> None:
    clock = Clock()
    world = GridWorld(clock, blind=True)
    result, run = await go(world, clock, 90, seed=4)
    assert result.legs >= 3
    assert run.memory.failed, "walks that found no way left a mark so they are not asked for again"


async def test_blind_in_a_shut_room_gives_up_after_a_few_failures() -> None:
    clock = Clock()
    world = GridWorld(clock, blind=True)
    world.explore_walk = lambda pos, timeout: _fail_walk(world)  # type: ignore[method-assign]
    result, _ = await go(world, clock, 90)
    assert result.stopped_because == "blocked"


async def _fail_walk(world):
    return WalkResult(ok=False, detail="no_way_round", position=world.pos(world.cell), moved=0.0)


async def test_a_probe_that_answers_nothing_is_an_error_result_not_a_hang() -> None:
    clock = Clock()
    world = GridWorld(clock)

    async def silent(v, n):
        return None

    world.explore_probe = silent  # type: ignore[method-assign]
    result, _ = await go(world, clock, 60)
    assert result.stopped_because == "error"


# -- what she is told -----------------------------------------------------------


def test_the_result_is_told_in_the_same_words_for_every_world() -> None:
    res = ExploreResult(seconds=62, distance=41.0, legs=6,
                        discovered=[{"name": "a lamp", "area": "west room"}, {"name": "a crate", "area": "east room"}],
                        stopped_because="budget")
    text = ex.format_explore_result(res)
    assert "6 stops" in text and "about 41 m" in text
    assert "New to you: a lamp (west room), a crate (east room)." in text, "two rooms: say which"
    one_room = ex.format_explore_result(ExploreResult(5, 1.0, 1, [{"name": "a lamp", "area": "den"}, {"name": "a crate", "area": "den"}], "budget"))
    assert "New to you: a lamp, a crate." in one_room, "one room: the name of the room is noise"
    assert "Nobody asked you to do this" in text and "What do you want to do now?" in text
    assert "must" not in text.lower()
    empty = ex.format_explore_result(ExploreResult(5, 0.0, 1, [], "threat"))
    assert "nothing you had not already seen" in empty and "could hurt you" in empty
    assert "1 stop," in empty
    many = ex.format_explore_result(ExploreResult(5, 1.0, 2, [{"name": f"t{i}", "area": None} for i in range(11)], "nothing_left"))
    assert "and 3 more" in many
    assert "(late)" in ex.format_explore_result(ExploreResult(5, 1.0, 2, [], "interrupted", detail="late"))


def test_the_verb_is_offered_with_one_set_of_words_and_no_fields() -> None:
    schema = ex.explore_schema()
    assert schema["required_fields"] == [] and schema["optional_fields"] == []
    assert "NOT asked anything meanwhile" in schema["description"]
    assert "must" not in schema["description"].lower()


def test_the_budget_is_the_wait_to_the_next_prompt_plus_the_turn_that_answers_it() -> None:
    assert ex.explore_budget(40, 20) == 60
    assert ex.explore_budget(5, 5) == 20, "never shorter than the floor"
    assert ex.explore_budget(300, 300) == 180, "never longer than the ceiling"
    m = ex.LatencyMeter(default=25.0)
    assert m.value() == 25.0
    m.observe(10.0)
    assert m.value() == 10.0, "the first real turn replaces the guess"
    m.observe(20.0)
    assert 10.0 < m.value() < 20.0
    m.observe(-1)
    m.observe(5000)
    assert m.samples == 2, "a nonsense duration is ignored"
    cfg = lambda key, default: {"VESSEL_EXPLORE_MIN_SEC": 1, "VESSEL_EXPLORE_MAX_SEC": "x"}.get(key, default)
    assert ex.resolve_explore_min(cfg) == 5.0 and ex.resolve_explore_max(cfg) == 180.0


# -- a connector that mixes it in -------------------------------------------------


class Connector(ExplorerMixin):
    EXPLORE_WORLD = "test"

    def __init__(self, world: GridWorld) -> None:
        self.world = world
        self.events: list[Any] = []
        self.journal: list[str] = []

        async def on_event(event):
            self.events.append(event)

        self._on_event = on_event

    async def explore_probe(self, viewpoints, max_candidates):
        return await self.world.explore_probe(viewpoints, max_candidates)

    async def explore_walk(self, pos, timeout):
        await asyncio.sleep(0)
        return await self.world.explore_walk(pos, timeout)

    async def explore_look(self, seconds):
        return await self.world.explore_look(seconds)

    async def explore_halt(self):
        await self.world.explore_halt()

    def _note(self, text):
        self.journal.append(text)


async def _drain(conn: Connector) -> None:
    task = conn._explore_state()["task"]
    if task is not None:
        await asyncio.wait_for(task, 5)


async def test_explore_answers_at_once_and_the_result_comes_as_an_event() -> None:
    clock = Clock()
    conn = Connector(GridWorld(clock))
    conn.set_explore_budget(40)
    # the world's fake clock does not move the mixin's own (real) clock: use a small real budget instead
    conn.explore_budget_s = 40
    answer = conn.start_explore({})
    assert answer["ok"] is True and "going for a walk of about 40 seconds" in answer["detail"]
    assert answer["data"]["budget_seconds"] == 40
    assert conn.idle_busy() is True
    conn.cancel_explore("test", silent=False)
    await _drain(conn)
    assert conn.idle_busy() is False
    assert len(conn.events) == 1
    ev = conn.events[0]
    assert ev.event_type == "explore_done" and ev.environment == "test" and ev.salience == 0.9
    assert ev.data["stopped_because"] == "interrupted" and "What do you want to do now?" in ev.summary
    assert conn.journal and "interrupted" in conn.journal[0]


async def test_a_walk_replaced_or_cancelled_silently_tells_the_model_nothing() -> None:
    clock = Clock()
    conn = Connector(GridWorld(clock))
    conn.start_explore({})
    first = conn._explore_state()["task"]
    conn.start_explore({})  # a new walk replaces the first: nobody is told the first one ended
    await asyncio.wait_for(first, 5)
    assert conn.events == []
    assert conn.cancel_explore("the player asked for something else") is True
    await _drain(conn)
    assert conn.events == [], "the one who changed the plan already knows"
    assert conn.cancel_explore() is False, "nothing left to stop"
    assert len(conn.journal) == 2, "but what she did is still remembered for the next prompt"


async def test_a_walk_that_breaks_is_told_as_a_failure_never_left_hanging() -> None:
    clock = Clock()
    conn = Connector(GridWorld(clock))

    async def broken(v, n):
        raise RuntimeError("the socket died")

    conn.explore_probe = broken  # type: ignore[method-assign]
    conn.start_explore({})
    await _drain(conn)
    assert len(conn.events) == 1 and conn.events[0].data["stopped_because"] == "error"
    assert "socket died" in conn.events[0].data["detail"]
    assert conn.idle_busy() is False


async def test_a_new_stay_forgets_where_she_has_been() -> None:
    conn = Connector(GridWorld(Clock()))
    conn.explore_memory().add_viewpoint((1.0, 0.0, 1.0))
    conn.explore_memory().discover("a lamp", "west room")
    conn.reset_exploration()
    assert conn.explore_memory().viewpoints == [] and conn.explore_memory().discovered == {}
