"""Centralised audio/video call service.

The SyntH can answer a call: a live audio or video conversation with a person.
This module owns the *concept* of a call once, for every interface:

* :class:`CallProvider` — what an interface implements to host calls (WebUI
  today; Telegram, Discord, … register the same way). The manager never imports
  an interface library: providers are optional components that fail closed.
* :class:`CallSession` — one live call (who, where, audio or video, state).
* :class:`CallManager` — registry of providers, the active sessions, the
  "message received" acknowledgement, and the screen-share frame intake.

Priority: a turn that belongs to a call is flagged with
:meth:`CallManager.mark_turn`; ``core.message_queue.enqueue`` then ranks it at
``PRIORITY_CALL`` — below a rift/vessel reflection, above every chat band.

Video calls: the SyntH's "webcam" is the Karada render (a close-up of the
model). It is produced client-side, so a provider only declares the ``video``
capability; a provider without it degrades the call to audio.

Screen share: a provider that declares ``screen_share`` pushes JPEG frames in
through :meth:`CallManager.submit_screen_frame`. Only the latest frame is kept;
it is attached to the next call turn as a standard multimodal attachment — the
same ``{mime_type, data, filename}`` shape the RetroXR vessel uses for what the
SyntH sees.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from core.call_ack_cache import get_call_ack_cache
from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning
from core.variables_engine import register_exposed_var

register_exposed_var(
    "CALL_ENABLED",
    label="Calls Enabled",
    default=True,
    value_type=bool,
    ui_type="bool",
    description="Allow audio/video calls with the SyntH (WebUI and call-capable interfaces).",
    scope="core",
    component="call_manager",
)
register_exposed_var(
    "CALL_LIVE_ENGINE_ENABLED",
    label="Use a Live Engine For Calls",
    default=False,
    value_type=bool,
    ui_type="bool",
    description=(
        "Off (default): a call is heard by speech-to-text and answered by a normal "
        "turn and voice, on every interface. On: calls whose interface supports it hand "
        "the conversation to a bidirectional Live engine (e.g. Gemini Live) instead."
    ),
    scope="core",
    component="call_manager",
)
register_exposed_var(
    "CALL_SOUNDS_ENABLED",
    label="Call Start/End Sounds",
    default=True,
    value_type=bool,
    ui_type="bool",
    description="Play a short pickup sound when a call starts and a hang-up sound when it ends.",
    scope="core",
    component="call_manager",
)
register_exposed_var(
    "CALL_SCREEN_FRAME_MIN_INTERVAL_SEC",
    label="Screen-share Frame Interval (s)",
    default=2.0,
    value_type=float,
    ui_type="number",
    description=(
        "Minimum seconds between accepted screen-share frames in a call; frames "
        "arriving faster are dropped (only the latest frame is ever kept)."
    ),
    scope="core",
    component="call_manager",
    advanced=True,
)
register_exposed_var(
    "CALL_SCREEN_MAX_EDGE_PX",
    label="Screen-share Max Edge (px)",
    default=1280,
    value_type=int,
    ui_type="number",
    description="Screen-share frames are downscaled so their longest edge is at most this.",
    scope="core",
    component="call_manager",
    advanced=True,
)


#: Shown to the model on a call turn (``source.in_call.note``), so it knows it is on a call.
CALL_NOTE = (
    "this is a live call, not a chat: the person hears you, and what you write is spoken "
    "aloud (the system speaks it for you, whatever send_as_voice says). Answer the way you "
    "would on the phone: short, plain sentences, no markdown, lists, links or emoji. Their "
    "words come from speech-to-text, so they can be cut off, mis-heard, or one thought "
    "split over several messages: if one ends mid-thought, wait for the rest instead of "
    "answering the fragment."
)

#: Minimum seconds between two acknowledgements in the same call. Also long enough
#: for the signal not to be answered by its own echo in the caller's microphone.
_ACK_MIN_GAP_S = 3.0


class CallError(Exception):
    """A call could not be started or driven (provider missing, disabled, …)."""


class CallKind(str, Enum):
    AUDIO = "audio"
    VIDEO = "video"


class CallState(str, Enum):
    ACTIVE = "active"
    ENDED = "ended"


@dataclass(frozen=True)
class CallCapabilities:
    """What a provider can host. Audio is the baseline of any call."""

    audio: bool = True
    video: bool = False
    screen_share: bool = False


@dataclass
class CallSession:
    """One live call."""

    call_id: str
    interface_name: str
    interface_path: str
    kind: CallKind
    requested_kind: CallKind
    user_id: str | None = None
    languages: tuple[str, ...] = ()
    state: CallState = CallState.ACTIVE
    started_at: float = field(default_factory=time.time)
    utterances: int = 0
    # Outcome of the latest acknowledgement attempt ("played", "not_ready",
    # "disabled", "debounced", "playback_failed", "no_provider"): lets a caller
    # (and the log) say why no "Hmm" was heard.
    last_ack: str = ""
    # Screen share: only the most recent accepted frame (JPEG bytes) is kept.
    latest_screen_frame: bytes | None = None
    _frame_hash: str = ""
    _frame_at: float = 0.0
    _last_ack_at: float = 0.0

    @property
    def degraded(self) -> bool:
        """True when a video call was downgraded to audio by the provider."""
        return self.requested_kind != self.kind


class CallProvider(ABC):
    """An interface's call hosting. Register with :meth:`CallManager.register_provider`."""

    #: Interface name this provider serves (matches the interface registry name).
    name: str = ""
    capabilities: CallCapabilities = CallCapabilities()

    @abstractmethod
    async def play_audio(self, session: CallSession, path: Path) -> None:
        """Play a local audio clip to the caller (e.g. the acknowledgement)."""

    #: True when the SyntH's spoken replies are played into the call by this provider
    #: (``play_speech``). False for the WebUI, where the browser plays them (Karada).
    plays_speech_in_call: bool = False

    async def play_speech(self, session: CallSession, path: Path) -> None:
        """Play a spoken reply of the SyntH into the call. Defaults to :meth:`play_audio`."""
        await self.play_audio(session, path)

    async def deliver_turn(
        self,
        session: CallSession,
        text: str,
        speaker_id: str,
        speaker_name: str,
    ) -> None:
        """Hand what the caller said (already transcribed) to the message chain as a call turn.

        Providers fed by :class:`core.call_audio.CallAudioPipeline` implement this: build
        the interface's own message for ``text``, run it through
        :meth:`CallManager.mark_turn` and enqueue it.
        """
        raise NotImplementedError

    def is_agent_speaking(self, session: CallSession) -> bool:
        """True while the SyntH's own voice is playing to the caller (no signal then)."""
        return False

    async def on_call_started(self, session: CallSession) -> None:  # noqa: B027
        """Hook: a call began on this provider."""

    async def on_call_ended(self, session: CallSession) -> None:  # noqa: B027
        """Hook: a call ended on this provider."""


def _downscale_jpeg(data: bytes, max_edge: int) -> bytes:
    """Return ``data`` re-encoded so its longest edge is <= ``max_edge``.

    Pass-through when Pillow cannot decode it or no resize is needed.
    """
    if max_edge <= 0:
        return data
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            if max(img.size) <= max_edge:
                return data
            img = img.convert("RGB")
            img.thumbnail((max_edge, max_edge))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=80)
            return out.getvalue()
    except Exception as exc:
        log_debug(f"[call] screen frame resize skipped: {exc}")
        return data


class CallManager:
    """Registry of call providers and active calls."""

    def __init__(self) -> None:
        self._providers: dict[str, CallProvider] = {}
        self._sessions: dict[str, CallSession] = {}
        self._by_path: dict[str, str] = {}
        self._background: set[asyncio.Task[Any]] = set()

    # -- providers --------------------------------------------------------

    def register_provider(self, provider: CallProvider) -> None:
        """Register (or replace) the provider for ``provider.name``."""
        if not provider.name:
            raise ValueError("CallProvider.name must be a non-empty string")
        self._providers[provider.name] = provider
        log_info(f"[call] provider registered: {provider.name}")

    def unregister_provider(self, name: str) -> None:
        """Remove a provider; its active calls are ended."""
        self._providers.pop(name, None)
        for session in [s for s in self._sessions.values() if s.interface_name == name]:
            self._drop(session)

    def get_provider(self, name: str) -> CallProvider | None:
        return self._providers.get(name)

    def providers(self) -> dict[str, CallCapabilities]:
        """Registered provider names with their capabilities."""
        return {n: p.capabilities for n, p in self._providers.items()}

    # -- sessions ---------------------------------------------------------

    def is_enabled(self) -> bool:
        return bool(
            config_registry.get_value(
                "CALL_ENABLED", True, value_type=bool, component="call_manager"
            )
        )

    def get_session(self, call_id: str | None) -> CallSession | None:
        return self._sessions.get(call_id) if call_id else None

    def get_active_call(self, interface_path: str) -> CallSession | None:
        return self.get_session(self._by_path.get(interface_path))

    async def request_call(
        self,
        interface_name: str,
        interface_path: str,
        kind: CallKind = CallKind.AUDIO,
        user_id: str | None = None,
        languages: tuple[str, ...] | list[str] | None = None,
        announce: bool = True,
    ) -> CallSession:
        """Start a call on ``interface_path`` and warm the acknowledgement cache.

        Raises :class:`CallError` when calls are disabled or the interface has no
        registered provider. A video call on an audio-only provider degrades to
        audio (see :attr:`CallSession.degraded`). A second request on a path
        that already has a call returns the existing session.
        """
        if not self.is_enabled():
            raise CallError("calls are disabled (CALL_ENABLED)")
        provider = self._providers.get(interface_name)
        if provider is None:
            raise CallError(f"no call provider registered for '{interface_name}'")
        existing = self.get_active_call(interface_path)
        if existing is not None:
            return existing

        effective = kind
        if kind is CallKind.VIDEO and not provider.capabilities.video:
            effective = CallKind.AUDIO
            log_info(
                f"[call] '{interface_name}' has no video; "
                f"call on {interface_path} degraded to audio"
            )

        session = CallSession(
            call_id=uuid.uuid4().hex,
            interface_name=interface_name,
            interface_path=interface_path,
            kind=effective,
            requested_kind=kind,
            user_id=user_id,
            languages=tuple(languages or ()),
        )
        self._sessions[session.call_id] = session
        self._by_path[interface_path] = session.call_id

        # Warm the "Hmm" cache so the very first turn already has it.
        self._spawn(get_call_ack_cache().prepare(session.languages or None))

        try:
            await provider.on_call_started(session)
        except Exception as exc:
            log_warning(f"[call] provider '{interface_name}' on_call_started: {exc}")
        if announce:
            await self._play_signal(session, "start")
        log_info(
            f"[call] started {session.call_id} ({session.kind.value}) "
            f"on {interface_name} {interface_path}"
        )
        return session

    async def end_call(self, call_id: str) -> bool:
        """End a call. Returns ``False`` when it was not active."""
        session = self._sessions.get(call_id)
        if session is None:
            return False
        provider = self._providers.get(session.interface_name)
        self._drop(session)
        try:
            from core.call_audio import get_call_audio_pipeline

            get_call_audio_pipeline().end(call_id)
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[call] audio pipeline cleanup skipped: {exc}")
        await self._play_signal(session, "end")
        if provider is not None:
            try:
                await provider.on_call_ended(session)
            except Exception as exc:
                log_warning(f"[call] provider '{provider.name}' on_call_ended: {exc}")
        log_info(f"[call] ended {call_id}")
        return True

    def _drop(self, session: CallSession) -> None:
        session.state = CallState.ENDED
        session.latest_screen_frame = None
        self._sessions.pop(session.call_id, None)
        if self._by_path.get(session.interface_path) == session.call_id:
            self._by_path.pop(session.interface_path, None)

    def live_enabled(self) -> bool:
        """Whether calls may use a bidirectional Live engine (off by default)."""
        return bool(
            config_registry.get_value(
                "CALL_LIVE_ENGINE_ENABLED",
                False,
                value_type=bool,
                component="call_manager",
            )
        )

    async def route_speech(self, interface_path: str | None, audio_path: Path) -> bool:
        """Play a spoken reply into the call that owns ``interface_path``, if there is one.

        Returns ``True`` when a call took the audio (the caller then must not also send it
        as a voice message). ``False`` for no call, or a provider that does not play speech
        itself (the WebUI).
        """
        if not interface_path:
            return False
        for session in list(self._sessions.values()):
            own = session.interface_path
            # The call's own chat, or a thread of it.
            if interface_path != own and not interface_path.startswith(own + "/"):
                continue
            provider = self._providers.get(session.interface_name)
            if provider is None or not provider.plays_speech_in_call:
                return False
            try:
                await provider.play_speech(session, audio_path)
            except Exception as exc:
                log_warning(f"[call] spoken reply not played into the call: {exc}")
            return True
        return False

    # -- sounds -----------------------------------------------------------

    async def _play_signal(self, session: CallSession, kind: str) -> None:
        """Play the ``start`` / ``end`` sound of a call through its provider. Never raises."""
        try:
            if not config_registry.get_value(
                "CALL_SOUNDS_ENABLED", True, value_type=bool, component="call_manager"
            ):
                return
            provider = self._providers.get(session.interface_name)
            if provider is None:
                return
            path = await asyncio.to_thread(get_call_ack_cache().get_signal, kind)
            if path is None:
                return
            await provider.play_audio(session, path)
        except Exception as exc:
            log_warning(f"[call] '{kind}' sound not played: {exc}")

    # -- acknowledgement --------------------------------------------------

    async def on_message_transcribed(
        self, call_id: str | None, language: str | None = None
    ) -> bool:
        """The caller's speech was transcribed to text: play the "message received" signal.

        Called only once text actually came out of speech-to-text, so typing, a
        cough or an echo (which transcribe to nothing) never trigger it. Returns
        ``True`` when the signal was played; a no-op (``False``) for an unknown call,
        when the signal is disabled, while the SyntH is speaking, or too soon after
        the previous one.
        """
        session = self.get_session(call_id)
        if session is None:
            return False
        session.utterances += 1
        if not config_registry.get_value(
            "CALL_ACK_ENABLED", True, value_type=bool, component="call_manager"
        ):
            session.last_ack = "disabled"
            return False
        provider = self._providers.get(session.interface_name)
        if provider is None:
            session.last_ack = "no_provider"
            return False
        try:
            if provider.is_agent_speaking(session):
                # The SyntH is talking: the signal would talk over it.
                session.last_ack = "synth_speaking"
                return False
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[call] is_agent_speaking failed: {exc}")
        now = time.monotonic()
        if session._last_ack_at and (now - session._last_ack_at) < _ACK_MIN_GAP_S:
            # VAD can report several speech ends for one utterance; ack once.
            session.last_ack = "debounced"
            return False
        lang = language or (session.languages[0] if session.languages else None)
        clip = get_call_ack_cache().get(lang)
        if clip is None:
            session.last_ack = "not_ready"
            log_info(
                f"[call] {session.call_id}: no acknowledgement played - the clip is "
                "could not be prepared (see the [call_ack] log lines)"
            )
            return False
        session._last_ack_at = now
        try:
            await provider.play_audio(session, clip)
        except Exception as exc:
            session.last_ack = "playback_failed"
            log_warning(f"[call] acknowledgement playback failed: {exc}")
            return False
        session.last_ack = "played"
        log_info(f"[call] {session.call_id}: acknowledgement played ({clip.name})")
        return True

    # -- screen share -----------------------------------------------------

    def submit_screen_frame(self, call_id: str | None, jpeg: bytes) -> bool:
        """Offer a screen-share frame; returns ``True`` if it was kept.

        Dropped when the call is unknown, the provider cannot share screens, the
        frame arrives faster than ``CALL_SCREEN_FRAME_MIN_INTERVAL_SEC``, or it
        is byte-identical to the last accepted frame.
        """
        session = self.get_session(call_id)
        if session is None or not jpeg:
            return False
        provider = self._providers.get(session.interface_name)
        if provider is None or not provider.capabilities.screen_share:
            return False

        now = time.monotonic()
        min_gap = float(
            config_registry.get_value(
                "CALL_SCREEN_FRAME_MIN_INTERVAL_SEC",
                2.0,
                value_type=float,
                component="call_manager",
            )
        )
        if session._frame_at and (now - session._frame_at) < min_gap:
            return False
        digest = hashlib.sha1(jpeg).hexdigest()
        if digest == session._frame_hash:
            return False

        max_edge = int(
            config_registry.get_value(
                "CALL_SCREEN_MAX_EDGE_PX",
                1280,
                value_type=int,
                component="call_manager",
            )
        )
        session.latest_screen_frame = _downscale_jpeg(jpeg, max_edge)
        session._frame_hash = digest
        session._frame_at = now
        return True

    # -- turns ------------------------------------------------------------

    def mark_turn(self, message: Any, call: CallSession | str | None) -> Any:
        """Flag ``message`` as a call turn so the queue ranks it at ``PRIORITY_CALL``.

        In a call the SyntH answers **by voice**: ``request_tts`` is forced on (the
        explicit flag ``message_queue`` copies into the context and ``message_chain``
        honours for every interface, even a voice-only one such as Telegram and even
        while Vox is momentarily off, so the reply never ends up as chat text only)
        and the turn counts as voice input. Also attaches the latest screen-share
        frame, if any. Safe to call with an unknown/ended call (returns the message
        untouched).
        """
        session = call if isinstance(call, CallSession) else self.get_session(call)
        if session is None or session.state is not CallState.ACTIVE:
            return message
        message._call = True
        message.call_id = session.call_id
        message.request_tts = True
        message.is_voice_input = True
        frame = session.latest_screen_frame
        if frame:
            attachments = list(getattr(message, "attachments", None) or [])
            attachments.append(
                {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(frame).decode("ascii"),
                    "filename": "screen.jpg",
                }
            )
            message.attachments = attachments
        return message

    def turn_context(self, call_id: str | None) -> dict[str, Any] | None:
        """What the model is told about the call a turn belongs to, or ``None``.

        Read by the queue into the turn context and rendered into the prompt, so the
        SyntH knows it is in a live call (not a chat) and how it is being heard.
        """
        session = self.get_session(call_id)
        if session is None or session.state is not CallState.ACTIVE:
            return None
        provider = self._providers.get(session.interface_name)
        shares_screen = bool(provider and provider.capabilities.screen_share)
        return {
            "call_id": session.call_id,
            "kind": session.kind.value,
            "interface": session.interface_name,
            "seconds": int(max(0.0, time.time() - session.started_at)),
            "caller_sees_you": session.kind is CallKind.VIDEO,
            "screen_share": shares_screen and session.latest_screen_frame is not None,
        }

    # -- internals --------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


_manager: CallManager | None = None


def get_call_manager() -> CallManager:
    """Return the process-wide call manager."""
    global _manager
    if _manager is None:
        _manager = CallManager()
    return _manager


def reset_call_manager() -> None:
    """Drop the singleton (tests)."""
    global _manager
    _manager = None
