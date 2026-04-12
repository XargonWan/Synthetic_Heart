# plugins/live_engines/gemini.py
"""Live engine: Gemini Live (bidirectional WebSocket streaming).

Wraps the ``google-genai`` Live WebSocket API to provide real-time
speech-to-text *and* text-to-speech in a single persistent session.

Capabilities
------------
- ``input``  : True  (PCM audio chunks → TRANSCRIPT events)
- ``output`` : True  (text prompts → AUDIO events via TTS)
- ``vad``    : True  (Gemini Live performs server-side VAD; manual VAD also
               supported via ``activity_start`` / ``activity_end``)
- ``local``  : False (requires Google API credentials)

Configuration
-------------
The engine reads ``GOOGLE_API_KEY`` (or ``GEMINI_API_KEY``) from the
environment.  Pass ``model`` and ``tools`` as keyword arguments to
``open_session`` to override defaults.

Tool Calls
----------
When the model emits a function call the engine yields a
``LiveEvent(type=TOOL_CALL, tool_call=ToolCallPayload(...))`` event.
The caller (``LiveToolExecutor``) runs the action and calls
``send_tool_response`` to unblock the model.

Registration
------------
Performed at import time via the module-level ``ENGINE_CLASS`` attribute —
loading this module is sufficient.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, AsyncIterator

from core.logging_utils import log_error, log_info, log_warning
from plugins.live_base import (
    LiveEngineBase,
    LiveEvent,
    LiveEventType,
    ToolCallPayload,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-3.1-flash-live-preview"

# PCM constants — Live API always outputs 24 kHz; input is expected at 16 kHz.
_INPUT_MIME = "audio/pcm;rate=16000"

try:
    from google import genai
    from google.genai import types as _genai_types

    _HAS_GENAI_SDK = True
except ImportError:
    genai = None  # type: ignore[assignment]
    _genai_types = None  # type: ignore[assignment]
    _HAS_GENAI_SDK = False

    def _extract_message_parts_payload(message: Any) -> tuple[bytes | None, str | None]:
        """Extract model-turn audio/text from a Gemini Live message.

        Gemini 3.1 can bundle multiple model-turn parts in one server event. Read
        structured parts first, then fall back to convenience properties.
        """

        audio_chunks: list[bytes] = []
        text_parts: list[str] = []

        server_content = getattr(message, "server_content", None)
        model_turn = (
            getattr(server_content, "model_turn", None) if server_content else None
        )
        parts = getattr(model_turn, "parts", None) or []

        for part in parts:
            inline_data = getattr(part, "inline_data", None)
            if inline_data is not None:
                mime_type = str(getattr(inline_data, "mime_type", "") or "").lower()
                data = getattr(inline_data, "data", None)
                if data and (not mime_type or mime_type.startswith("audio/")):
                    if isinstance(data, str):
                        try:
                            data = base64.b64decode(data)
                        except Exception:
                            data = None
                    if isinstance(data, bytes):
                        audio_chunks.append(data)

            part_text = getattr(part, "text", None)
            if part_text:
                text_parts.append(str(part_text))

        fallback_audio = getattr(message, "data", None)
        if not audio_chunks and isinstance(fallback_audio, bytes) and fallback_audio:
            audio_chunks.append(fallback_audio)

        fallback_text = getattr(message, "text", None)
        if not text_parts and fallback_text:
            text_parts.append(str(fallback_text))

        audio = b"".join(audio_chunks) if audio_chunks else None
        text = "".join(text_parts).strip() if text_parts else None
        return audio, text


class GeminiLiveEngine(LiveEngineBase):
    """Live bidirectional engine using the Gemini Live WebSocket API.

    Each session is independent: one WebSocket connection per ``session_id``.
    The engine does *not* manage reconnection or personas — those are handled
    by ``LiveSessionManager``.
    """

    display_name = "Gemini Live"

    supports_input: bool = True
    supports_output: bool = True

    def __init__(self) -> None:
        # session_id → {"queue": Queue, "conn": AsyncSession, "ctx": CM, "model": str}
        self._sessions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open_session(self, session_id: str, **kwargs: Any) -> None:
        """Open a Gemini Live WebSocket session.

        Args:
            session_id: Unique identifier for this session.
            **kwargs:
                model (str): Model string override.
                config (LiveConnectConfig): Full config object; if omitted a
                    minimal audio-only config is constructed.
                tools (list): Pre-built Gemini tool declarations to pass in
                    the session config (from ``GeminiToolAdapter``).
                api_key (str): Gemini API key. Falls back to
                    ``GEMINI_API_KEY`` env / config registry if omitted.
        """
        if not _HAS_GENAI_SDK:
            raise RuntimeError(
                "[live/gemini] google-genai SDK is required. "
                "Install with: uv add google-genai"
            )

        if session_id in self._sessions:
            log_warning(f"[live/gemini] Session {session_id!r} already open, skipping.")
            return

        model: str = kwargs.get("model", _DEFAULT_MODEL)
        api_key: str = str(kwargs.get("api_key") or _resolve_api_key())

        live_config = kwargs.get("config")
        if live_config is None:
            # Minimal fallback config — callers should usually supply their own.
            tool_list = kwargs.get("tools")
            live_config = _genai_types.LiveConnectConfig(
                response_modalities=["AUDIO"],  # type: ignore[arg-type]
            )
            if tool_list:
                live_config.tools = tool_list

        client = genai.Client(
            api_key=api_key,
            http_options={"api_version": "v1alpha"},
        )
        queue: asyncio.Queue[LiveEvent | object] = asyncio.Queue()

        try:
            session_ctx = client.aio.live.connect(
                model=model,
                config=live_config,
            )
            session = await session_ctx.__aenter__()
        except Exception as exc:
            raise RuntimeError(
                f"[live/gemini] Failed to open session {session_id!r}: {exc}"
            ) from exc

        self._sessions[session_id] = {
            "queue": queue,
            "conn": session,
            "ctx": session_ctx,
            "model": model,
        }

        # Start the background pump that reads from the WebSocket and
        # pushes LiveEvent objects onto the queue.
        asyncio.create_task(
            self._pump_events(session_id, session),
            name=f"gemini_live_pump_{session_id}",
        )

        log_info(f"[live/gemini] Session {session_id!r} opened (model={model})")

    async def close_session(self, session_id: str) -> None:
        """Close the Gemini Live session and terminate ``receive_events``.

        Args:
            session_id: Session to close.
        """
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            return

        # Signal the receive_events generator to stop.
        await entry["queue"].put(_CLOSED)

        ctx = entry.get("ctx")
        if ctx is not None:
            try:
                await ctx.__aexit__(None, None, None)
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
            session_id:  Target session.
            chunk:       Raw signed-16-bit little-endian PCM bytes.
            sample_rate: Sample rate in Hz (default 16 000).
        """
        entry = self._sessions.get(session_id)
        if entry is None:
            log_warning(f"[live/gemini] send_audio on unknown session {session_id!r}.")
            return
        conn = entry["conn"]
        mime = f"audio/pcm;rate={sample_rate}"
        try:
            await conn.send_realtime_input(
                audio=_genai_types.Blob(data=chunk, mime_type=mime)
            )
        except Exception as exc:
            log_warning(f"[live/gemini] send_audio error in {session_id!r}: {exc}")

    # ------------------------------------------------------------------
    # Output side: inject text / trigger TTS
    # ------------------------------------------------------------------

    async def send_text(self, session_id: str, text: str) -> None:
        """Send a text message to Gemini Live (triggers TTS synthesis).

        On Gemini 3.1 Flash Live, use ``send_realtime_input(text=...)`` —
        ``send_client_content`` requires the ``history_config`` flag.

        Args:
            session_id: Target session.
            text:       Text to synthesise or inject as context.
        """
        entry = self._sessions.get(session_id)
        if entry is None:
            log_warning(f"[live/gemini] send_text on unknown session {session_id!r}.")
            return
        try:
            await entry["conn"].send_realtime_input(text=text)
        except Exception as exc:
            log_warning(f"[live/gemini] send_text error in {session_id!r}: {exc}")

    # ------------------------------------------------------------------
    # Tool response
    # ------------------------------------------------------------------

    async def send_tool_response(
        self,
        session_id: str,
        call_id: str,
        name: str,
        result: dict[str, Any],
    ) -> None:
        """Send the result of a tool call back to the Gemini model.

        Gemini 3.1 is synchronous-only: the model blocks until this is sent.
        Gemini 2.5 supports async scheduling; inspect ``result.get("scheduling")``
        and include it in the response if present.

        Args:
            session_id: Owning session.
            call_id:    ID from ``ToolCallPayload.call_id``.
            name:       Tool name from ``ToolCallPayload.name``.
            result:     Dict returned by ``run_action`` / ``LiveToolExecutor``.
        """
        entry = self._sessions.get(session_id)
        if entry is None:
            log_warning(
                f"[live/gemini] send_tool_response on unknown session {session_id!r}."
            )
            return

        # Strip internal scheduling hint from the payload dict Gemini receives
        # (Gemini 2.5: scheduling lives inside result; 3.1 ignores it).
        response_payload = {k: v for k, v in result.items() if k != "scheduling"}

        try:
            await entry["conn"].send_tool_response(
                function_responses=[
                    _genai_types.FunctionResponse(
                        id=call_id,
                        name=name,
                        response=response_payload,
                    )
                ]
            )
            log_info(
                f"[live/gemini] Sent tool response for {name!r} "
                f"(session {session_id!r})"
            )
        except Exception as exc:
            log_error(
                f"[live/gemini] send_tool_response failed for {name!r} "
                f"in session {session_id!r}: {exc}"
            )

    # ------------------------------------------------------------------
    # Event stream
    # ------------------------------------------------------------------

    async def receive_events(self, session_id: str) -> AsyncIterator[LiveEvent]:
        """Yield ``LiveEvent`` objects from the Gemini Live session.

        Terminates when ``close_session`` is called or the connection drops.

        Yields:
            ``AUDIO``      — TTS audio chunk (raw 24 kHz PCM bytes).
            ``TRANSCRIPT`` — Input or output transcription fragment.
            ``VAD``        — Voice-activity signal (speech_start / speech_end).
            ``TOOL_CALL``  — Model-requested function call.
            ``ERROR``      — Session-level error.
        """
        entry = self._sessions.get(session_id)
        if entry is None:
            log_error(
                f"[live/gemini] receive_events on unknown session {session_id!r}."
            )
            return

        queue: asyncio.Queue[LiveEvent | object] = entry["queue"]

        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if session_id not in self._sessions:
                    break
                continue

            if item is _CLOSED:
                break

            yield item

    # ------------------------------------------------------------------
    # Background pump
    # ------------------------------------------------------------------

    async def _pump_events(self, session_id: str, conn: Any) -> None:
        """Read messages from the Gemini WebSocket and push LiveEvents to the queue.

        This task runs for the lifetime of the session.  Each received message
        is decoded into one or more ``LiveEvent`` objects and placed on the
        internal queue consumed by ``receive_events``.

        Gemini 3.1 note: a single server event may contain *multiple* content
        parts simultaneously (e.g. audio + transcript in the same message).
        All parts are processed in order.
        """
        entry = self._sessions.get(session_id)
        if entry is None:
            return
        queue: asyncio.Queue[LiveEvent | object] = entry["queue"]

        try:
            async for message in conn.receive():
                # ----------------------------------------------------------
                # Audio data — SDK convenience property handles decoding
                # ----------------------------------------------------------
                audio_data, text = _extract_message_parts_payload(message)
                if audio_data:
                    await queue.put(
                        LiveEvent(
                            type=LiveEventType.AUDIO,
                            audio=audio_data,
                        )
                    )

                # ----------------------------------------------------------
                # Text (inline text response, not transcription)
                # ----------------------------------------------------------
                if text:
                    await queue.put(
                        LiveEvent(
                            type=LiveEventType.TRANSCRIPT,
                            text=text,
                            is_final=True,
                        )
                    )

                # ----------------------------------------------------------
                # Transcriptions (input + output audio → text)
                # ----------------------------------------------------------
                sc = getattr(message, "server_content", None)
                if sc is not None:
                    # Input transcription (what the user said)
                    it = getattr(sc, "input_transcription", None)
                    if it is not None:
                        t = getattr(it, "text", None)
                        if t:
                            await queue.put(
                                LiveEvent(
                                    type=LiveEventType.TRANSCRIPT,
                                    text=t,
                                    is_final=True,
                                    metadata={"side": "input"},
                                )
                            )
                    # Output transcription (what the model said)
                    ot = getattr(sc, "output_transcription", None)
                    if ot is not None:
                        t = getattr(ot, "text", None)
                        if t:
                            await queue.put(
                                LiveEvent(
                                    type=LiveEventType.TRANSCRIPT,
                                    text=t,
                                    is_final=True,
                                    metadata={"side": "output"},
                                )
                            )

                # ----------------------------------------------------------
                # Tool / function calls
                # ----------------------------------------------------------
                tool_call = getattr(message, "tool_call", None)
                if tool_call is not None:
                    function_calls = getattr(tool_call, "function_calls", []) or []
                    for fc in function_calls:
                        fc_name: str = str(getattr(fc, "name", ""))
                        fc_id: str = str(getattr(fc, "id", ""))
                        fc_args: dict[str, Any] = dict(getattr(fc, "args", {}) or {})
                        await queue.put(
                            LiveEvent(
                                type=LiveEventType.TOOL_CALL,
                                tool_call=ToolCallPayload(
                                    call_id=fc_id,
                                    name=fc_name,
                                    args=fc_args,
                                ),
                            )
                        )

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log_error(
                f"[live/gemini] _pump_events error in session {session_id!r}: {exc}"
            )
            await queue.put(LiveEvent(type=LiveEventType.ERROR, detail=str(exc)))
        finally:
            # Signal receive_events to stop if it hasn't already.
            await queue.put(_CLOSED)
            log_info(f"[live/gemini] _pump_events exited for session {session_id!r}.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str:
    """Return the Gemini API key from config registry or environment."""
    try:
        from core.config_manager import config_registry

        val = config_registry.get_value("GEMINI_API_KEY", "")
        if val:
            return str(val).strip()
    except Exception:
        pass
    import os

    return os.environ.get("GEMINI_API_KEY", os.environ.get("GOOGLE_API_KEY", ""))


# ---------------------------------------------------------------------------
# Sentinel used to terminate the receive_events generator
# ---------------------------------------------------------------------------

_CLOSED = object()


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

ENGINE_CLASS = GeminiLiveEngine
