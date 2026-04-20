# core/vad_service.py
"""Core singleton: Silero Voice Activity Detection (VAD) service.

``VADService`` wraps the Silero VAD model and provides a simple, session-aware
interface to apply voice-activity detection on raw PCM audio streams.

**Why a core service (not a plugin)?**

Silero VAD is a *utility* — it listens and reports whether someone is speaking.
It is not a conversational endpoint.  Any component (WebUI streaming endpoint,
Discord voice capture, future ambient listener …) can use it without going
through the plugin or Live registry.

Usage example
-------------

.. code-block:: python

    from core.vad_service import VAD_SERVICE

    if VAD_SERVICE.is_available():
        VAD_SERVICE.open_session("my_session")
        events = VAD_SERVICE.process_chunk("my_session", pcm_bytes, sample_rate=16000)
        # events might be ['speech_start'] or ['speech_end'] or []
        VAD_SERVICE.close_session("my_session")

Callback registration
---------------------

Other components can register named callbacks to react to global VAD events::

    def on_vad(session_id: str, event: str) -> None:
        ...

    VAD_SERVICE.register_handler("my_handler", on_vad)

Dependencies
------------

``silero-vad >= 5.1`` and ``torch`` must be installed:

.. code-block:: bash

    pip install '.[silero]'
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Callable

from core.logging_utils import log_debug, log_error, log_info, log_warning

# ---------------------------------------------------------------------------
# VAD events
# ---------------------------------------------------------------------------

SPEECH_START = "speech_start"
SPEECH_END = "speech_end"

# ---------------------------------------------------------------------------
# Internal per-session state
# ---------------------------------------------------------------------------


@dataclass
class _VADSession:
    """Stateful VAD tracker for one audio stream."""

    # Leftover bytes from the last chunk (not yet a full frame)
    buffer: bytearray = field(default_factory=bytearray)
    # Whether Silero currently considers us in a speech segment
    in_speech: bool = False
    # Consecutive silent frames counted so far (threshold: SILENCE_TRIGGER)
    silent_frames: int = 0
    # Consecutive voiced frames counted so far (threshold: SPEECH_TRIGGER)
    voiced_frames: int = 0


# ---------------------------------------------------------------------------
# VADService
# ---------------------------------------------------------------------------

# Silero expects exactly 512 samples at 16 kHz (32 ms per frame).
# At 8 kHz the model expects 256 samples.
_CHUNK_SAMPLES_16K = 512
_CHUNK_SAMPLES_8K = 256

# Hysteresis thresholds
_VAD_THRESHOLD = 0.5  # probability above this → voiced
_SPEECH_TRIGGER = 1  # voiced frames needed to emit speech_start
_SILENCE_TRIGGER = 8  # silent frames needed to emit speech_end (~256 ms)


class VADService:
    """Singleton Silero VAD service.

    Handles lazy model loading (silero-vad / torch optional), per-session
    buffering, hysteresis, and optional event callbacks.
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._torch: Any = None
        self._available: bool | None = None  # None = not yet attempted
        self._sessions: dict[str, _VADSession] = {}
        self._handlers: dict[str, Callable[[str, str], None]] = {}

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        """Attempt to load the Silero VAD model.

        Returns:
            True if the model loaded successfully, False otherwise.
        """
        if self._available is True:
            return True
        try:
            import torch  # type: ignore[import]

            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=False,
            )
            self._torch = torch
            self._model = model
            self._available = True
            log_info("[vad_service] Silero VAD model loaded successfully.")
            return True
        except Exception as exc:
            self._available = False
            log_warning(
                f"[vad_service] Silero VAD not available (install '.[silero]'): {exc}"
            )
            return False

    def is_available(self) -> bool:
        """Return True if the Silero model is (or can be) loaded.

        Returns:
            bool: Availability status.
        """
        if self._available is None:
            self.initialize()
        return bool(self._available)

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def open_session(self, session_id: str) -> None:
        """Create a new VAD session.

        Args:
            session_id: Unique identifier for this audio stream.
        """
        if session_id in self._sessions:
            log_debug(
                f"[vad_service] Session {session_id!r} already exists, resetting."
            )
        self._sessions[session_id] = _VADSession()
        if self._model is not None:
            try:
                self._model.reset_states()
            except Exception:
                pass
        log_debug(f"[vad_service] Session {session_id!r} opened.")

    def close_session(self, session_id: str) -> list[str]:
        """Close a VAD session and flush any pending speech_end.

        Args:
            session_id: Session to close.

        Returns:
            List of pending events (may contain ``speech_end`` if session
            ends mid-utterance).
        """
        sess = self._sessions.pop(session_id, None)
        events: list[str] = []
        if sess and sess.in_speech:
            events.append(SPEECH_END)
            self._fire_handlers(session_id, SPEECH_END)
        log_debug(f"[vad_service] Session {session_id!r} closed.")
        return events

    # ------------------------------------------------------------------
    # Audio processing
    # ------------------------------------------------------------------

    def process_chunk(
        self,
        session_id: str,
        pcm: bytes,
        sample_rate: int = 16000,
    ) -> list[str]:
        """Process a raw PCM s16le audio chunk and return VAD events.

        The service buffers partial frames internally; you do not need to
        send exactly-aligned chunks.

        Args:
            session_id: Session identifier (must have been opened first).
            pcm: Raw 16-bit little-endian PCM bytes.
            sample_rate: Audio sample rate in Hz (8000 or 16000).

        Returns:
            List of event strings — ``'speech_start'`` and/or ``'speech_end'``.
        """
        sess = self._sessions.get(session_id)
        if sess is None:
            log_warning(
                f"[vad_service] process_chunk on unknown session {session_id!r} — "
                "call open_session first."
            )
            return []

        if not self.is_available():
            return []  # silently no-op

        chunk_samples = (
            _CHUNK_SAMPLES_16K if sample_rate >= 16000 else _CHUNK_SAMPLES_8K
        )
        bytes_per_sample = 2  # s16le
        frame_bytes = chunk_samples * bytes_per_sample

        sess.buffer.extend(pcm)
        events: list[str] = []

        while len(sess.buffer) >= frame_bytes:
            frame = bytes(sess.buffer[:frame_bytes])
            del sess.buffer[:frame_bytes]

            prob = self._infer(frame, sample_rate)
            voiced = prob >= _VAD_THRESHOLD

            if voiced:
                sess.silent_frames = 0
                sess.voiced_frames += 1
                if not sess.in_speech and sess.voiced_frames >= _SPEECH_TRIGGER:
                    sess.in_speech = True
                    events.append(SPEECH_START)
                    self._fire_handlers(session_id, SPEECH_START)
            else:
                sess.voiced_frames = 0
                if sess.in_speech:
                    sess.silent_frames += 1
                    if sess.silent_frames >= _SILENCE_TRIGGER:
                        sess.in_speech = False
                        sess.silent_frames = 0
                        events.append(SPEECH_END)
                        self._fire_handlers(session_id, SPEECH_END)

        return events

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def register_handler(self, name: str, callback: Callable[[str, str], None]) -> None:
        """Register a global VAD event callback.

        Args:
            name: Unique handler name (used to remove it later).
            callback: ``callback(session_id: str, event: str)`` — called on
                each ``speech_start`` / ``speech_end`` event across all sessions.
        """
        self._handlers[name] = callback

    def unregister_handler(self, name: str) -> None:
        """Remove a previously registered callback.

        Args:
            name: Handler name passed to :meth:`register_handler`.
        """
        self._handlers.pop(name, None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _infer(self, frame_bytes: bytes, sample_rate: int) -> float:
        """Run Silero on one fixed-size PCM frame and return probability.

        Args:
            frame_bytes: Exactly ``chunk_samples * 2`` bytes of s16le PCM.
            sample_rate: Sample rate (8000 or 16000).

        Returns:
            Float in [0, 1] — speech probability.
        """
        try:
            torch = self._torch
            n_samples = len(frame_bytes) // 2
            samples = struct.unpack(f"<{n_samples}h", frame_bytes)
            tensor = torch.tensor(samples, dtype=torch.float32) / 32768.0
            prob = float(self._model(tensor, sample_rate).item())
            return prob
        except Exception as exc:
            log_debug(f"[vad_service] VAD inference error: {exc}")
            return 0.0

    def _fire_handlers(self, session_id: str, event: str) -> None:
        """Invoke all registered handlers for an event.

        Args:
            session_id: The session generating the event.
            event: ``'speech_start'`` or ``'speech_end'``.
        """
        for name, cb in list(self._handlers.items()):
            try:
                cb(session_id, event)
            except Exception as exc:
                log_error(f"[vad_service] Handler {name!r} raised: {exc}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

VAD_SERVICE: VADService = VADService()
