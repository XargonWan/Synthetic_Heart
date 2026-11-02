"""Karada -> RetroXR: her animation states, her voice and her face, over the rift socket.

Synth's Karada state server is the single source of truth for "what the avatar is doing":
it tells every registered *transport* when she starts thinking, writing or talking, when a
clip of her voice plays (with lip-sync), and what her face looks like. The WebUI is one
transport, the Karada API socket another. The rift is a third: while a RetroXR rift is open
this transport is registered, and everything Karada says goes to the mod as a ``karada``
frame, so the body in the room does what the avatar does instead of standing in idle.

    Synth -> mod   {"type": "karada", "kind": "animation"|"audio"|"face"|"model"|"expression"
                    |"direct", "payload": {...Karada's own payload...}}

The payloads are Karada's, unchanged (``vrm_animation_v2`` carries ``state``/``descriptor``/
``started_at``; ``tts-play`` carries the audio ``url``, ``lipsync``, ``audio_duration_s`` and
``text``). The mod maps a ``state`` to a clip of her skin and downloads the audio ``url`` from
the same server, so no file travels over the socket.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.karada_transport import KaradaTransport
from core.logging_utils import log_debug

LOG_PREFIX = "[retroxr_karada]"

#: ``send(frame)`` -> awaitable; ``is_open()`` -> bool. Supplied by the connector.
SendFn = Callable[[Dict[str, Any]], Awaitable[bool]]
OpenFn = Callable[[], bool]


class RiftKaradaTransport(KaradaTransport):
    """Delivers Karada payloads to the one RetroXR rift that is open."""

    def __init__(self, send: SendFn, is_open: OpenFn) -> None:
        self._send = send
        self._is_open = is_open
        self.sent = 0

    async def _forward(self, kind: str, payload: Dict[str, Any]) -> None:
        if not self._is_open():
            return
        ok = await self._send({"type": "karada", "kind": kind, "payload": dict(payload or {})})
        if ok:
            self.sent += 1
            log_debug(f"{LOG_PREFIX} {kind}: {str((payload or {}).get('type') or (payload or {}).get('state') or '')}")

    async def broadcast_animation(self, payload: Dict[str, Any]) -> None:
        await self._forward("animation", payload)

    async def broadcast_audio(self, payload: Dict[str, Any]) -> None:
        await self._forward("audio", payload)

    async def broadcast_face(self, payload: Dict[str, Any]) -> None:
        await self._forward("face", payload)

    async def broadcast_model(self, payload: Dict[str, Any]) -> None:
        await self._forward("model", payload)

    async def broadcast_expression(self, payload: Dict[str, Any]) -> None:
        await self._forward("expression", payload)

    async def broadcast_presence(self, payload: Dict[str, Any]) -> None:
        await self._forward("presence", payload)

    async def send_to_session(self, session_id: str, payload: Dict[str, Any]) -> None:
        await self._forward("direct", payload)

    async def preload_asset(
        self, session_id: Optional[str], payload: Dict[str, Any]
    ) -> None:
        # The mod fetches a clip by its state name when it is first asked for.
        return None

    def get_connected_sessions(self) -> List[str]:
        return ["retroxr"] if self._is_open() else []
