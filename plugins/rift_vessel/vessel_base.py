# plugins/rift_vessel/vessel_base.py
"""Base class and data types for Rift Vessel connectors.

A Vessel connector bridges SyntH to a single game world (Minecraft, Skyrim,
VRChat, ...). It is responsible only for **embodiment** — translating in-world
events into normalized :class:`PerceptionEvent` objects, and translating
SyntH's normalized actions into world-specific commands. Cognition, memory and
identity stay in the SyntH core (see the Rift Vessel decision boundary in
``docs/rift_vessel.rst``).

All connectors must subclass :class:`VesselConnectorBase` and implement the
abstract methods. Register a connector at module import time::

    from core.vessel_registry import register_vessel_connector
    from plugins.rift_vessel.vessel_base import VesselConnectorBase

    class MyWorldConnector(VesselConnectorBase):
        display_name = "My World"
        ...

    CONNECTOR_CLASS = MyWorldConnector
    register_vessel_connector("myworld", __name__, label="My world connector")

Design constraints (per project decisions):

* Connectors **must not** create agentic tasks (no Agent Lane / Drone spawn).
  Vessel actions run on the normal action path.
* Connectors **must not** write diary/memory entries mid-session. The Vessel
  interface buffers experiences and flushes a single autobiographical entry at
  end-of-session (explicit logout or long inactivity cooldown).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


@dataclass
class WorldState:
    """A normalized snapshot of the vessel's current environment.

    Kept deliberately technical (not narrative) so the cognition layer retains
    decision fidelity — see the perception discussion in issue #310.

    Attributes:
        environment:      Short world identifier, e.g. ``"minecraft"``.
        health:           Optional current health, engine-defined scale.
        position:         Optional ``{"x", "y", "z"}`` (or world-specific) coords.
        possible_actions: Normalized action names the vessel can currently take
                          (e.g. ``["move", "attack", "use"]``).
        flags:            Free-form technical state flags (combat, daytime, ...).
        extra:            Connector-specific payload, opaque to the core.
    """

    environment: str
    health: float | None = field(default=None)
    position: Dict[str, Any] | None = field(default=None)
    possible_actions: List[str] = field(default_factory=list)
    flags: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PerceptionEvent:
    """A single normalized event perceived from the world.

    The Vessel interface applies lightweight salience filtering (dedup +
    rate-limit) before injecting selected events into the message chain. A
    future perception/salience worker may replace this simple filter without
    changing this contract.

    Attributes:
        environment:  Short world identifier, e.g. ``"minecraft"``.
        event_type:   Normalized type, e.g. ``"chat"``, ``"proximity"``,
                      ``"damage"``, ``"spawn"``.
        summary:      Short human-readable summary of the event.
        actor:        Optional originating actor id/name (player, mob, ...).
        salience:     Optional connector hint in [0.0, 1.0]; higher = more
                      likely to reach cognition. ``None`` = let the interface
                      decide.
        data:         Structured technical payload (vectors, distances, ...).
    """

    environment: str
    event_type: str
    summary: str
    actor: str | None = field(default=None)
    salience: float | None = field(default=None)
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VesselActionResult:
    """Result of dispatching a normalized action to a Vessel connector.

    Attributes:
        ok:      Whether the action was accepted/executed by the world.
        detail:  Optional human-readable detail or error message.
        data:    Optional structured result payload from the connector.
    """

    ok: bool
    detail: str | None = field(default=None)
    data: Dict[str, Any] = field(default_factory=dict)


# Callback the interface passes to the connector so inbound world events can be
# injected into the message chain. The connector calls it for every perceived
# event; the interface applies salience filtering + enqueue.
PerceptionCallback = Callable[[PerceptionEvent], Any]


class VesselConnectorBase(ABC):
    """Abstract base for all Rift Vessel connectors.

    The Vessel core plugin/interface calls these methods and handles everything
    else (salience filtering, chain injection, session tracking, activity
    logging, end-of-session diary flush).
    """

    display_name: str = "Unnamed Vessel"

    # Human-readable reason for the most recent failed :meth:`connect`. A
    # connector should set this (and clear it on success / at the start of a
    # new attempt) so the core plugin can tell Synth — and the requester —
    # WHY entering the world failed, instead of a generic error. ``None`` when
    # there is no failure to report.
    last_error: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(
        self,
        settings: Dict[str, Any],
        on_event: PerceptionCallback,
    ) -> bool:
        """Connect to the world and start streaming perception events.

        Args:
            settings:  Connector-specific configuration (host, port, creds, ...),
                       typically parsed from ``VESSEL_SETTINGS``.
            on_event:  Callback the connector must invoke for every perceived
                       :class:`PerceptionEvent`. The interface handles salience
                       filtering and message-chain injection.

        Returns:
            ``True`` if the connection succeeded and the vessel is embodied.
            On failure return ``False`` **and** set :attr:`last_error` to a
            human-readable reason so the caller can surface it.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the world and release resources (idempotent)."""

    # ------------------------------------------------------------------
    # Action + perception
    # ------------------------------------------------------------------

    @abstractmethod
    async def act(
        self,
        action: str,
        payload: Dict[str, Any],
    ) -> VesselActionResult:
        """Execute a normalized action inside the world.

        Args:
            action:   Normalized action name without the ``vessel_`` prefix,
                      e.g. ``"say"``, ``"move"``, ``"look"``, ``"use"``.
            payload:  Action fields (text, target, direction, ...).

        Returns:
            A :class:`VesselActionResult`. Must never raise for a normal
            in-world failure — return ``ok=False`` with a ``detail`` instead.
        """

    async def get_world_state(self) -> WorldState | None:
        """Return the current :class:`WorldState`, or ``None`` if unavailable.

        Optional. Connectors that cannot cheaply snapshot state may skip this.
        """
        return None

    def describe_capabilities(self) -> Dict[str, Any]:
        """Return capability flags / metadata for the WebUI and prompt context.

        Optional override. Defaults to an empty dict.
        """
        return {}

    def get_world_actions(self) -> Dict[str, Dict[str, Any]]:
        """Return **world-specific** embodiment actions this connector adds.

        The core Vessel plugin already exposes the world-agnostic *core set*
        (``connect``, ``disconnect``, ``say``, ``move``, ``look``, ``use``,
        ``status``) that every embodied world shares. A connector may override
        this hook to declare **extra** verbs that only make sense in its world
        (e.g. Minecraft ``craft`` / ``mine``, Skyrim ``cast_spell`` / ``sneak``).

        The returned mapping uses the same schema shape as a plugin's
        ``get_supported_actions`` — keyed by the **bare verb** (no prefix), each
        value a dict with ``description`` / ``required_fields`` /
        ``optional_fields`` / ``security_level``. The core Vessel plugin
        namespaces the key as ``vessel_<world>_<verb>`` when exposing it and
        dispatches the verb back to :meth:`act`. Do **not** declare
        ``external_effects`` here — Vessel actions must stay on the normal
        (Fast-Lane) action path.

        Optional override. Defaults to an empty dict (core set only).
        """
        return {}

    @property
    def is_connected(self) -> bool:
        """Whether the connector currently holds a live world connection."""
        return False

    # ------------------------------------------------------------------
    # Setup hooks (optional)
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Called once when the connector is loaded. Warm up, probe, etc."""

    def teardown(self) -> None:
        """Called when the connector is unloaded or the app shuts down."""
