"""A Rift Vessel connector, written as an extension (``[[provides.vessels]]``).

A connector is the synth's body in one world. The Vessel in the Core decides *what* to do and
remembers it; the connector connects to the game or room, reports what happens, and does what
it is asked. Subclass :class:`VesselConnector` and give it to ``ctx.vessels.serve(name,
connector)``.

The method names are the ones the Core's connector contract uses, so a connector written for
the Core moves here almost unchanged. Two groups:

* **Coroutines the Core awaits**: ``connect``, ``disconnect``, ``act``, ``motor_step``, the
  goal and quest lookups, ``lookup_knowledge``, ``get_world_state``, ``get_embodiment_assets``.
* **Plain answers the Core reads without waiting**: ``is_connected``, ``idle_busy``,
  ``describe_surroundings``, ``get_world_actions`` and the like. The Core cannot wait for
  those, so :meth:`snapshot` collects them and the SDK pushes the snapshot to the Core
  whenever it changes (checked after every call and every second while connected).
  Call :meth:`VesselConnector.changed` to push at once.

Standard library only: it never imports ``core``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

#: ``await emit({"event_type": ..., "summary": ...})`` reports something that happened.
Emit = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class ActionResult:
    """What carrying out an action came to. Never raise for an ordinary in-world failure:
    return ``ok=False`` with a ``detail``."""

    ok: bool
    detail: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class VesselConnector:
    """Base class of a connector. Override what your world supports."""

    #: The world's chat is still there on the next visit (declare it in the manifest too).
    remembers_chat: bool = False
    #: Why the last ``connect`` failed (shown to the person who asked to enter the world).
    last_error: str | None = None
    #: She is a person in a room, not a bot with goals: when nobody talks to her she is told
    #: what is around her and asked what she wants to do (the Vessel's idle beat).
    uses_idle_beat: bool = False
    #: The Core verbs (``say``, ``move``, ``look``...) this world offers; ``None``: all of them.
    core_verbs: frozenset[str] | set[str] | list[str] | None = None
    #: The text to show for a Core verb in this world, in place of the generic one.
    core_verb_descriptions: dict[str, str] = {}

    def __init__(self) -> None:
        self._changed: Callable[[], None] | None = None
        self._deliberate_in_flight = 0

    # ------------------------------------------------------------- lifecycle

    async def connect(self, settings: dict[str, Any], emit: Emit) -> bool:
        """Connect to the world and start reporting events with ``emit``. On failure
        return ``False`` and set :attr:`last_error`."""
        raise NotImplementedError

    async def disconnect(self) -> None:
        """Leave the world and release what you hold. Safe to call twice."""

    async def act(
        self, action: str, payload: dict[str, Any]
    ) -> ActionResult | dict[str, Any]:
        """Carry out a normalised action (``say``, ``move``, ``look``, ...)."""
        raise NotImplementedError

    @property
    def deliberate_action_in_flight(self) -> bool:
        """True while a cognition-driven action runs: a fast reflex layer must yield."""
        return self._deliberate_in_flight > 0

    async def act_deliberate(
        self, action: str, payload: dict[str, Any]
    ) -> ActionResult | dict[str, Any]:
        """An action the model chose: keeps :attr:`deliberate_action_in_flight` set."""
        self._deliberate_in_flight += 1
        try:
            return await self.act(action, payload)
        finally:
            self._deliberate_in_flight -= 1

    # ------------------------------------------------------- awaited by the Core

    async def get_embodiment_assets(
        self, wanted_animations: list[str] | None = None
    ) -> dict[str, Any] | None:
        return None

    async def get_world_state(self) -> dict[str, Any] | None:
        """``{"environment", "health", "position", "possible_actions", "flags", "extra"}``."""
        return None

    async def idle_intention(self) -> dict[str, Any] | None:
        return None

    async def motor_step(self, goal: dict[str, Any] | None) -> dict[str, Any]:
        return {"acted": False, "reason": "no_motorics"}

    async def lookup_knowledge(
        self, query: str, limit: int = 3
    ) -> list[dict[str, Any]]:
        return []

    async def evaluate_goal_completion(
        self, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return {"satisfied": False}

    async def evaluate_goal_completion_from_history(
        self, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return {"satisfied": False}

    async def get_active_goal(self) -> dict[str, Any] | None:
        return None

    async def complete_active_goal(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "noop"}

    async def get_bases(self) -> list[dict[str, Any]]:
        return []

    async def get_active_quest(self) -> dict[str, Any] | None:
        return None

    async def on_entity_killed(self, mob_kind: str) -> None:
        return None

    async def probe_external_liveness(self) -> bool:
        return False

    def set_explore_budget(self, seconds: float) -> None:
        return None

    # ------------------------------------------- answers the Core reads at once

    @property
    def is_connected(self) -> bool:
        return False

    def get_in_world_name(self) -> str | None:
        return None

    def get_world_identity(self) -> str | None:
        return None

    def get_progression_context(self) -> list[str] | None:
        return None

    def get_progression_stage(self) -> dict[str, Any] | None:
        return None

    def describe_surroundings(self) -> str | None:
        return None

    def idle_busy(self) -> bool:
        return False

    def idle_ready_since(self) -> float | None:
        return 0.0

    def idle_journal(self) -> list[tuple[float, str]]:
        return []

    def describe_capabilities(self) -> dict[str, Any]:
        return {}

    def get_world_actions(self) -> dict[str, dict[str, Any]]:
        return {}

    def get_knowledge_sources(self) -> list[dict[str, Any]]:
        return []

    def snapshot(self) -> dict[str, Any]:
        """What the Core answers without asking you. Override to add or drop keys."""
        return {
            "is_connected": bool(self.is_connected),
            "last_error": self.last_error,
            "in_world_name": self.get_in_world_name(),
            "world_identity": self.get_world_identity(),
            "progression_context": self.get_progression_context(),
            "progression_stage": self.get_progression_stage(),
            "surroundings": self.describe_surroundings(),
            "idle_busy": self.idle_busy(),
            "idle_ready_since": self.idle_ready_since(),
            "idle_journal": [list(item) for item in self.idle_journal()],
            "capabilities": self.describe_capabilities(),
            "world_actions": self.get_world_actions(),
            "knowledge_sources": self.get_knowledge_sources(),
            "uses_idle_beat": bool(self.uses_idle_beat),
            "core_verbs": None if self.core_verbs is None else sorted(self.core_verbs),
            "core_verb_descriptions": dict(self.core_verb_descriptions),
        }

    def changed(self) -> None:
        """Something the Core reads at once has changed: push the snapshot now."""
        if self._changed is not None:
            self._changed()
