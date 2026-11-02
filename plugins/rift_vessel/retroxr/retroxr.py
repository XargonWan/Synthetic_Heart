# plugins/rift_vessel/retroxr/retroxr.py
"""RetroXR connector for the Rift Vessel.

RetroXR is a VR retro-gaming room (Godot). Unlike Minecraft, where Synth starts
a Node bridge and polls it, the world here comes to Synth: the RetroXR mod
``xenu.synth_rift`` opens a WebSocket to ``/api/vessel/retroxr/ws`` when the
player switches on the *Synthetic Heart Rift Summoner*. That socket IS the
embodiment: events flow in as :class:`PerceptionEvent`, Synth's verbs flow out
as ``action`` frames and the mod answers each with an ``action_result``.

The body, its model and its animations are Synth's Karada assets, served over
the existing ``/api/karada`` HTTP routes; this module only decides which ones the
world is told about (``get_embodiment_assets``).

Wire protocol (JSON text frames, protocol 1)::

    mod  -> synth   {"type": "hello", "protocol": 1, "room": "...", "capabilities": [...]}
    synth-> mod     {"type": "welcome", "session": "...", "assets": {...}}
    mod  -> synth   {"type": "event", "event_type": "chat", "summary": "...", "actor": "...",
                     "salience": 0.7, "data": {...}}
    mod  -> synth   {"type": "state", "state": {"position": {...}, "extra": {...}}}
    synth-> mod     {"type": "action", "id": 7, "action": "say", "payload": {...}}
    mod  -> synth   {"type": "action_result", "id": 7, "ok": true, "detail": "", "data": {}}

Constraints (docs/rift_vessel.rst): no agentic tasks, no diary mid-session. And
for RetroXR specifically: **the body never plays.** No verb may write input to a
core port; ``act`` refuses anything outside :data:`WORLD_VERBS` / the core set.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any, Dict

from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_info, log_warning
from core.plugin_base import PluginBase
from core.vessel_registry import register_vessel_connector
from plugins.rift_vessel.vessel_base import (
    PerceptionCallback,
    PerceptionEvent,
    VesselActionResult,
    VesselConnectorBase,
    WorldState,
)

LOG_PREFIX = "[retroxr_connector]"
ENVIRONMENT = "retroxr"
PROTOCOL_VERSION = 1

# How long the mod has to answer one action frame.
_ACTION_TIMEOUT_SEC = 20.0
# Verbs that take a while in the room (walking across it, a carry).
_LONG_VERBS = frozenset({"move", "follow", "pick_up", "put_down", "hand_over", "sit"})
_LONG_ACTION_TIMEOUT_SEC = 90.0

# The core set (shared by every world) that makes sense inside a room.
CORE_VERBS = frozenset({"say", "move", "look", "use", "follow", "unfollow", "status"})

# RetroXR's own verbs: bare names, exposed as ``vessel_retroxr_<verb>``.
WORLD_VERBS: Dict[str, Dict[str, Any]] = {
    "look_at": {
        "description": (
            "Turn your head and eyes toward something in the room: a screen, an "
            "object, or the player. Give the object's id from your surroundings."
        ),
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
    "touch": {
        "description": (
            "Reach out and touch an object in the room, or the player's hand. "
            "This never presses a controller button: you do not play games."
        ),
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
    "pick_up": {
        "description": (
            "Pick an object up in your hand so you can look at it or carry it. "
            "You hold one object at a time. The player may take it from your hand."
        ),
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
    "put_down": {
        "description": "Put down what you are holding where you stand (or on 'target').",
        "required_fields": [],
        "optional_fields": ["target"],
        "security_level": "low",
    },
    "hand_over": {
        "description": "Hold what you are carrying out to the player so they can take it.",
        "required_fields": [],
        "optional_fields": [],
        "security_level": "low",
    },
    "sit": {
        "description": "Sit on a seat or a surface in the room; 'stand' as the target stands you up.",
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
    "emote": {
        "description": (
            "Play an animation from your skin: for example wave, laugh, think, "
            "greet. The animation must exist in your skin; unknown names are ignored."
        ),
        "required_fields": ["name"],
        "optional_fields": [],
        "security_level": "low",
    },
    "inspect": {
        "description": (
            "Look closely at an object or a screen and describe what you see. "
            "For a screen this means what is on it right now."
        ),
        "required_fields": ["target"],
        "optional_fields": [],
        "security_level": "low",
    },
}


class RetroXRConnector(VesselConnectorBase):
    """Rift Vessel connector for the RetroXR room, over the mod's WebSocket."""

    display_name = "RetroXR"

    def __init__(self) -> None:
        self._on_event: PerceptionCallback | None = None
        self._ws: Any = None
        self._connected = False
        self._connect_settings: Dict[str, Any] = {}
        self._hello: Dict[str, Any] = {}
        self._state: Dict[str, Any] = {}
        self._pending: Dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._send_lock = asyncio.Lock()
        self.last_error = None

    # ------------------------------------------------------------------
    # Socket ownership (driven by the route in core/vessel_retroxr_api.py)
    # ------------------------------------------------------------------

    def attach_socket(self, ws: Any, hello: Dict[str, Any]) -> None:
        """Adopt the mod's socket. Called BEFORE ``connect_world`` runs."""
        self._ws = ws
        self._hello = dict(hello or {})
        self._state = {}
        self.last_error = None

    def detach_socket(self) -> None:
        """Forget the socket and fail every action still waiting for an answer."""
        self._ws = None
        self._connected = False
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(VesselActionResult(ok=False, detail="rift_closed"))
        self._pending.clear()

    @property
    def is_connected(self) -> bool:
        return bool(self._connected and self._ws is not None)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(
        self, settings: Dict[str, Any], on_event: PerceptionCallback
    ) -> bool:
        self._connect_settings = dict(settings or {})
        if self._ws is None:
            self.last_error = (
                "the RetroXR rift is closed: switch on the Synthetic Heart Rift "
                "Summoner in the room to open it"
            )
            log_info(f"{LOG_PREFIX} connect refused: no socket attached")
            return False
        self._on_event = on_event
        self._connected = True
        log_info(
            f"{LOG_PREFIX} embodied (room={self._hello.get('room')!r}, "
            f"caps={self._hello.get('capabilities')})"
        )
        return True

    async def disconnect(self) -> None:
        ws = self._ws
        self.detach_socket()
        self._on_event = None
        if ws is not None:
            try:
                await ws.close()
            except Exception as exc:
                log_debug(f"{LOG_PREFIX} close on disconnect: {exc}")
        log_info(f"{LOG_PREFIX} disconnected")

    def get_world_identity(self) -> str | None:
        """Per-room scope for goals and history: ``vessel/retroxr/<room>``."""
        room = str(self._hello.get("room") or "").strip()
        return room or None

    # ------------------------------------------------------------------
    # Inbound frames
    # ------------------------------------------------------------------

    async def handle_frame(self, msg: Dict[str, Any]) -> None:
        """Dispatch one frame received from the mod."""
        kind = str(msg.get("type") or "")
        if kind == "action_result":
            fut = self._pending.pop(int(msg.get("id") or 0), None)
            if fut is not None and not fut.done():
                fut.set_result(
                    VesselActionResult(
                        ok=bool(msg.get("ok")),
                        detail=msg.get("detail") or None,
                        data=msg.get("data") if isinstance(msg.get("data"), dict) else {},
                    )
                )
            return
        if kind == "state":
            state = msg.get("state")
            if isinstance(state, dict):
                self._state = state
            return
        if kind == "event":
            await self._dispatch_event(msg)
            return
        log_debug(f"{LOG_PREFIX} ignored frame type {kind!r}")

    async def _dispatch_event(self, raw: Dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            event = PerceptionEvent(
                environment=ENVIRONMENT,
                event_type=str(raw.get("event_type") or "event"),
                summary=str(raw.get("summary") or ""),
                actor=raw.get("actor"),
                salience=raw.get("salience"),
                data=raw.get("data") if isinstance(raw.get("data"), dict) else {},
            )
            result = self._on_event(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} failed to dispatch event: {exc}")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def act(self, action: str, payload: Dict[str, Any]) -> VesselActionResult:
        verb = str(action or "")
        if verb not in CORE_VERBS and verb not in WORLD_VERBS:
            # The body never plays: nothing outside the declared verbs reaches the room.
            log_warning(f"{LOG_PREFIX} refused unknown verb {verb!r}")
            return VesselActionResult(ok=False, detail=f"unknown_verb: {verb}")
        if not self.is_connected:
            return VesselActionResult(ok=False, detail="rift_closed")

        action_id = next(self._ids)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[action_id] = fut
        try:
            async with self._send_lock:
                await self._ws.send_json(
                    {
                        "type": "action",
                        "id": action_id,
                        "action": verb,
                        "payload": dict(payload or {}),
                    }
                )
        except Exception as exc:
            self._pending.pop(action_id, None)
            self._connected = False
            self.last_error = f"lost the RetroXR socket: {exc}"
            log_warning(f"{LOG_PREFIX} send failed for {verb!r}: {exc}")
            return VesselActionResult(ok=False, detail="rift_closed")

        timeout = _LONG_ACTION_TIMEOUT_SEC if verb in _LONG_VERBS else _ACTION_TIMEOUT_SEC
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(action_id, None)
            log_warning(f"{LOG_PREFIX} {verb!r} not answered in {timeout:.0f}s")
            return VesselActionResult(ok=False, detail="timeout")

    # ------------------------------------------------------------------
    # World state and capabilities
    # ------------------------------------------------------------------

    async def get_world_state(self) -> WorldState | None:
        if not self.is_connected:
            return None
        st = self._state or {}
        extra = dict(st.get("extra") or {})
        extra.setdefault("room", self._hello.get("room"))
        return WorldState(
            environment=ENVIRONMENT,
            health=None,
            position=st.get("position"),
            possible_actions=sorted(CORE_VERBS | set(WORLD_VERBS)),
            flags={"connected": True, "holding": st.get("holding")},
            extra=extra,
        )

    def get_world_actions(self) -> Dict[str, Dict[str, Any]]:
        return {verb: dict(schema) for verb, schema in WORLD_VERBS.items()}

    def describe_capabilities(self) -> Dict[str, Any]:
        return {
            "movement": True,
            "chat": True,
            "perception": True,
            "interaction": True,
            "gameplay": False,
            "mod_capabilities": list(self._hello.get("capabilities") or []),
        }

    async def get_embodiment_assets(
        self, wanted_animations: list[str] | None = None
    ) -> Dict[str, Any] | None:
        """What the world is told about Synth's body (Rift Vessel extension).

        The mod fetches the files over ``/skins/...`` itself; here Synth names the
        skin and resolves the animation names RetroXR asked for. Nothing is pushed.
        """
        from core.embodiment_assets import describe_assets

        wanted = wanted_animations
        if wanted is None:
            wanted = list(self._hello.get("animations") or [])
        return await describe_assets(
            wanted_animations=wanted,
        )


CONNECTOR_CLASS = RetroXRConnector

register_vessel_connector(
    "retroxr",
    __name__,
    capabilities={
        "movement": True,
        "chat": True,
        "perception": True,
        "interaction": True,
        "local": False,
    },
    label="RetroXR (Rift Summoner)",
)


class RetroXRVesselPlugin(PluginBase):
    """Attachable RetroXR Vessel sub-plugin (toggleable like Minecraft's).

    Registers no actions: the ``vessel_*`` actions live in the Rift Vessel core
    plugin. It surfaces the RetroXR world as its own card and owns its keys.
    """

    display_name = "RetroXR Vessel"

    def __init__(self) -> None:
        super().__init__()
        register_plugin("retroxr_vessel", self)
        log_info("[retroxr_vessel] Registered RetroXRVesselPlugin")

    def get_metadata(self) -> dict:
        return {
            "name": "retroxr_vessel",
            "display_name": "RetroXR Vessel",
            "description": (
                "Lets Synth step into a RetroXR room through the Synthetic Heart "
                "Rift Summoner (the RetroXR mod xenu.synth_rift). The mod connects to "
                "this server; Synth moves, speaks and handles objects in the room, "
                "but never plays games. Requires the Rift Vessel core plugin."
            ),
            "category": "Vessels",
            "icon": "icon.svg",
            "guide": "guide.md",
            "depends_on": ["vessel_plugin"],
        }

    def get_supported_action_types(self) -> list[str]:
        return []

    def get_supported_actions(self) -> dict:
        return {}

    async def teardown(self) -> None:
        """Close the rift when this sub-plugin is switched off. Never raises."""
        try:
            from core.vessel_registry import VESSEL_REGISTRY

            connector = (getattr(VESSEL_REGISTRY, "_instances", {}) or {}).get(
                ENVIRONMENT
            )
            if connector is not None:
                await connector.disconnect()
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[retroxr_vessel] teardown: disconnect failed: {exc}")
        try:
            from core.core_initializer import INTERFACE_REGISTRY

            iface = INTERFACE_REGISTRY.get("vessel")
            if iface is not None and hasattr(iface, "end_sessions_for_environment"):
                await iface.end_sessions_for_environment(ENVIRONMENT, reason="logout")
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[retroxr_vessel] teardown: end session failed: {exc}")
        log_info("[retroxr_vessel] teardown complete (rift closed on disable)")


PLUGIN_CLASS = RetroXRVesselPlugin
