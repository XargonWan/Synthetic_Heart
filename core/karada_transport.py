"""KaradaTransport — abstract transport layer for the Karada State Server.

Transports are responsible for delivering animation, model, face-value
and audio payloads to connected clients.  The KaradaStateServer only talks
to this interface, never to a specific WebSocket/HTTP implementation.

Concrete implementations:
    - ``WebSocketTransport``  (core/karada_ws_transport.py)  — built-in
    - Future: MQTT for IoT panels, gRPC for XR headsets, …
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class KaradaTransport(ABC):
    """Abstract base class for Karada State Server transports.

    Every concrete transport must implement all abstract methods.
    The KaradaStateServer keeps a list of transports and iterates them
    when broadcasting state changes.
    """

    # ------------------------------------------------------------------
    # Broadcast (to ALL connected clients)
    # ------------------------------------------------------------------

    @abstractmethod
    async def broadcast_animation(self, payload: Dict[str, Any]) -> None:
        """Push an animation command to every connected client.

        Args:
            payload: Dict with at least ``type``, ``file``, ``loop``, ``state``,
                     ``animation_id``, and optionally ``descriptor``,
                     ``animation_state``, ``play_section``, ``priority``, ``source``.
        """

    @abstractmethod
    async def broadcast_audio(self, payload: Dict[str, Any]) -> None:
        """Push a TTS / audio-play command to every connected client.

        Args:
            payload: Dict with ``type`` (``tts-play``), ``url``, and optionally
                     ``lipsync``, ``audio_duration_s``, ``text``.
        """

    @abstractmethod
    async def broadcast_face(self, payload: Dict[str, Any]) -> None:
        """Push face blend-shape values to every connected client.

        Args:
            payload: Dict with ``type`` (``vrm_face``) and ``values``.
        """

    @abstractmethod
    async def broadcast_model(self, payload: Dict[str, Any]) -> None:
        """Push a VRM model change command to every connected client.

        Args:
            payload: Dict with ``type`` (``vrm_model``), ``name``, ``url``,
                     and optionally ``hash``.
        """

    @abstractmethod
    async def broadcast_expression(self, payload: Dict[str, Any]) -> None:
        """Push a facial expression set/clear command to every connected client.

        Args:
            payload: Dict with ``type`` (``vrm_expression_set`` or
                     ``vrm_expression_clear``) and optional ``targets``.
        """

    # ------------------------------------------------------------------
    # Targeted send (single client / session)
    # ------------------------------------------------------------------

    @abstractmethod
    async def send_to_session(self, session_id: str, payload: Dict[str, Any]) -> None:
        """Send a payload to a single session.

        Args:
            session_id: Identifier of the target session.
            payload: Arbitrary JSON-serialisable dict.
        """

    # ------------------------------------------------------------------
    # Preload
    # ------------------------------------------------------------------

    @abstractmethod
    async def preload_asset(
        self,
        session_id: Optional[str],
        payload: Dict[str, Any],
    ) -> None:
        """Ask client(s) to pre-load an animation asset.

        If *session_id* is ``None``, the preload is broadcast to all clients.

        Args:
            session_id: Target session or ``None`` for broadcast.
            payload: Dict with ``type`` (``vrm_preload``), ``file``, ``state``,
                     ``descriptor``.
        """

    # ------------------------------------------------------------------
    # Connection introspection
    # ------------------------------------------------------------------

    @abstractmethod
    def get_connected_sessions(self) -> List[str]:
        """Return the list of currently connected session IDs."""

    def has_connected_clients(self) -> bool:
        """Return ``True`` when at least one client is connected."""
        return len(self.get_connected_sessions()) > 0
