"""WebSocketTransport — concrete KaradaTransport over WebSocket connections.

This transport forwards all Karada payloads to the WebSocket sessions
maintained by ``SynthWebUIInterface``.  It is the default (and currently
only) transport used in production.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from core.karada_transport import KaradaTransport
from core.logging_utils import log_debug, log_warning

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


class WebSocketTransport(KaradaTransport):
    """Deliver Karada payloads over WebSocket to connected browser / app clients.

    The transport holds a reference to a *connections* dict that maps
    ``session_id → WebSocket``.  This dict is typically owned by
    ``SynthWebUIInterface`` and shared by reference so that new
    connections are visible immediately.
    """

    def __init__(
        self,
        connections: Dict[str, "WebSocket"],
    ) -> None:
        self._connections = connections

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _broadcast(self, payload: Dict[str, Any], label: str) -> None:
        """Send *payload* to every connected session, logging failures."""
        for sid, ws in list(self._connections.items()):
            try:
                await ws.send_json(payload)
            except Exception as exc:
                log_warning(
                    f"[WebSocketTransport] Failed to {label} to session {sid}: {exc}"
                )

    # ------------------------------------------------------------------
    # KaradaTransport implementation
    # ------------------------------------------------------------------

    async def broadcast_animation(self, payload: Dict[str, Any]) -> None:
        await self._broadcast(payload, "broadcast animation")

    async def broadcast_audio(self, payload: Dict[str, Any]) -> None:
        await self._broadcast(payload, "broadcast audio")

    async def broadcast_face(self, payload: Dict[str, Any]) -> None:
        await self._broadcast(payload, "broadcast face")

    async def broadcast_model(self, payload: Dict[str, Any]) -> None:
        await self._broadcast(payload, "broadcast model")

    async def broadcast_expression(self, payload: Dict[str, Any]) -> None:
        await self._broadcast(payload, "broadcast expression")

    async def send_to_session(self, session_id: str, payload: Dict[str, Any]) -> None:
        ws = self._connections.get(session_id)
        if not ws:
            log_debug(
                f"[WebSocketTransport] No active websocket for session {session_id}"
            )
            return
        try:
            await ws.send_json(payload)
        except Exception as exc:
            log_warning(
                f"[WebSocketTransport] Failed to send to session {session_id}: {exc}"
            )

    async def preload_asset(
        self,
        session_id: Optional[str],
        payload: Dict[str, Any],
    ) -> None:
        if session_id is None:
            await self._broadcast(payload, "broadcast preload")
        else:
            await self.send_to_session(session_id, payload)

    def get_connected_sessions(self) -> List[str]:
        return list(self._connections.keys())
