"""RetroXR rift socket: ``/api/vessel/retroxr/ws``.

The RetroXR mod (``xenu.synth_rift``) connects here when the player switches on the
Synthetic Heart Rift Summoner. The socket is the embodiment (see
:mod:`plugins.rift_vessel.retroxr.retroxr` for the wire protocol); this module owns
only the handshake and the read loop, and turns "the mod connected" into "Synth
entered the world" through the Rift Vessel plugin's own ``connect_world``.

The plugin and connector are imported lazily and every failure answers the mod with
an ``error`` frame: removing the RetroXR sub-plugin must not break the server.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core import webui_auth
from core.logging_utils import log_info, log_warning

LOG_PREFIX = "[RetroXRRift]"
ENVIRONMENT = "retroxr"
PROTOCOL_VERSION = 1
HELLO_TIMEOUT_SEC = 10.0


def _vessel_plugin() -> Any | None:
    try:
        from core.core_initializer import PLUGIN_REGISTRY

        return PLUGIN_REGISTRY.get("vessel_plugin")
    except Exception:  # pragma: no cover - defensive
        return None


async def _refuse(websocket: WebSocket, code: str, message: str) -> None:
    log_warning(f"{LOG_PREFIX} refused: {code} ({message})")
    try:
        await websocket.send_json({"type": "error", "code": code, "message": message})
    except Exception:
        pass
    try:
        await websocket.close(code=4400)
    except Exception:
        pass


async def handle_rift(websocket: WebSocket) -> None:
    """Run one rift session on an already-accepted socket."""
    try:
        from plugins.rift_vessel.retroxr.retroxr import RetroXRConnector  # noqa: F401
        from core.vessel_registry import VESSEL_REGISTRY
    except Exception as exc:
        await _refuse(websocket, "unavailable", f"RetroXR vessel is not installed: {exc}")
        return

    # 1. hello
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=HELLO_TIMEOUT_SEC)
        hello = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError, TypeError, ValueError):
        await _refuse(websocket, "bad_hello", "expected a JSON hello frame first")
        return
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        await _refuse(websocket, "bad_hello", "the first frame must be type 'hello'")
        return
    if int(hello.get("protocol") or 0) != PROTOCOL_VERSION:
        await _refuse(
            websocket,
            "protocol",
            f"this server speaks protocol {PROTOCOL_VERSION}, got {hello.get('protocol')!r}",
        )
        return

    plugin = _vessel_plugin()
    if plugin is None:
        await _refuse(websocket, "vessel_off", "the Rift Vessel plugin is not running")
        return

    # 2. adopt the socket, then enter the world through the plugin's own path.
    connector = VESSEL_REGISTRY.load_connector(ENVIRONMENT)
    if connector.is_connected:
        await _refuse(websocket, "busy", "a RetroXR rift is already open")
        return
    connector.attach_socket(websocket, hello)
    try:
        result = await plugin.connect_world(connector_name=ENVIRONMENT)
    except Exception as exc:
        connector.detach_socket()
        await _refuse(websocket, "connect_failed", str(exc))
        return
    if not result.ok:
        connector.detach_socket()
        await _refuse(websocket, "connect_failed", result.detail or "refused")
        return

    # 3. welcome (with the body), then pump frames until the mod goes away.
    assets: Dict[str, Any] | None = None
    try:
        assets = await connector.get_embodiment_assets()
    except Exception as exc:
        log_warning(f"{LOG_PREFIX} could not describe the body: {exc}")
    await websocket.send_json(
        {
            "type": "welcome",
            "protocol": PROTOCOL_VERSION,
            "session": (result.data or {}).get("session_id"),
            "assets": assets or {},
        }
    )
    log_info(f"{LOG_PREFIX} rift open (room={hello.get('room')!r})")
    # Now that she is welcomed: say what she is doing, then Karada's changes follow on their own.
    try:
        await connector.announce_state()
    except Exception as exc:
        log_warning(f"{LOG_PREFIX} could not announce her state: {exc}")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "request_assets":
                names = msg.get("animations")
                body = await connector.get_embodiment_assets(
                    [str(n) for n in names] if isinstance(names, list) else None
                )
                await websocket.send_json({"type": "assets", "assets": body or {}})
                continue
            await connector.handle_frame(msg)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log_warning(f"{LOG_PREFIX} socket error: {exc}")
    finally:
        # Leaving ends the session through the plugin so the recap is written once.
        try:
            await plugin.disconnect_world(connector_name=ENVIRONMENT)
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} disconnect_world failed: {exc}")
            connector.detach_socket()
        log_info(f"{LOG_PREFIX} rift closed")


def create_retroxr_router() -> APIRouter:
    """Return the router carrying the rift WebSocket (include it on the app)."""
    ws_router = APIRouter()

    @ws_router.websocket("/api/vessel/retroxr/ws")
    async def retroxr_ws(websocket: WebSocket) -> None:
        # Same gate as the Karada socket: the ASGI middleware already guards
        # routes, this keeps the check explicit for a handshake.
        if not webui_auth.websocket_authorized(websocket):
            await websocket.close(code=4401, reason="Invalid or missing API token")
            return
        await websocket.accept()
        await handle_rift(websocket)

    return ws_router
