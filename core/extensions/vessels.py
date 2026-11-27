"""Rift Vessel connectors that live in an extension (capability ``vessel.connector``).

The Vessel is in the Core: it owns the session, the salience filter, the beats, the diary and
the goals. What a *world* adds is a **connector**: a body that connects to the game or room,
turns what happens into perception events and carries out the actions the Vessel asks for.
An extension that declares ``[[provides.vessels]]`` and holds ``vessel.connector`` is offered to
the Vessel under the name it declared, next to any other connector, and nothing else in the
Vessel changes.

The Vessel calls a connector in two ways, and a remote one has to answer both:

* **Questions that take a moment** (``connect``, ``act``, ``motor_step``, the goal and quest
  lookups...) are coroutines. They become one ``vessel.call`` request to the extension.
* **Questions that are answered at once** (``is_connected``, ``idle_busy``,
  ``describe_surroundings``, the action table...) are plain methods called from synchronous
  code, so they cannot wait for a message. The extension keeps the Core's copy of them current
  by pushing a **state snapshot** (``vessel.state``) whenever one changes; the Core answers
  from the last snapshot. A key the extension never sent gets the connector base class's
  default.

What the extension sends the Core: ``vessel.perceive`` (an event of its world) and
``vessel.state`` (the snapshot). Both are accepted only for a connector the extension
declared, and perception only while the Vessel has the connector connected.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, Protocol

from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.gateway import ApiMethod, CallContext, Gateway, Param, Session
from core.extensions.manifest import VesselProvision

CAP_VESSEL = "vessel.connector"
#: The largest snapshot the extension may push in one message.
MAX_STATE_BYTES = 64_000
#: How long each request may take, in seconds. Anything not listed gets the default.
TIMEOUTS = {
    "connect": 120.0,
    "disconnect": 30.0,
    "act": 130.0,
    "act_deliberate": 130.0,
    "motor_step": 20.0,
    "lookup_knowledge": 40.0,
    "get_embodiment_assets": 60.0,
}
DEFAULT_TIMEOUT = 30.0
#: The questions the Vessel may ask a remote connector (``vessel.call``).
CALLABLE_METHODS = frozenset(
    {
        "connect",
        "disconnect",
        "act",
        "act_deliberate",
        "get_embodiment_assets",
        "idle_intention",
        "get_world_state",
        "lookup_knowledge",
        "motor_step",
        "evaluate_goal_completion",
        "evaluate_goal_completion_from_history",
        "get_active_goal",
        "complete_active_goal",
        "get_bases",
        "get_active_quest",
        "on_entity_killed",
        "probe_external_liveness",
        "set_explore_budget",
    }
)
#: The snapshot keys and what each answers on the connector.
STATE_KEYS = frozenset(
    {
        "is_connected",
        "last_error",
        "in_world_name",
        "world_identity",
        "progression_context",
        "progression_stage",
        "surroundings",
        "idle_busy",
        "idle_ready_since",
        "idle_journal",
        "capabilities",
        "world_actions",
        "knowledge_sources",
        "uses_idle_beat",
        "core_verbs",
        "core_verb_descriptions",
    }
)


class VesselHost(Protocol):
    """What the Core offers to register a connector in."""

    def taken(self, name: str) -> bool:
        """True when a connector of another origin already holds ``name``."""
        ...

    def register(
        self, name: str, connector: Any, label: str, capabilities: dict[str, bool]
    ) -> None: ...

    def unregister(self, name: str) -> None: ...


def connector_class() -> type:
    """The proxy class. Built here so importing this module needs no Vessel code."""
    from plugins.rift_vessel.vessel_base import (
        PerceptionEvent,
        VesselActionResult,
        VesselConnectorBase,
        WorldState,
    )

    class RemoteVesselConnector(VesselConnectorBase):
        """A connector whose body an extension runs."""

        def __init__(self, session: Session, spec: VesselProvision) -> None:
            self._session, self._spec = session, spec
            self.display_name = spec.label or spec.name
            self.remembers_chat = spec.remembers_chat
            self.last_error = None
            #: The last snapshot the extension pushed (``STATE_KEYS``).
            self.mirror: dict[str, Any] = {}
            self._on_event: Callable[[Any], Any] | None = None

        # ------------------------------------------------------------ plumbing

        def _gone(self) -> bool:
            return self._session.peer.closed

        async def _ask(self, method: str, args: dict[str, Any] | None = None) -> Any:
            if method not in CALLABLE_METHODS:
                raise RpcError(ErrorCode.INVALID_PARAMS, f"unknown method {method!r}")
            if self._gone():
                raise RpcError(
                    ErrorCode.INTERNAL_ERROR,
                    f"the extension of connector {self._spec.name!r} is not running",
                )
            return await self._session.call(
                "vessel.call",
                {"name": self._spec.name, "method": method, "args": args or {}},
                timeout=TIMEOUTS.get(method, DEFAULT_TIMEOUT),
            )

        async def _answer(
            self, method: str, args: dict[str, Any] | None, default: Any
        ) -> Any:
            """The extension's answer, or ``default`` when it could not give one."""
            try:
                return await self._ask(method, args)
            except RpcError as exc:
                self.last_error = str(exc)
                return default

        def _mirrored(self, key: str, default: Any) -> Any:
            if key in self.mirror:
                return self.mirror[key]
            return default

        def apply_state(self, state: dict[str, Any]) -> None:
            self.mirror.update({k: v for k, v in state.items() if k in STATE_KEYS})
            if "last_error" in state:
                error = state["last_error"]
                self.last_error = str(error) if error else None

        async def emit(self, event: dict[str, Any]) -> bool:
            """Hand a perceived event to the Vessel. False when nobody is listening."""
            callback = self._on_event
            if callback is None:
                return False
            built = PerceptionEvent(
                environment=str(event.get("environment") or self._spec.name),
                event_type=str(event.get("event_type") or ""),
                summary=str(event.get("summary") or ""),
                actor=(str(event["actor"]) if event.get("actor") else None),
                salience=(
                    float(event["salience"])
                    if isinstance(event.get("salience"), (int, float))
                    else None
                ),
                data=dict(event.get("data") or {}),
            )
            result = callback(built)
            if asyncio.iscoroutine(result):
                await result
            return True

        # ---------------------------------------------------------- lifecycle

        async def connect(self, settings: dict[str, Any], on_event: Any) -> bool:
            self.last_error = None
            self._on_event = on_event
            try:
                result = await self._ask("connect", {"settings": settings})
            except RpcError as exc:
                self.last_error = str(exc)
                self._on_event = None
                return False
            ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
            if isinstance(result, dict):
                if isinstance(result.get("state"), dict):
                    self.apply_state(result["state"])
                if result.get("error"):
                    self.last_error = str(result["error"])
            if not ok:
                self._on_event = None
            return ok

        async def disconnect(self) -> None:
            self._on_event = None
            self.mirror["is_connected"] = False
            if self._gone():
                return
            await self._answer("disconnect", None, None)

        # ----------------------------------------------------------- the body

        async def act(self, action: str, payload: dict[str, Any]) -> Any:
            try:
                result = await self._ask("act", {"action": action, "payload": payload})
            except RpcError as exc:
                return VesselActionResult(ok=False, detail=str(exc))
            return _action_result(result, VesselActionResult)

        async def act_deliberate(self, action: str, payload: dict[str, Any]) -> Any:
            try:
                result = await self._ask(
                    "act_deliberate", {"action": action, "payload": payload}
                )
            except RpcError as exc:
                return VesselActionResult(ok=False, detail=str(exc))
            return _action_result(result, VesselActionResult)

        async def get_embodiment_assets(
            self, wanted_animations: list[str] | None = None
        ) -> dict[str, Any] | None:
            result = await self._answer(
                "get_embodiment_assets", {"wanted_animations": wanted_animations}, None
            )
            return dict(result) if isinstance(result, dict) else None

        async def get_world_state(self) -> Any:
            result = await self._answer("get_world_state", None, None)
            if not isinstance(result, dict):
                return None
            return WorldState(
                environment=str(result.get("environment") or self._spec.name),
                health=result.get("health"),
                position=result.get("position"),
                possible_actions=[str(a) for a in result.get("possible_actions") or []],
                flags=dict(result.get("flags") or {}),
                extra=dict(result.get("extra") or {}),
            )

        # ---------------------------------------------------- the idle layer

        async def idle_intention(self) -> dict[str, Any] | None:
            result = await self._answer("idle_intention", None, None)
            return dict(result) if isinstance(result, dict) else None

        def idle_busy(self) -> bool:
            return bool(self._mirrored("idle_busy", False))

        def idle_ready_since(self) -> float | None:
            value = self._mirrored("idle_ready_since", 0.0)
            return float(value) if isinstance(value, (int, float)) else None

        def idle_journal(self) -> list[tuple[float, str]]:
            return [
                (float(t), str(line))
                for t, line in (
                    item
                    for item in self._mirrored("idle_journal", [])
                    if isinstance(item, (list, tuple)) and len(item) == 2
                )
            ]

        def set_explore_budget(self, seconds: float) -> None:
            try:
                asyncio.get_running_loop().create_task(
                    self._answer("set_explore_budget", {"seconds": seconds}, None)
                )
            except RuntimeError:
                return

        async def motor_step(self, goal: dict[str, Any] | None) -> dict[str, Any]:
            result = await self._answer(
                "motor_step", {"goal": goal}, {"acted": False, "reason": "no_motorics"}
            )
            return (
                dict(result)
                if isinstance(result, dict)
                else {"acted": False, "reason": "no_motorics"}
            )

        # ----------------------------------------- what the world tells about itself

        def describe_surroundings(self) -> str | None:
            value = self._mirrored("surroundings", None)
            return str(value) if value else None

        def describe_capabilities(self) -> dict[str, Any]:
            return dict(self._mirrored("capabilities", {}))

        def get_world_actions(self) -> dict[str, dict[str, Any]]:
            return dict(self._mirrored("world_actions", {}))

        def get_knowledge_sources(self) -> list[dict[str, Any]]:
            return [dict(s) for s in self._mirrored("knowledge_sources", [])]

        def get_knowledge_wiki_sources(self) -> list[Any]:
            # The extension looks things up itself (``lookup_knowledge``).
            return []

        def get_progression_stage(self) -> dict[str, Any] | None:
            value = self._mirrored("progression_stage", None)
            return dict(value) if isinstance(value, dict) else None

        def get_progression_context(self) -> list[str] | None:
            value = self._mirrored("progression_context", None)
            return [str(v) for v in value] if isinstance(value, list) else None

        async def lookup_knowledge(
            self, query: str, limit: int = 3, **kwargs: Any
        ) -> list[dict[str, Any]]:
            result = await self._answer(
                "lookup_knowledge", {"query": query, "limit": limit}, []
            )
            return [dict(r) for r in result or [] if isinstance(r, dict)]

        # ----------------------------------------------------- goals and quests

        async def evaluate_goal_completion(
            self, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            result = await self._answer(
                "evaluate_goal_completion",
                {"args": list(args), "kwargs": kwargs},
                {"satisfied": False},
            )
            return dict(result) if isinstance(result, dict) else {"satisfied": False}

        async def evaluate_goal_completion_from_history(
            self, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            result = await self._answer(
                "evaluate_goal_completion_from_history",
                {"args": list(args), "kwargs": kwargs},
                {"satisfied": False},
            )
            return dict(result) if isinstance(result, dict) else {"satisfied": False}

        async def get_active_goal(self) -> dict[str, Any] | None:
            result = await self._answer("get_active_goal", None, None)
            return dict(result) if isinstance(result, dict) else None

        async def complete_active_goal(
            self, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            result = await self._answer(
                "complete_active_goal",
                {"args": list(args), "kwargs": kwargs},
                {"status": "noop"},
            )
            return dict(result) if isinstance(result, dict) else {"status": "noop"}

        async def get_bases(self) -> list[dict[str, Any]]:
            result = await self._answer("get_bases", None, [])
            return [dict(b) for b in result or [] if isinstance(b, dict)]

        async def get_active_quest(self) -> dict[str, Any] | None:
            result = await self._answer("get_active_quest", None, None)
            return dict(result) if isinstance(result, dict) else None

        async def on_entity_killed(self, mob_kind: str) -> None:
            await self._answer("on_entity_killed", {"mob_kind": mob_kind}, None)

        # ------------------------------------------------------------- liveness

        @property
        def is_connected(self) -> bool:
            return (not self._gone()) and bool(self._mirrored("is_connected", False))

        async def probe_external_liveness(self) -> bool:
            if self._gone():
                return False
            return bool(await self._answer("probe_external_liveness", None, False))

        # What the Vessel reads off a connector besides the contract's methods.
        @property
        def uses_idle_beat(self) -> bool:
            return bool(self._mirrored("uses_idle_beat", False))

        @property
        def core_verbs(self) -> frozenset[str] | None:
            """The Core verbs this world offers; ``None``: it does not restrict itself."""
            value = self._mirrored("core_verbs", None)
            return frozenset(str(v) for v in value) if isinstance(value, list) else None

        @property
        def core_verb_descriptions(self) -> dict[str, str]:
            return {
                str(k): str(v)
                for k, v in dict(self._mirrored("core_verb_descriptions", {})).items()
            }

        def get_in_world_name(self) -> str | None:
            value = self._mirrored("in_world_name", None)
            return str(value) if value else None

        def get_world_identity(self) -> str | None:
            value = self._mirrored("world_identity", None)
            return str(value) if value else None

    return RemoteVesselConnector


def _action_result(raw: Any, cls: Any) -> Any:
    if isinstance(raw, dict):
        return cls(
            ok=bool(raw.get("ok")),
            detail=(str(raw["detail"]) if raw.get("detail") is not None else None),
            data=dict(raw.get("data") or {}),
        )
    return cls(ok=bool(raw))


def _owned(session: Session) -> dict[str, Any]:
    return session.state.setdefault("vessels", {})


def register_vessel_api(gateway: Gateway) -> None:
    """Register ``vessel.perceive`` and ``vessel.state`` on the gateway."""

    def connector(ctx: CallContext, name: str) -> Any:
        found = _owned(ctx.session).get(name)
        if found is None:
            gateway.record(ctx.extension_id, "vessel", "denied", name)
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"{name!r} is not a connector you declared",
            )
        return found

    async def perceive(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        remote = connector(ctx, args["name"])
        event = args["event"]
        if not isinstance(event, dict) or not str(event.get("event_type") or ""):
            raise RpcError(ErrorCode.INVALID_PARAMS, "event needs an event_type")
        return {"accepted": await remote.emit(event)}

    async def state(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        remote = connector(ctx, args["name"])
        snapshot = args["state"]
        if not isinstance(snapshot, dict):
            raise RpcError(ErrorCode.INVALID_PARAMS, "state must be an object")
        try:
            size = len(json.dumps(snapshot, separators=(",", ":"), allow_nan=False))
        except (TypeError, ValueError):
            raise RpcError(ErrorCode.INVALID_PARAMS, "state is not valid JSON")
        if size > MAX_STATE_BYTES:
            raise RpcError(
                ErrorCode.QUOTA_EXCEEDED,
                f"state is larger than {MAX_STATE_BYTES} bytes",
            )
        remote.apply_state(snapshot)
        return {"keys": sorted(k for k in snapshot if k in STATE_KEYS)}

    gateway.register(
        ApiMethod(
            "vessel.perceive",
            CAP_VESSEL,
            (
                Param("name", "str", max_length=40),
                Param("event", "object"),
            ),
            perceive,
            "Tell the Vessel about something that happened in the world.",
        )
    )
    gateway.register(
        ApiMethod(
            "vessel.state",
            CAP_VESSEL,
            (
                Param("name", "str", max_length=40),
                Param("state", "object"),
            ),
            state,
            "Update the Core's copy of the answers it gives without asking you.",
        )
    )


def attach_vessels(
    gateway: Gateway,
    host: VesselHost,
    *,
    warn: Callable[[str], None] | None = None,
) -> None:
    """Offer an extension's connectors to the Vessel while it is connected."""

    def on_attach(session: Session) -> None:
        specs = session.manifest.provides.vessels
        if not specs or not session.allowed(CAP_VESSEL):
            return
        cls = connector_class()
        added: list[str] = []
        for spec in specs:
            if host.taken(spec.name):
                if warn is not None:
                    warn(
                        f"[extensions] {session.extension_id}: connector {spec.name!r} "
                        "is already provided by another source; skipped"
                    )
                continue
            remote = cls(session, spec)
            _owned(session)[spec.name] = remote
            host.register(
                spec.name,
                remote,
                spec.label or spec.name,
                {flag: True for flag in spec.capabilities},
            )
            added.append(spec.name)

        async def cleanup() -> None:
            for name in added:
                _owned(session).pop(name, None)
                host.unregister(name)

        if added:
            session.on_close(cleanup)

    gateway.on_attach.append(on_attach)


class CoreVesselHost:
    """``VesselHost`` backed by the Core's Vessel registry."""

    @staticmethod
    def _registry() -> Any:
        from core.vessel_registry import VESSEL_REGISTRY

        return VESSEL_REGISTRY

    def taken(self, name: str) -> bool:
        registry = self._registry()
        if name not in registry.get_available_connectors():
            return False
        instance = registry.get_instance(name)
        return not type(instance).__name__ == "RemoteVesselConnector"

    def register(
        self, name: str, connector: Any, label: str, capabilities: dict[str, bool]
    ) -> None:
        self._registry().register_instance(
            name,
            connector,
            label=label,
            capabilities=capabilities,
        )

    def unregister(self, name: str) -> None:
        self._registry().unregister_connector(name)
