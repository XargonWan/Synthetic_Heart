"""Speech in a call: audio frames in, normal turns out.

This is the default way a call *hears* — for every interface that gives the core raw
audio (Discord, Telegram, ...): the same path as a voice message, just continuous.

    PCM frames -> voice activity detection -> one utterance -> Auris (speech to text)
        -> "message received" signal -> a normal turn at ``PRIORITY_CALL``

The SyntH's answer is then an ordinary reply, spoken by Vox (``request_tts`` is forced
by ``CallManager.mark_turn``) and played into the call by the provider
(``CallManager.route_speech``). No Live engine is involved: that bidirectional route is
optional and off by default (``CALL_LIVE_ENGINE_ENABLED``).

A provider feeds mono 16-bit 16 kHz PCM with :meth:`CallAudioPipeline.feed` (from any
thread) and implements :meth:`core.call_manager.CallProvider.deliver_turn`. The WebUI
does not use this: its browser does the detection and uploads each utterance.
"""

from __future__ import annotations

import array
import asyncio
import collections
import math
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from core.call_manager import CallSession, get_call_manager
from core.logging_utils import log_debug, log_info, log_warning

SAMPLE_RATE = 16000

#: Shorter than this is a noise blip or a cough, not something said.
MIN_UTTERANCE_S = 0.6
#: A speaker that never pauses is cut here and sent as it is.
MAX_UTTERANCE_S = 30.0
#: Audio kept from before the VAD fired, so the first syllable is not lost.
PREROLL_S = 0.4
#: Silence that ends an utterance when the energy detector (no Silero) is in use.
_ENERGY_SILENCE_S = 0.8
#: Silence after which Silero reports speech_end (8 frames of 32 ms).
_SILERO_SILENCE_S = 0.26
#: The SyntH's own voice is not listened to while it speaks, nor for this long after.
_ECHO_COOLDOWN_S = 0.5


@dataclass
class _Stream:
    """Voice state of one speaker in one call."""

    key: str
    speaker_id: str
    speaker_name: str
    preroll: collections.deque[bytes] = field(default_factory=collections.deque)
    preroll_bytes: int = 0
    utterance: bytearray = field(default_factory=bytearray)
    speaking: bool = False
    started_at: float = 0.0
    # energy detector state
    noise_floor: float = 200.0
    last_voiced_at: float = 0.0
    muted_until: float = 0.0
    # How long after the last sound the detector in use reports speech_end.
    tail_s: float = 0.8


def _rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)


class CallAudioPipeline:
    """Turns the continuous audio of a call into transcribed utterances."""

    def __init__(self) -> None:
        self._streams: dict[tuple[str, str], _Stream] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._vad_warned: set[str] = set()
        self._tasks: set[asyncio.Future[None]] = set()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """The event loop utterances are processed on (frames may arrive on another thread)."""
        self._loop = loop

    # -- input ------------------------------------------------------------

    def feed(
        self,
        call_id: str,
        pcm16k: bytes,
        speaker_id: str = "",
        speaker_name: str = "",
    ) -> None:
        """Offer mono 16-bit 16 kHz PCM from one speaker. Thread-safe, never raises."""
        try:
            self._feed(call_id, pcm16k, speaker_id or "?", speaker_name or "")
        except Exception as exc:  # pragma: no cover - a frame must never break a call
            log_debug(f"[call_audio] frame dropped: {exc}")

    def _feed(
        self, call_id: str, pcm: bytes, speaker_id: str, speaker_name: str
    ) -> None:
        if not pcm:
            return
        manager = get_call_manager()
        session = manager.get_session(call_id)
        if session is None:
            return
        provider = manager.get_provider(session.interface_name)
        now = time.monotonic()
        key = f"{call_id}:{speaker_id}"
        with self._lock:
            stream = self._streams.get((call_id, speaker_id))
            if stream is None:
                stream = _Stream(key, speaker_id, speaker_name)
                self._streams[(call_id, speaker_id)] = stream
                self._open_vad(stream, session)
            elif speaker_name:
                stream.speaker_name = speaker_name

            # Never listen to the SyntH's own voice (or the signals it plays).
            speaking = False
            try:
                speaking = bool(provider and provider.is_agent_speaking(session))
            except Exception:  # pragma: no cover - defensive
                pass
            if speaking:
                stream.muted_until = now + _ECHO_COOLDOWN_S
            if now < stream.muted_until:
                self._reset(stream)
                return

            self._remember_preroll(stream, pcm)
            events = self._events(stream, pcm, now)
            if "speech_start" in events and not stream.speaking:
                stream.speaking = True
                stream.started_at = now
                stream.utterance = bytearray(b"".join(stream.preroll))
            elif stream.speaking:
                stream.utterance.extend(pcm)

            finished = "speech_end" in events and stream.speaking
            speech_s = max(0.0, (now - stream.started_at) - stream.tail_s)
            too_long = stream.speaking and len(stream.utterance) >= int(
                MAX_UTTERANCE_S * SAMPLE_RATE * 2
            )
            if finished or too_long:
                utterance = bytes(stream.utterance)
                self._reset(stream)
                # What was said, not the lead-in or the pause that ended it.
                if too_long or speech_s >= MIN_UTTERANCE_S:
                    self._submit(call_id, stream, utterance)
                else:
                    log_debug(
                        f"[call_audio] {speech_s:.2f}s of sound ignored (too short)"
                    )

    def _remember_preroll(self, stream: _Stream, pcm: bytes) -> None:
        stream.preroll.append(pcm)
        stream.preroll_bytes += len(pcm)
        limit = int(PREROLL_S * SAMPLE_RATE * 2)
        while stream.preroll_bytes - len(stream.preroll[0]) >= limit:
            stream.preroll_bytes -= len(stream.preroll.popleft())

    def _reset(self, stream: _Stream) -> None:
        stream.speaking = False
        stream.utterance = bytearray()
        stream.preroll.clear()
        stream.preroll_bytes = 0

    # -- voice activity ---------------------------------------------------

    def _open_vad(self, stream: _Stream, session: CallSession) -> None:
        try:
            from core.vad_service import VAD_SERVICE

            if VAD_SERVICE.is_available():
                VAD_SERVICE.open_session(stream.key)
                return
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[call_audio] Silero VAD not usable: {exc}")
        if session.call_id not in self._vad_warned:
            self._vad_warned.add(session.call_id)
            log_warning(
                f"[call_audio] call {session.call_id}: Silero VAD is not available, "
                "using the simple energy detector (less accurate in noisy rooms)"
            )

    def _events(self, stream: _Stream, pcm: bytes, now: float) -> list[str]:
        try:
            from core.vad_service import VAD_SERVICE

            if VAD_SERVICE.is_available():
                stream.tail_s = _SILERO_SILENCE_S
                return VAD_SERVICE.process_chunk(stream.key, pcm, SAMPLE_RATE)
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[call_audio] VAD failed, using the energy detector: {exc}")
        stream.tail_s = _ENERGY_SILENCE_S
        return self._energy_events(stream, pcm, now)

    def _energy_events(self, stream: _Stream, pcm: bytes, now: float) -> list[str]:
        """A deliberately simple fallback: sound clearly above the room's noise floor."""
        level = _rms(pcm)
        threshold = max(500.0, stream.noise_floor * 3.0)
        events: list[str] = []
        if level >= threshold:
            stream.last_voiced_at = now
            if not stream.speaking:
                events.append("speech_start")
        else:
            # Track the room's noise while nobody speaks.
            if not stream.speaking:
                stream.noise_floor = 0.95 * stream.noise_floor + 0.05 * level
            elif now - stream.last_voiced_at >= _ENERGY_SILENCE_S:
                events.append("speech_end")
        return events

    # -- output -----------------------------------------------------------

    def _submit(self, call_id: str, stream: _Stream, pcm: bytes) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            log_warning("[call_audio] no event loop bound; utterance dropped")
            return
        coro = self._process(call_id, stream.speaker_id, stream.speaker_name, pcm)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._track(asyncio.ensure_future(coro))
        else:
            loop.call_soon_threadsafe(lambda: self._track(asyncio.ensure_future(coro)))

    def _track(self, task: asyncio.Future[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(
        self, call_id: str, speaker_id: str, speaker_name: str, pcm: bytes
    ) -> None:
        """One utterance: transcribe it; if text came out, signal it and hand over the turn."""
        manager = get_call_manager()
        session = manager.get_session(call_id)
        if session is None:
            return
        text = await self._transcribe(pcm)
        if not text:
            log_debug(f"[call_audio] call {call_id}: nothing transcribed")
            return
        log_info(
            f"[call_audio] call {call_id}: {speaker_name or speaker_id} said {text!r}"
        )
        # Re-read: the call may have ended while the speech was being recognised.
        session = manager.get_session(call_id)
        provider = manager.get_provider(session.interface_name) if session else None
        if session is None or provider is None:
            return
        await manager.on_message_transcribed(call_id)
        try:
            await provider.deliver_turn(session, text, speaker_id, speaker_name)
        except NotImplementedError:
            log_warning(
                f"[call_audio] provider '{provider.name}' cannot deliver a turn "
                "(deliver_turn not implemented)"
            )
        except Exception as exc:
            log_warning(f"[call_audio] delivering the turn failed: {exc}")

    @staticmethod
    async def _transcribe(pcm: bytes) -> str:
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            auris = PLUGIN_REGISTRY.get("auris_plugin")
            if auris is None:
                log_warning("[call_audio] Auris is not loaded: the call cannot hear")
                return ""
            with tempfile.TemporaryDirectory(prefix="synth_call_") as tmp:
                path = Path(tmp) / "utterance.wav"
                await asyncio.to_thread(_write_wav, path, pcm)
                result = await auris.transcribe_audio(str(path), mime_type="audio/wav")
            return (getattr(result, "text", "") or "").strip() if result else ""
        except Exception as exc:
            log_warning(f"[call_audio] transcription failed: {exc}")
            return ""

    # -- lifecycle --------------------------------------------------------

    def end(self, call_id: str) -> None:
        """Forget a call's streams (VAD sessions included)."""
        with self._lock:
            doomed = [k for k in self._streams if k[0] == call_id]
            for k in doomed:
                stream = self._streams.pop(k)
                try:
                    from core.vad_service import VAD_SERVICE

                    VAD_SERVICE.close_session(stream.key)
                except Exception:  # pragma: no cover - defensive
                    pass
        self._vad_warned.discard(call_id)


_pipeline: CallAudioPipeline | None = None


def get_call_audio_pipeline() -> CallAudioPipeline:
    """Return the process-wide pipeline."""
    global _pipeline
    if _pipeline is None:
        _pipeline = CallAudioPipeline()
    return _pipeline


def reset_call_audio_pipeline() -> None:
    """Drop the singleton (tests)."""
    global _pipeline
    _pipeline = None
