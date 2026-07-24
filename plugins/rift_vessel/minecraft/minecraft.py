# plugins/rift_vessel/minecraft/minecraft.py
"""Minecraft Vessel connector (PoC).

Bridges SyntH's Rift Vessel layer to a Minecraft world via the Node.js
Mineflayer bridge (:mod:`interface_dev/minecraft_bridge_minimal.js`, managed by
:mod:`interface.minecraft_provisioner`). This connector speaks plain HTTP to the
local bridge:

* normalized actions (``say`` / ``move`` / ``look`` / ``use`` / ``status``) →
  ``POST /cmd``
* world events ← polled from ``GET /events`` and forwarded to the interface's
  perception callback as :class:`plugins.rift_vessel.vessel_base.PerceptionEvent`.

Design constraints (see ``docs/rift_vessel.rst``):

* The connector never creates agentic tasks — actions map 1:1 to bridge
  commands, and perception events are pushed through the interface's simple
  salience filter (no Agent Lane, no Drone).
* No diary/memory is written here; the interface buffers experiences and flushes
  at end-of-session.

Register at import time so the registry can discover it lazily.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import aiohttp

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.vessel_registry import register_vessel_connector
from plugins.rift_vessel.vessel_base import (
    PerceptionCallback,
    PerceptionEvent,
    VesselActionResult,
    VesselConnectorBase,
    WorldState,
)

LOG_PREFIX = "[minecraft_connector]"

ENVIRONMENT = "minecraft"
_POLL_INTERVAL_SEC = 1.0
_HTTP_TIMEOUT_SEC = 10.0


class MinecraftConnector(VesselConnectorBase):
    """Rift Vessel connector for Minecraft via the local Mineflayer bridge."""

    display_name = "Minecraft"

    def __init__(self) -> None:
        self._on_event: PerceptionCallback | None = None
        self._session: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._connected = False
        self._base_url = ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_base_url(self, settings: Dict[str, Any]) -> str:
        host = settings.get("bridge_host") or config_registry.get_value(
            "MINECRAFT_BRIDGE_HOST",
            "127.0.0.1",
            group="plugins",
            component="vessel_plugin",
        )
        port = settings.get("bridge_port") or config_registry.get_value(
            "MINECRAFT_BRIDGE_PORT",
            8137,
            group="plugins",
            component="vessel_plugin",
        )
        host = str(host or "127.0.0.1")
        port = str(port or "8137")
        return f"http://{host}:{port}"

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._session is None:
            return {"ok": False, "detail": "no http session"}
        try:
            async with self._session.post(
                f"{self._base_url}{path}", json=payload
            ) as resp:
                return await resp.json()
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} POST {path} failed: {exc}")
            return {"ok": False, "detail": str(exc)}

    async def _get(self, path: str) -> Dict[str, Any]:
        if self._session is None:
            return {"ok": False, "detail": "no http session"}
        try:
            async with self._session.get(f"{self._base_url}{path}") as resp:
                return await resp.json()
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} GET {path} failed: {exc}")
            return {"ok": False, "detail": str(exc)}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(
        self,
        settings: Dict[str, Any],
        on_event: PerceptionCallback,
    ) -> bool:
        self._on_event = on_event
        self._base_url = self._resolve_base_url(settings or {})
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SEC)
        self._session = aiohttp.ClientSession(timeout=timeout)

        health = await self._get("/health")
        if not health.get("ok"):
            log_error(
                f"{LOG_PREFIX} bridge health check failed at {self._base_url}: "
                f"{health.get('detail')}"
            )
            await self._close_session()
            return False

        if not health.get("mineflayer", True):
            log_error(f"{LOG_PREFIX} bridge reports mineflayer not installed")
            await self._close_session()
            return False

        # Tell the bridge to (re)connect to the Minecraft server.
        conn = await self._post("/connect", {})
        if not conn.get("ok"):
            log_error(f"{LOG_PREFIX} bridge failed to connect: {conn.get('detail')}")
            await self._close_session()
            return False

        self._connected = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        log_info(f"{LOG_PREFIX} connected via {self._base_url}")
        return True

    async def disconnect(self) -> None:
        self._connected = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        try:
            await self._post("/disconnect", {})
        except Exception:
            pass
        await self._close_session()
        log_info(f"{LOG_PREFIX} disconnected")

    async def _close_session(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    # ------------------------------------------------------------------
    # Perception polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._connected:
            try:
                res = await self._get("/events")
                events = res.get("events") if isinstance(res, dict) else None
                if events:
                    for raw in events:
                        self._dispatch_event(raw)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_debug(f"{LOG_PREFIX} poll error: {exc}")
            await asyncio.sleep(_POLL_INTERVAL_SEC)

    def _dispatch_event(self, raw: Dict[str, Any]) -> None:
        if not self._on_event or not isinstance(raw, dict):
            return
        try:
            event = PerceptionEvent(
                environment=str(raw.get("environment") or ENVIRONMENT),
                event_type=str(raw.get("event_type") or "event"),
                summary=str(raw.get("summary") or ""),
                actor=raw.get("actor"),
                salience=raw.get("salience"),
                data=raw.get("data") or {},
            )
            self._on_event(event)
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} failed to dispatch event: {exc}")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def act(
        self,
        action: str,
        payload: Dict[str, Any],
    ) -> VesselActionResult:
        if not self._connected:
            return VesselActionResult(ok=False, detail="not connected to a world")
        res = await self._post("/cmd", {"action": action, "payload": payload or {}})
        return VesselActionResult(
            ok=bool(res.get("ok")),
            detail=res.get("detail"),
            data=res.get("data") or {},
        )

    async def get_world_state(self) -> WorldState | None:
        if not self._connected:
            return None
        res = await self._post("/cmd", {"action": "status", "payload": {}})
        if not res.get("ok"):
            return None
        data = res.get("data") or {}
        return WorldState(
            environment=ENVIRONMENT,
            health=data.get("health"),
            position=data.get("position"),
            possible_actions=["say", "move", "look", "use"],
            flags={"connected": bool(data.get("connected"))},
            extra={"username": data.get("username")},
        )

    def describe_capabilities(self) -> Dict[str, Any]:
        return {
            "movement": True,
            "chat": True,
            "perception": True,
            "interaction": True,
            "local": True,
        }

    @property
    def is_connected(self) -> bool:
        return self._connected


# Module-level connector class + self-registration (registry contract).
CONNECTOR_CLASS = MinecraftConnector

register_vessel_connector(
    "minecraft",
    __name__,
    capabilities={
        "movement": True,
        "chat": True,
        "perception": True,
        "interaction": True,
        "local": True,
    },
    label="Minecraft (Mineflayer PoC)",
)
