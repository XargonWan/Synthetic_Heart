# plugins/live_base.py
"""Base class for Live bidirectional-streaming engines.

Live engines manage *persistent sessions* where audio chunks flow in
(microphone) and transcript/audio events flow out (STT results, TTS synthesis).
Examples: Gemini Live, Silero VAD + on-device ASR.

**Contract summary**

    async open_session(session_id, **kwargs)   → set up session
    async send_audio(session_id, chunk, sr)    → push PCM chunk (STT side)
    receive_events(session_id)                 → AsyncIterator[LiveEvent]
    async send_text(session_id, text)          → request TTS synthesis
    async close_session(session_id)            → flush + teardown

Engines register themselves at import time:

    from core.live_registry import register_live_engine
    from plugins.live_base import LiveEngineBase, LiveEvent, LiveEventType

    class MyLiveEngine(LiveEngineBase):
        ...

    ENGINE_CLASS = MyLiveEngine
    register_live_engine("my_engine", __name__, {"input": True, "output": True})
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


class LiveEventType(str, Enum):
    """Types of events emitted by a Live engine."""

    TRANSCRIPT = "transcript"  # STT result (partial or final)
    AUDIO = "audio"  # TTS audio chunk (bytes)
    VAD = "vad"  # Voice-activity detection signal
    ERROR = "error"  # Engine-side error
    TOOL_CALL = "tool_call"  # Model requested a function/tool call


@dataclass
class ToolCallPayload:
    """Model-agnostic representation of a tool/function call from the model.

    Attributes:
        call_id: Opaque identifier assigned by the model.  Must be echoed
                 back in the tool response so the model can correlate it.
        name:    Tool name — matches the ``ToolManifest.name`` / SyntH
                 action type.
        args:    Key-value arguments provided by the model.
    """

    call_id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class LiveEvent:
    """A single event emitted by a Live engine's ``receive_events`` stream."""

    type: LiveEventType
    text: Optional[str] = None  # Populated for TRANSCRIPT events
    audio: Optional[bytes] = None  # Populated for AUDIO events
    is_final: bool = False  # True when segment is complete
    vad_signal: Optional[str] = None  # "speech_start" | "speech_end" for VAD
    detail: Optional[str] = None  # Error message for ERROR events
    tool_call: Optional[ToolCallPayload] = None  # Populated for TOOL_CALL events
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class LiveEngineBase(ABC):
    """Abstract base for all Live streaming engines.

    All async methods default to no-ops so engines only need to implement
    the sides they actually support (``supports_input`` / ``supports_output``).
    """

    display_name: str = "Unnamed Live Engine"

    # ------------------------------------------------------------------
    # Capability flags (override in subclasses)
    # ------------------------------------------------------------------

    @property
    def supports_input(self) -> bool:
        """True when the engine can accept audio and produce transcripts."""
        return False

    @property
    def supports_output(self) -> bool:
        """True when the engine can synthesise speech from text."""
        return False

    # ------------------------------------------------------------------
    # Session lifecycle — required
    # ------------------------------------------------------------------

    @abstractmethod
    async def open_session(self, session_id: str, **kwargs: Any) -> None:
        """Initialise a new streaming session.

        Args:
            session_id: Unique token for this session.
            **kwargs:   Engine-specific options (e.g. ``language``, ``voice``).
        """

    @abstractmethod
    async def close_session(self, session_id: str) -> None:
        """Flush and tear down a session.  Must call even on error."""

    # ------------------------------------------------------------------
    # STT side (input)
    # ------------------------------------------------------------------

    async def send_audio(
        self,
        session_id: str,
        chunk: bytes,
        sample_rate: int = 16000,
    ) -> None:
        """Push a raw PCM audio chunk into the session.

        Args:
            session_id:  Token from ``open_session``.
            chunk:       Raw signed-16-bit little-endian PCM bytes.
            sample_rate: Sample rate of the incoming audio (default 16 000 Hz).
        """

    @abstractmethod
    async def receive_events(self, session_id: str) -> AsyncIterator[LiveEvent]:
        """Yield events produced by the engine for this session.

        The iterator must terminate naturally when ``close_session`` is called.
        """
        # Trick to make the type-checker happy for the default case.
        # Subclasses should use "yield" to make this an actual async generator.
        return
        yield

    # ------------------------------------------------------------------
    # TTS side (output) — optional
    # ------------------------------------------------------------------

    async def send_text(self, session_id: str, text: str) -> None:
        """Request speech synthesis for *text* within this session.

        The synthesised audio will arrive as ``LiveEvent(type=AUDIO, ...)``
        events on ``receive_events``.  Engines that do not support output may
        leave this as a no-op.
        """

    async def send_tool_response(
        self,
        session_id: str,
        call_id: str,
        name: str,
        result: dict[str, Any],
    ) -> None:
        """Send the result of a tool/function call back to the model.

        Called after the executor has run the action associated with a
        ``TOOL_CALL`` event.  The model unblocks and continues generating
        once it receives this response.

        Engines that support async scheduling should inspect
        ``result.get("scheduling")`` (values: ``"INTERRUPT"``, ``"WHEN_IDLE"``,
        ``"SILENT"``) to decide how to deliver the response (Gemini 2.5+).
        Engines that only support synchronous function calling (Gemini 3.1)
        can ignore the key.

        Args:
            session_id: Session that originated the tool call.
            call_id:    Opaque ID from ``ToolCallPayload.call_id`` — must be
                        echoed back verbatim.
            name:       Tool name from ``ToolCallPayload.name``.
            result:     Arbitrary dict returned by ``run_action``.
        """

    # ------------------------------------------------------------------
    # Lifecycle hooks (optional)
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Called once when the engine is instantiated.  Download models, warm up, etc."""

    def teardown(self) -> None:
        """Called when the engine is unloaded or the application shuts down."""
