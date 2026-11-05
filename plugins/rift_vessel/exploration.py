"""Exploration -- one way to walk round a world with no place in mind, for every Rift Vessel adapter.

The core decides WHERE to go and WHEN to stop; the adapter (a game) knows only what its engine knows: where the body
is, what can be walked to, how to walk, how to look. That is the whole split. Pathfinding proper (A* over the physics
world in Godot, mineflayer-pathfinder, a Skyrim navmesh) stays the adapter's.

What "explore" means here (RetroXR first, Minecraft next, Skyrim's rooms of objects later -- the same contract):

* **Reachable first, then the place.** The adapter returns candidates it already knows a way to, with the cost of that
  way (``Probe.candidates``). Choosing a point and finding out afterwards that there is no road to it is how the old
  RetroXR ``explore`` failed two walks in three (``no_way_round`` in the 2026-11-05 log).
* **Places she has not seen.** The core sends the points she has looked from (``viewpoints``); the adapter marks a
  candidate ``seen`` when one of them has a line of sight to it. A place behind a corner, behind a piece of furniture or
  in another part of the room scores higher than one in plain view.
* **Things in a room.** Points of interest (``Probe.pois``) she has not studied are candidates too, grouped by ``area``
  (a room): this is also how a Skyrim body would list the objects of a house.
* **Conscientious but casual.** Score = novelty + interest - the cost of the way + a little persistence of heading - a
  penalty for going back where she has been; the choice is a softmax with a temperature, and now and then one from
  further down the list. The generator is injectable, so a test sets the seed.
* **Careful.** The adapter lists ``hazards`` (the player, something that moves, a hostile mob). A place inside one is
  dropped; one that comes close while she walks stops the leg and the result says ``threat``.
* **It lasts until the next prompt.** A run has a budget in seconds, set by the interface (the interval to the next
  idle beat plus how long a turn takes), and chains legs -- walk, look for a few seconds, walk -- until it is spent,
  nothing is left to see, she is interrupted or the way is shut. The time is filled with stops, not with kilometres.
* **Told afterwards.** Nothing is asked meanwhile. The result is ONE event (``explore_done``) with data and a prose
  summary worded here, the same for every world.

Never keyword logic: everything here is geometry, counts and flags. Never raises into the connector: a run that fails
ends with a result of its own.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Dict, List, Sequence

from core.logging_utils import log_debug, log_info, log_warning

LOG_PREFIX = "[explore]"

#: A position: (x, y, z). Planar distances use x and z.
Vec = tuple[float, float, float]

STOP_REASONS = ("budget", "nothing_left", "blocked", "threat", "interrupted", "error")


# ---------------------------------------------------------------------------
# Small geometry helpers
# ---------------------------------------------------------------------------


def as_vec(value: Any) -> Vec | None:
    """A ``{x, y, z}`` dict, a 2/3-sequence or a ``Vec`` as a ``Vec``; ``None`` when it is not a position."""
    try:
        if isinstance(value, dict):
            return (float(value["x"]), float(value.get("y", 0.0) or 0.0), float(value["z"]))
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return (float(value[0]), float(value[1]), float(value[2]))
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return (float(value[0]), 0.0, float(value[1]))
    except (KeyError, TypeError, ValueError):
        return None
    return None


def flat_dist(a: Vec, b: Vec) -> float:
    return math.hypot(a[0] - b[0], a[2] - b[2])


def bearing(a: Vec, b: Vec) -> float:
    """Heading from ``a`` to ``b`` in radians (atan2 of dz, dx)."""
    return math.atan2(b[2] - a[2], b[0] - a[0])


# ---------------------------------------------------------------------------
# What an adapter answers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A place the body can walk to. ``cost`` is the length of the way (metres), ``seen`` whether she has had a line of
    sight to it from where she has looked. ``kind`` is ``frontier`` or ``poi`` (a thing to study; ``name`` and ``area``)."""

    pos: Vec
    cost: float = 0.0
    seen: bool = False
    kind: str = "frontier"
    name: str | None = None
    area: str | None = None


@dataclass(frozen=True)
class Hazard:
    """Something to keep clear of. ``kind`` ``threat`` also stops a leg when she is inside its radius."""

    pos: Vec
    radius: float
    kind: str = "obstacle"


@dataclass
class Probe:
    """What the adapter can tell about where she is. ``candidates`` ``None`` means "I cannot enumerate what is reachable":
    the core then makes its own (blind), and a walk that fails marks the place."""

    position: Vec
    heading: float | None = None
    candidates: List[Candidate] | None = None
    pois: List[Candidate] = field(default_factory=list)
    hazards: List[Hazard] = field(default_factory=list)


@dataclass
class WalkResult:
    ok: bool
    detail: str = ""
    position: Vec | None = None
    moved: float = 0.0


@dataclass
class LookResult:
    """Names of what came into view while she looked (``{"name": ..., "area": ...}``)."""

    saw: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Memory of one stay
# ---------------------------------------------------------------------------


class ExplorationMemory:
    """Where she has looked from, what she has studied, what she has come across, where a walk failed.

    Kept by the connector for the whole stay (several runs share it), so a second ``explore`` goes elsewhere.
    """

    def __init__(self, max_viewpoints: int = 80) -> None:
        self.max_viewpoints = max_viewpoints
        self.viewpoints: List[Vec] = []
        self.inspected: set[str] = set()
        self.discovered: Dict[str, str | None] = {}
        self.failed: List[tuple[Vec, float]] = []

    def reset(self) -> None:
        self.viewpoints.clear()
        self.inspected.clear()
        self.discovered.clear()
        self.failed.clear()

    def add_viewpoint(self, pos: Vec | None) -> None:
        if pos is None:
            return
        if self.viewpoints and flat_dist(self.viewpoints[-1], pos) < 0.5:
            return
        self.viewpoints.append(pos)
        del self.viewpoints[: -self.max_viewpoints]

    def recent(self, n: int = 12) -> List[Vec]:
        """Up to ``n`` viewpoints, spread over the whole history (the newest always among them)."""
        pts = self.viewpoints
        if len(pts) <= n:
            return list(pts)
        step = len(pts) / float(n)
        picked = [pts[min(len(pts) - 1, int(i * step))] for i in range(n)]
        picked[-1] = pts[-1]
        return picked

    def visited_near(self, pos: Vec, radius: float) -> bool:
        return any(flat_dist(pos, v) < radius for v in self.viewpoints)

    def fail(self, pos: Vec, radius: float = 1.5) -> None:
        self.failed.append((pos, radius))
        del self.failed[:-40]

    def is_failed(self, pos: Vec) -> bool:
        return any(flat_dist(pos, p) < r for p, r in self.failed)

    def discover(self, name: str, area: str | None = None) -> bool:
        """Note something she came across. True when it is new to her."""
        name = str(name or "").strip()
        if not name or name in self.discovered:
            return False
        self.discovered[name] = area
        return True


# ---------------------------------------------------------------------------
# The policy: conscientious but casual
# ---------------------------------------------------------------------------


@dataclass
class Tuning:
    unseen_bonus: float = 2.0
    poi_bonus: float = 1.6
    cost_weight: float = 0.12  # per metre of way
    persistence: float = 0.5  # cos(angle to the heading) in [-1, 1]
    revisit_radius: float = 2.0
    revisit_penalty: float = 1.5
    temperature: float = 0.6
    wild_chance: float = 0.15  # a choice from further down the list
    top_n: int = 8
    min_leg: float = 1.2  # nearer than this is not a leg (a poi is exempt)
    hazard_margin: float = 0.4
    pause_min: float = 1.5
    pause_max: float = 4.5
    blind_count: int = 12
    blind_near: float = 4.0
    blind_far: float = 12.0
    blind_seen_radius: float = 3.0  # a made-up place this close to somewhere she has been counts as seen


class ExplorationPolicy:
    """Pure: given what the adapter says, which place next. ``rng`` is injectable."""

    def __init__(self, tuning: Tuning | None = None, rng: random.Random | None = None) -> None:
        self.t = tuning or Tuning()
        self.rng = rng or random.Random()

    def usable(self, pos: Vec, cands: Sequence[Candidate], hazards: Sequence[Hazard], memory: ExplorationMemory) -> List[Candidate]:
        out: List[Candidate] = []
        for c in cands:
            if c.kind == "poi" and c.name and c.name in memory.inspected:
                continue
            if c.kind != "poi" and flat_dist(pos, c.pos) < self.t.min_leg:
                continue
            if memory.is_failed(c.pos):
                continue
            if any(flat_dist(c.pos, h.pos) < h.radius + self.t.hazard_margin for h in hazards):
                continue
            out.append(c)
        return out

    def score(self, c: Candidate, pos: Vec, heading: float | None, memory: ExplorationMemory) -> float:
        t = self.t
        s = 0.0
        if not c.seen:
            s += t.unseen_bonus
        if c.kind == "poi":
            s += t.poi_bonus
        s -= t.cost_weight * (c.cost if c.cost > 0 else flat_dist(pos, c.pos))
        if heading is not None:
            s += t.persistence * math.cos(bearing(pos, c.pos) - heading)
        if memory.visited_near(c.pos, t.revisit_radius):
            s -= t.revisit_penalty
        return s

    def choose(self, cands: Sequence[Candidate], pos: Vec, heading: float | None, memory: ExplorationMemory) -> Candidate | None:
        if not cands:
            return None
        ranked = sorted(cands, key=lambda c: self.score(c, pos, heading, memory), reverse=True)
        top = ranked[: self.t.top_n]
        if len(top) >= 4 and self.rng.random() < self.t.wild_chance:
            return self.rng.choice(top[2:])
        scores = [self.score(c, pos, heading, memory) for c in top]
        best = max(scores)
        weights = [math.exp((s - best) / max(self.t.temperature, 1e-6)) for s in scores]
        return self.rng.choices(top, weights=weights, k=1)[0]

    def blind_candidates(self, pos: Vec, heading: float | None, memory: ExplorationMemory) -> List[Candidate]:
        """Places round the body when the adapter cannot say what is reachable. A walk that fails marks the place."""
        t = self.t
        n = max(4, t.blind_count)
        base = self.rng.uniform(0.0, math.tau)
        out: List[Candidate] = []
        for k in range(n):
            a = base + math.tau * k / n + self.rng.uniform(-0.15, 0.15)
            d = self.rng.uniform(t.blind_near, t.blind_far)
            p = (pos[0] + math.cos(a) * d, pos[1], pos[2] + math.sin(a) * d)
            out.append(Candidate(pos=p, cost=d, seen=memory.visited_near(p, t.blind_seen_radius)))
        return out

    def pause_seconds(self, remaining: float) -> float:
        return max(0.0, min(self.rng.uniform(self.t.pause_min, self.t.pause_max), remaining - 1.0))


# ---------------------------------------------------------------------------
# The result and how it is told
# ---------------------------------------------------------------------------


@dataclass
class ExploreResult:
    seconds: float
    distance: float
    legs: int
    discovered: List[Dict[str, Any]]
    stopped_because: str
    coverage: float | None = None
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seconds": round(self.seconds, 1),
            "distance": round(self.distance, 1),
            "legs": self.legs,
            "discovered": list(self.discovered),
            "stopped_because": self.stopped_because,
            "coverage": None if self.coverage is None else round(self.coverage, 2),
            "detail": self.detail,
        }


def _span(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(round(seconds / 5.0)) * 5} seconds"
    return f"{seconds / 60.0:.1f} minutes"


def format_explore_result(result: ExploreResult) -> str:
    """The prose the model is told, the same for every world. States what happened; asks nothing of her."""
    parts = [
        f"You went for a walk with no particular place in mind: {result.legs} "
        f"stop{'s' if result.legs != 1 else ''}, about {result.distance:.0f} m, for {_span(result.seconds)}."
    ]
    names = result.discovered
    if names:
        shown = []
        # An area is worth saying only when there is more than one (a house of rooms); in one room it is just noise.
        many_areas = len({d.get("area") for d in names if d.get("area")}) > 1
        for d in names[:8]:
            area = f" ({d['area']})" if many_areas and d.get("area") else ""
            shown.append(f"{d['name']}{area}")
        more = f", and {len(names) - 8} more" if len(names) > 8 else ""
        parts.append("New to you: " + ", ".join(shown) + more + ".")
    else:
        parts.append("You came across nothing you had not already seen.")
    why = {
        "budget": "You went on for about as long as you had meant to.",
        "nothing_left": "There is nowhere left here you have not been or looked at.",
        "blocked": "You could not get any further: the way was shut.",
        "threat": "You stopped because something that could hurt you came close.",
        "interrupted": f"You were interrupted{(' (' + result.detail + ')') if result.detail else ''}.",
        "error": f"Something went wrong with the walk{(' (' + result.detail + ')') if result.detail else ''}.",
    }.get(result.stopped_because, "")
    if why:
        parts.append(why)
    parts.append("Nobody asked you to do this: it was yours. What do you want to do now?")
    return " ".join(parts)


def explore_schema(extra_description: str = "") -> Dict[str, Any]:
    """The verb as every world offers it, so the model reads the same words everywhere."""
    return {
        "description": (
            "Go for a walk to find out what is around you, with no place in mind. You choose nothing about the "
            "route: your body picks places you have not seen yet or things you have not looked at, goes round what "
            "is in the way, keeps clear of anything dangerous and stops now and then to look. It goes on for about "
            "as long as it takes to think of what to do next (roughly a minute). You are NOT asked anything "
            "meanwhile: you are told afterwards, as a new event, what you came across. Use it when you would like to "
            "see what is here instead of staying where you are." + (" " + extra_description if extra_description else "")
        ),
        "required_fields": [],
        "optional_fields": [],
        "security_level": "low",
    }


# ---------------------------------------------------------------------------
# Budget and pace
# ---------------------------------------------------------------------------


def explore_budget(next_interval_s: float, turn_latency_s: float, lo: float = 20.0, hi: float = 180.0) -> float:
    """Seconds a walk lasts: until the next prompt would have been asked, plus the turn that answers it."""
    return float(max(lo, min(hi, float(next_interval_s) + float(turn_latency_s))))


class LatencyMeter:
    """A running mean of how long a turn nobody asked for takes (seconds), so the walk can match it."""

    def __init__(self, default: float = 25.0, weight: float = 0.3) -> None:
        self._value = float(default)
        self._weight = weight
        self.samples = 0

    def observe(self, seconds: float) -> None:
        if seconds <= 0 or seconds > 900:
            return
        self._value = seconds if self.samples == 0 else (1 - self._weight) * self._value + self._weight * seconds
        self.samples += 1

    def value(self) -> float:
        return self._value


def _resolve(config_get: Any, key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(config_get(key, default))
    except Exception:
        return default
    return max(lo, min(hi, v))


def resolve_explore_min(config_get: Any, default: float = 20.0) -> float:
    return _resolve(config_get, "VESSEL_EXPLORE_MIN_SEC", default, 5.0, 600.0)


def resolve_explore_max(config_get: Any, default: float = 180.0) -> float:
    return _resolve(config_get, "VESSEL_EXPLORE_MAX_SEC", default, 10.0, 1800.0)


# ---------------------------------------------------------------------------
# The run: legs until the time is spent
# ---------------------------------------------------------------------------


class ExplorationRun:
    """One walk. ``host`` provides ``explore_probe / explore_walk / explore_look`` and optionally ``explore_study`` and
    ``explore_halt`` (the adapter's side of the contract). ``clock`` is injectable for tests."""

    #: Legs that make no headway in a row before the way is called shut.
    MAX_FAILS = 3
    #: Less than this left and no new leg is started.
    MIN_LEG_SECONDS = 4.0

    def __init__(
        self,
        host: Any,
        memory: ExplorationMemory,
        policy: ExplorationPolicy,
        budget_s: float,
        clock: Callable[[], float] = time.monotonic,
        world: str = "world",
    ) -> None:
        self.host = host
        self.memory = memory
        self.policy = policy
        self.budget_s = float(budget_s)
        self.clock = clock
        self.world = world
        self.cancelled: str | None = None
        #: Set with ``cancel``: whoever stopped the walk knows, so its end tells the model nothing.
        self.silent = False
        self.legs = 0
        self.distance = 0.0
        self.new_names: List[Dict[str, Any]] = []
        self._coverage: float | None = None

    def cancel(self, why: str = "stopped") -> None:
        """Stop at the next safe point (and ask the body to stop walking now)."""
        if self.cancelled is None:
            self.cancelled = why
        halt = getattr(self.host, "explore_halt", None)
        if callable(halt):
            try:
                res = halt()
                if asyncio.iscoroutine(res):
                    asyncio.ensure_future(res)
            except Exception as exc:  # pragma: no cover - defensive
                log_debug(f"{LOG_PREFIX} halt failed: {exc}")

    def _note_saw(self, look: LookResult | None) -> None:
        if look is None:
            return
        for item in look.saw or []:
            name = str(item.get("name") or "").strip()
            if name and self.memory.discover(name, item.get("area")):
                self.new_names.append({"name": name, "area": item.get("area")})

    def _result(self, started: float, why: str, detail: str = "") -> ExploreResult:
        res = ExploreResult(
            seconds=max(0.0, self.clock() - started),
            distance=self.distance,
            legs=self.legs,
            discovered=list(self.new_names),
            stopped_because=why,
            coverage=self._coverage,
            detail=detail or (self.cancelled if why == "interrupted" else ""),
        )
        log_info(
            f"{LOG_PREFIX} [{self.world}] done: {why} after {res.seconds:.0f}s of {self.budget_s:.0f}s, "
            f"{res.legs} legs, {res.distance:.0f} m, {len(res.discovered)} new"
        )
        return res

    async def run(self) -> ExploreResult:
        started = self.clock()
        fails = 0
        host, memory, policy = self.host, self.memory, self.policy
        try:
            # Know where she is before going anywhere: one short look from the start, the first time only.
            probe = await host.explore_probe(memory.recent(12), 24)
            if probe is None:
                return self._result(started, "error", "the body could not say where it is")
            if not memory.viewpoints:
                memory.add_viewpoint(probe.position)
                self._note_saw(await host.explore_look(min(2.5, max(0.5, self.budget_s / 6.0))))
            while True:
                if self.cancelled:
                    return self._result(started, "interrupted")
                remaining = self.budget_s - (self.clock() - started)
                if remaining < self.MIN_LEG_SECONDS:
                    return self._result(started, "budget")
                probe = await host.explore_probe(memory.recent(12), 24)
                if probe is None:
                    return self._result(started, "error", "the body stopped answering")
                pos = probe.position
                threat = next((h for h in probe.hazards if h.kind == "threat" and flat_dist(pos, h.pos) < h.radius), None)
                if threat is not None:
                    return self._result(started, "threat")
                blind = probe.candidates is None
                cands = list(probe.candidates or []) if not blind else policy.blind_candidates(pos, probe.heading, memory)
                cands += [replace(p, kind="poi") for p in probe.pois if p.name]
                if not blind:
                    seen = [c for c in (probe.candidates or [])]
                    self._coverage = (sum(1 for c in seen if c.seen) / len(seen)) if seen else None
                usable = policy.usable(pos, cands, probe.hazards, memory)
                if not usable:
                    return self._result(started, "blocked" if fails else "nothing_left")
                choice = policy.choose(usable, pos, probe.heading, memory)
                if choice is None:
                    return self._result(started, "nothing_left")
                timeout = max(5.0, min(remaining + 5.0, 45.0))
                walk: WalkResult = await host.explore_walk(choice.pos, timeout)
                self.legs += 1
                if walk.position is not None:
                    memory.add_viewpoint(walk.position)
                if walk.moved > 0:
                    self.distance += walk.moved
                if str(walk.detail).startswith("threat"):
                    return self._result(started, "threat")
                if self.cancelled:
                    return self._result(started, "interrupted")
                if not walk.ok and walk.moved < 0.3:
                    memory.fail(choice.pos)
                    fails += 1
                    log_debug(f"{LOG_PREFIX} [{self.world}] leg failed ({walk.detail}); {fails} in a row")
                    if fails >= self.MAX_FAILS:
                        return self._result(started, "blocked")
                    continue
                fails = 0
                if not walk.ok:
                    memory.fail(choice.pos, 0.8)  # got part of the way: do not ask for the same spot again
                # Arrived (or near enough): study the thing, look round for a few seconds.
                remaining = self.budget_s - (self.clock() - started)
                if choice.kind == "poi" and choice.name:
                    study = getattr(host, "explore_study", None)
                    if callable(study):
                        try:
                            await study(choice)
                        except Exception as exc:  # pragma: no cover - defensive
                            log_debug(f"{LOG_PREFIX} study failed: {exc}")
                    memory.inspected.add(choice.name)
                    if memory.discover(choice.name, choice.area):
                        self.new_names.append({"name": choice.name, "area": choice.area})
                pause = policy.pause_seconds(remaining)
                if pause > 0.2:
                    self._note_saw(await host.explore_look(pause))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} [{self.world}] the run failed: {exc}")
            return self._result(started, "error", str(exc)[:120])


# ---------------------------------------------------------------------------
# What a connector mixes in to offer `explore`
# ---------------------------------------------------------------------------


class ExplorerMixin:
    """For a connector that is an exploration host: ``explore`` starts a run in the background and answers at once; the
    result comes back as an ``explore_done`` event. The connector implements ``explore_probe / explore_walk /
    explore_look`` (and may implement ``explore_study / explore_halt``), and ``_on_event`` is its perception callback.
    """

    explores: bool = True
    explore_budget_s: float = 60.0
    EXPLORE_WORLD: str = "world"
    #: Time, as the walk reads it: replaceable, so a test does not wait for a minute (and its pauses) to pass.
    explore_clock: Callable[[], float] = staticmethod(time.monotonic)
    explore_sleep: Callable[[float], Awaitable[Any]] = staticmethod(asyncio.sleep)
    #: A world whose unit is not a metre in a room (Minecraft: blocks, legs of tens of them) sets its own tuning.
    EXPLORE_TUNING: Tuning | None = None

    # -- state (created lazily so a subclass need not call a base __init__) --

    def _explore_state(self) -> Dict[str, Any]:
        st = self.__dict__.get("_explore_st")
        if st is None:
            st = {"task": None, "run": None, "memory": ExplorationMemory(), "policy": ExplorationPolicy(self.EXPLORE_TUNING)}
            self.__dict__["_explore_st"] = st
        return st

    def set_explore_budget(self, seconds: float) -> None:
        """The interface tells the body how long a walk should last (until the next prompt)."""
        try:
            self.explore_budget_s = max(5.0, float(seconds))
        except (TypeError, ValueError):
            pass

    def idle_busy(self) -> bool:
        """Is a walk going on? (The idle beat waits: the walk's own result is the next prompt.)"""
        task = self._explore_state()["task"]
        return task is not None and not task.done()

    @property
    def deliberate_action_in_flight(self) -> bool:
        """A walk is a deliberate act that outlasts the verb that began it: a fast motor layer must not take the legs
        back meanwhile (Minecraft's ``motor_step`` re-issues a ``goto`` every few seconds and would replace each leg)."""
        return self.idle_busy() or bool(getattr(super(), "deliberate_action_in_flight", False))

    def explore_memory(self) -> ExplorationMemory:
        return self._explore_state()["memory"]

    def reset_exploration(self) -> None:
        """A new stay: forget where she has been."""
        self.cancel_explore("the stay ended", silent=True)
        self._explore_state()["memory"].reset()

    # -- the adapter's side (override) --

    async def explore_probe(self, viewpoints: Sequence[Vec], max_candidates: int) -> Probe | None:  # pragma: no cover
        raise NotImplementedError

    async def explore_walk(self, pos: Vec, timeout: float) -> WalkResult:  # pragma: no cover
        raise NotImplementedError

    async def explore_look(self, seconds: float) -> LookResult:  # pragma: no cover
        raise NotImplementedError

    async def explore_study(self, poi: Candidate) -> bool:
        return True

    async def explore_halt(self) -> None:
        return None

    def explore_journal(self, text: str) -> None:
        """Hook: remember what she did for the idle prompt (the connector may keep a journal)."""
        note = getattr(self, "_note", None)
        if callable(note):
            note(text)

    # -- control --

    def cancel_explore(self, why: str = "stopped", silent: bool = True) -> bool:
        """Stop a walk at its next safe point. ``silent`` (the default): no event, the one who asked for the change
        knows. True when there was a walk to stop."""
        st = self._explore_state()
        run: ExplorationRun | None = st["run"]
        if run is None or not self.idle_busy():
            return False
        run.silent = silent
        run.cancel(why)
        return True

    def start_explore(self, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Begin a walk. Returns ``{"ok": bool, "detail": str, "data": {...}}`` (a connector wraps it)."""
        st = self._explore_state()
        if self.idle_busy():
            self.cancel_explore("replaced by a new walk", silent=True)
        budget = self.explore_budget_s
        run = ExplorationRun(self, st["memory"], st["policy"], budget, clock=self.explore_clock, world=self.EXPLORE_WORLD)
        st["run"] = run
        st["task"] = asyncio.ensure_future(self._explore_main(run))
        log_info(f"{LOG_PREFIX} [{self.EXPLORE_WORLD}] walk starts: about {budget:.0f}s")
        return {
            "ok": True,
            "detail": f"going for a walk of about {budget:.0f} seconds: you will be told what you come across",
            "data": {"routine": "explore", "budget_seconds": round(budget, 1)},
        }

    async def _explore_main(self, run: ExplorationRun) -> None:
        result = await run.run()
        # Remember it for the idle prompt in any case, tell the model unless whoever changed the plan already knows.
        if result.stopped_because == "interrupted":
            self.explore_journal(
                f"went for a walk, interrupted after {result.seconds:.0f}s ({len(result.discovered)} new things)"
            )
        else:
            names = ", ".join(d["name"] for d in result.discovered[:4])
            self.explore_journal(
                f"went for a walk of {result.seconds:.0f}s, {result.legs} stops" + (f"; came across {names}" if names else "")
            )
        if run.silent:
            log_debug(f"{LOG_PREFIX} [{self.EXPLORE_WORLD}] silent end ({result.stopped_because}): no event")
            return
        await self._explore_emit(result)

    async def _explore_emit(self, result: ExploreResult) -> None:
        callback = getattr(self, "_on_event", None)
        if callback is None:
            log_warning(f"{LOG_PREFIX} [{self.EXPLORE_WORLD}] the walk ended with nobody to tell")
            return
        from plugins.rift_vessel.vessel_base import PerceptionEvent

        event = PerceptionEvent(
            environment=self.EXPLORE_WORLD,
            event_type="explore_done",
            summary=format_explore_result(result),
            actor=None,
            salience=0.9,
            data=result.to_dict(),
        )
        try:
            out = callback(event)
            if asyncio.iscoroutine(out):
                await out
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} [{self.EXPLORE_WORLD}] could not deliver the result: {exc}")


__all__ = [
    "Candidate",
    "ExplorationMemory",
    "ExplorationPolicy",
    "ExplorationRun",
    "ExploreResult",
    "ExplorerMixin",
    "Hazard",
    "LatencyMeter",
    "LookResult",
    "Probe",
    "Tuning",
    "Vec",
    "WalkResult",
    "as_vec",
    "bearing",
    "explore_budget",
    "explore_schema",
    "flat_dist",
    "format_explore_result",
]
