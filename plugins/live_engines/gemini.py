# plugins/live_engines/gemini.py
"""Live engine stub: Gemini Live (bidirectional streaming).

Wraps the ``google-genai`` Live WebSocket API to provide real-time
speech-to-text *and* text-to-speech in a single persistent session.

**Status**: stub — connects the registry; full implementation is TODO.

Capabilities
------------
- ``input``  : True  (PCM audio chunks → transcript events)
- ``output`` : True  (text prompts → synthesised audio events)
- ``vad``    : True  (Gemini Live performs server-side VAD)
- ``local``  : False (requires Google API credentials)

Configuration
-------------
The engine reads ``GOOGLE_API_KEY`` (or ``GEMINI_API_KEY``) from the
environment.  Pass ``model`` as a keyword argument to ``open_session``
to override the default Live model (``gemini-2.0-flash-live-001``).

Registration
------------
Performed at import time — loading this module is sufficient.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from core.logging_utils import log_error, log_info, log_warning
from plugins.live_base import LiveEngineBase, LiveEvent

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.0-flash-live-001"


class GeminiLiveEngine(LiveEngineBase):
    """Live bidirectional engine using Gemini Live WebSocket API."""

    display_name = "Gemini Live"

    supports_input: bool = True
    supports_output: bool = True

    def __init__(self) -> None:
        # session_id → dict with {"queue": asyncio.Queue, "conn": Any}
        self._sessions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Lifecycle: session management
    # ------------------------------------------------------------------

    async def open_session(self, session_id: str, **kwargs: Any) -> None:
        """Open a Gemini Live WebSocket session.

        Args:
            session_id: Unique identifier for this session.
            **kwargs: Optional overrides — ``model`` (str).
        """
        if session_id in self._sessions:
            log_warning(f"[live/gemini] Session {session_id!r} already open, skipping.")
            return

        model: str = kwargs.get("model", _DEFAULT_MODEL)
        queue: asyncio.Queue[LiveEvent | object] = asyncio.Queue()
        self._sessions[session_id] = {"queue": queue, "conn": None, "model": model}

        log_info(
            f"[live/gemini] Session {session_id!r} opened (model={model}) — stub only."
        )
        # TODO: establish actual google-genai Live WebSocket connection here.
        # Example (google-genai ≥ 1.0):
        #   from google import genai
        #   client = genai.Client()
        #   conn = await client.aio.live.connect(model=model, config={"response_modalities": ["AUDIO"]})
        #   self._sessions[session_id]["conn"] = conn
        #   asyncio.create_task(self._pump_events(session_id, conn))

    async def close_session(self, session_id: str) -> None:
        """Close the Gemini Live session and signal receive_events to stop.

        Args:
            session_id: Session to close.
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            return

        # Signal the async generator to terminate.
        await session["queue"].put(_CLOSED)

        conn = session.get("conn")
        if conn is not None:
            try:
                # TODO: await conn.close()
                pass
            except Exception as exc:
                log_warning(
                    f"[live/gemini] Error closing session {session_id!r}: {exc}"
                )

        log_info(f"[live/gemini] Session {session_id!r} closed.")

    # ------------------------------------------------------------------
    # Input side: push PCM audio
    # ------------------------------------------------------------------

    async def send_audio(
        self,
        session_id: str,
        chunk: bytes,
        sample_rate: int = 16000,
    ) -> None:
        """Forward a PCM audio chunk to Gemini Live.

        Args:
            session_id: Target session.
            chunk: Raw PCM bytes (16-bit mono preferred).
            sample_rate: Sample rate in Hz (default 16 000).
        """
        session = self._sessions.get(session_id)
        if session is None:
            log_warning(
                f"[live/gemini] send_audio on unknown session {session_id!r}, ignoring."
            )
            return

        conn = session.get("conn")
        if conn is None:
            # Stub: nothing to do yet.
            return

        # TODO: await conn.send({"realtime_input": {"media_chunks": [{"data": chunk, "mime_type": "audio/pcm"}]}})

    # ------------------------------------------------------------------
    # Output side: request TTS synthesis
    # ------------------------------------------------------------------

    async def send_text(self, session_id: str, text: str) -> None:
        """Send a text prompt to Gemini Live for TTS synthesis.

        Args:
            session_id: Target session.
            text: Text to synthesise.
        """
        session = self._sessions.get(session_id)
        if session is None:
            log_warning(
                f"[live/gemini] send_text on unknown session {session_id!r}, ignoring."
            )
            return

        conn = session.get("conn")
        if conn is None:
            return

        # TODO: await conn.send({"client_content": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turn_complete": True}})

    # ------------------------------------------------------------------
    # Event stream
    # ------------------------------------------------------------------

    async def receive_events(self, session_id: str) -> AsyncIterator[LiveEvent]:  # type: ignore[override]
        """Yield LiveEvent objects from the Gemini Live session.

        Terminates when ``close_session`` is called or the connection drops.

        Args:
            session_id: Session to stream events from.

        Yields:
            LiveEvent instances (TRANSCRIPT, AUDIO, VAD, ERROR).
        """
        session = self._sessions.get(session_id)
        if session is None:
            log_error(
                f"[live/gemini] receive_events on unknown session {session_id!r}."
            )
            return

        queue: asyncio.Queue[LiveEvent | object] = session["queue"]

        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # Check session is still alive.
                if session_id not in self._sessions:
                    break
                continue

            if item is _CLOSED:
                break

            yield item  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Background pump (called once conn is established)
    # ------------------------------------------------------------------

    async def _pump_events(self, session_id: str, conn: Any) -> None:
        """Read events from the Gemini Live connection and push to the queue.

        Args:
            session_id: Owning session.
            conn: google-genai live connection object.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return

        # TODO: implement async for message in conn: ... parsing
        # Access the event queue via: session["queue"]
        # Example structure:
        #   if message.server_content:
        #       for part in message.server_content.model_turn.parts:
        #           if part.text:
        #               await queue.put(LiveEvent(type=LiveEventType.TRANSCRIPT, text=part.text, is_final=True))
        #           if part.inline_data:
        #               await queue.put(LiveEvent(type=LiveEventType.AUDIO, audio=part.inline_data.data))

        log_info(f"[live/gemini] _pump_events for {session_id!r} complete (stub).")


# ---------------------------------------------------------------------------
# Sentinel used to terminate the receive_events generator
# ---------------------------------------------------------------------------

_CLOSED = object()


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

ENGINE_CLASS = GeminiLiveEngine
