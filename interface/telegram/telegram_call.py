"""Telegram voice calls for the user-account interface (``telegram``).

Telegram's Bot API has no calls at all, so this only exists for the Telethon *user*
account. It uses the py-tgcalls library (``pytgcalls``, which brings the native
``ntgcalls``), installed on first use like the other engine packages
(``core.package_installer``).

How a call works here — the same way as on every interface (``core/call_audio.py``):

    the caller's audio frames -> ``CallAudioPipeline`` (voice detection, Auris)
        -> a normal turn at ``PRIORITY_CALL`` (``deliver_turn``)
    the SyntH's reply is spoken by Vox and played into the call (``play_speech``)

The trainer (or an id listed in ``TELEGRAM_CALL_ALLOWED_IDS``) is answered at once. A call
from anyone else is not answered: the trainer is told who is calling, and the SyntH answers
(or declines) with the ``answer_call_telegram`` action once the trainer has said what to do.
The call only rings for a short while (``_RING_TIMEOUT_S``).

NOTE FOR DEBUGGING. This module was written against py-tgcalls' published examples
(``example/p2p_example``: ``ChatUpdate.Status.INCOMING_CALL`` + ``play``; the
``stream_frame`` filter) without a Telegram account to run it on, and the library's
private-call documentation is thin. Every library call is isolated in ``_Lib`` or in a
guarded block and logs the exception with the ``[telegram_call]`` prefix; the two things
most likely to need adjusting are the incoming-audio format
(``TELEGRAM_CALL_RX_RATE`` / ``TELEGRAM_CALL_RX_CHANNELS``) and the names of the end-of-call
statuses (``_ENDED_STATUS_NAMES``).

Video: the SyntH's face is drawn by ``core.avatar_render`` (a small headless renderer of the
active VRM model on the server; no browser) or, with ``CALL_VIDEO_SOURCE=browser``, comes
from an open WebUI tab (``core.call_video``). An answered call then starts with audio *and*
video: frames are converted to I420 when needed and pushed into the call with py-tgcalls'
external-frame API (``MediaStream(ExternalMedia.VIDEO, ...)`` +
``send_frame(chat_id, Device.CAMERA, ...)``; checked against py-tgcalls 3.0.0's code, not on
a live call). With no source, or if it stops, the call is audio-only
(``CallSession.degraded``) and says so with a ``[telegram_call]`` log line.
"""

from __future__ import annotations

import asyncio
import audioop
import datetime
import inspect
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.call_audio import get_call_audio_pipeline
from core.call_video import VideoSettings, get_call_video_hub, jpeg_to_i420
from core.call_manager import (
    CallCapabilities,
    CallError,
    CallKind,
    CallProvider,
    CallSession,
    get_call_manager,
)
from core.config_manager import config_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.variables_engine import register_exposed_var

INTERFACE_ID = "telegram"

register_exposed_var(
    "TELEGRAM_CALLS_ENABLED",
    label="Telegram Calls",
    default=True,
    value_type=bool,
    ui_type="bool",
    description=(
        "Let the Telegram user account answer voice calls (py-tgcalls is installed on "
        "first use). Only the trainer and TELEGRAM_CALL_ALLOWED_IDS are answered."
    ),
    scope="interface",
    component="telegram",
)
register_exposed_var(
    "TELEGRAM_CALL_ALLOWED_IDS",
    label="Telegram Call Allowed Users",
    default="",
    value_type=str,
    ui_type="string",
    description="Comma-separated Telegram user ids, besides the trainer, whose calls are answered.",
    scope="interface",
    component="telegram",
    advanced=True,
)
register_exposed_var(
    "TELEGRAM_CALL_RX_RATE",
    label="Telegram Call Incoming Sample Rate",
    default=48000,
    value_type=int,
    ui_type="number",
    description="Sample rate (Hz) of the raw audio frames py-tgcalls delivers from the caller.",
    scope="interface",
    component="telegram",
    advanced=True,
)
register_exposed_var(
    "TELEGRAM_CALL_RX_CHANNELS",
    label="Telegram Call Incoming Channels",
    default=1,
    value_type=int,
    ui_type="number",
    description="Channels (1 or 2) of the raw audio frames py-tgcalls delivers from the caller.",
    scope="interface",
    component="telegram",
    advanced=True,
)

register_exposed_var(
    "TELEGRAM_CALL_VIDEO_ENABLED",
    label="Telegram Call Video",
    default=True,
    value_type=bool,
    ui_type="bool",
    description=(
        "Show the SyntH's face (the Karada render) in answered Telegram calls. It needs a "
        "WebUI tab with the avatar open for the whole call; without one the call is audio-only. "
        "Frame rate and size: CALL_VIDEO_FPS / CALL_VIDEO_WIDTH / CALL_VIDEO_HEIGHT."
    ),
    scope="interface",
    component="telegram",
)

#: ``ChatUpdate.Status`` members that mean the call is over; resolved by name because
#: the library's set of statuses is not documented. Unknown names are skipped.
_ENDED_STATUS_NAMES = (
    "CLOSED_VOICE_CHAT",
    "LEFT_CALL",
    "KICKED",
    "DISCARDED_CALL",
    "BUSY_CALL",
    "CALL_DECLINED",
    "CALL_ENDED",
)


#: How long an unanswered call from a stranger is still worth answering (Telegram rings
#: about this long before the caller gives up).
_RING_TIMEOUT_S = 45.0

#: Video pump limits: how long to wait for the browser's first frame / how old a frame may
#: get before it is reported, and how many send failures in a row end the video.
_VIDEO_FIRST_FRAME_S = 8.0
_VIDEO_STALE_S = 5.0
_VIDEO_MAX_FAILURES = 5


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(config_registry.get_value(key, default, value_type=int))
    except (TypeError, ValueError):
        return default


async def _maybe_await(value: Any) -> Any:
    """py-tgcalls' calls are coroutines in some versions and plain calls in others."""
    return await value if inspect.isawaitable(value) else value


def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate() or 1)
    except Exception:
        return 0.0


class _Lib:
    """The pieces of py-tgcalls this module uses, resolved defensively."""

    def __init__(self) -> None:
        from core.package_installer import ensure_package

        if not ensure_package("pytgcalls"):
            raise RuntimeError(
                "py-tgcalls is not installed and could not be installed "
                "(see the [package_installer] log lines)"
            )
        import pytgcalls  # type: ignore[import-not-found]
        from pytgcalls import PyTgCalls  # type: ignore[import-not-found]

        self.PyTgCalls = PyTgCalls
        self.filters: Any = getattr(pytgcalls, "filters", None)
        if self.filters is None:
            from pytgcalls import filters as _filters  # type: ignore[import-not-found]

            self.filters = _filters
        types = __import__("pytgcalls.types", fromlist=["*"])
        self.ChatUpdate = types.ChatUpdate
        self.MediaStream = types.MediaStream
        # Optional: used to narrow the frame filter; frames are re-checked by hand.
        self.Direction = getattr(types, "Direction", None)
        self.Device: Any = getattr(types, "Device", None)
        # Optional: only needed to show video (external frames, py-tgcalls >= 3).
        self.ExternalMedia: Any = getattr(types, "ExternalMedia", None)
        self.Frame: Any = getattr(types, "Frame", None)
        try:
            from pytgcalls.types.raw import VideoParameters  # type: ignore[import-not-found]

            self.VideoParameters: Any = VideoParameters
        except Exception as exc:
            log_debug(f"[telegram_call] no VideoParameters in this py-tgcalls: {exc!r}")
            self.VideoParameters = None

    @property
    def video_supported(self) -> bool:
        """Does the installed py-tgcalls take external video frames?"""
        return bool(
            self.ExternalMedia is not None
            and self.VideoParameters is not None
            and self.Frame is not None
            and self.Device is not None
            and hasattr(self.Device, "CAMERA")
            and hasattr(self.ExternalMedia, "VIDEO")
        )

    def video_stream(self, path: Path, video: VideoSettings) -> Any:
        """``path`` as the audio, plus an external (frame-by-frame) camera."""
        return self.MediaStream(
            self.ExternalMedia.VIDEO,
            audio_path=str(path),
            video_parameters=self.VideoParameters(video.width, video.height, video.fps),
        )

    def frame_info(self, video: VideoSettings) -> Any:
        return self.Frame.Info(int(time.time() * 1000), video.width, video.height)

    def ended_flags(self) -> Any:
        flags = None
        status = self.ChatUpdate.Status
        for name in _ENDED_STATUS_NAMES:
            member = getattr(status, name, None)
            if member is not None:
                flags = member if flags is None else (flags | member)
        return flags

    def frame_filter(self) -> Any:
        kwargs: dict[str, Any] = {}
        if self.Direction is not None and hasattr(self.Direction, "INCOMING"):
            kwargs["directions"] = self.Direction.INCOMING
        if self.Device is not None and hasattr(self.Device, "MICROPHONE"):
            kwargs["devices"] = self.Device.MICROPHONE
        try:
            return self.filters.stream_frame(**kwargs)
        except TypeError:
            return self.filters.stream_frame()


class TelegramCalls:
    """The Telegram call engine: py-tgcalls on the interface's Telethon client."""

    def __init__(self, interface: Any) -> None:
        self._interface = interface
        self._lib: _Lib | None = None
        self._app: Any = None
        # chat_id -> call state
        self._calls: dict[int, dict[str, Any]] = {}
        # Calls from users who may not call (yet): chat_id -> when it started ringing.
        self._ringing: dict[int, float] = {}
        self._provider: TelegramCallProvider | None = None
        # chat_id -> the running video of that call (its pump task, feed and settings)
        self._video: dict[int, dict[str, Any]] = {}

    # -- lifecycle --------------------------------------------------------

    async def start(self, client: Any) -> bool:
        """Start py-tgcalls and register the provider. Never raises; returns success."""
        if not bool(
            config_registry.get_value(
                "TELEGRAM_CALLS_ENABLED", True, value_type=bool, component="telegram"
            )
        ):
            log_info("[telegram_call] calls are disabled (TELEGRAM_CALLS_ENABLED)")
            return False
        try:
            # Installing the package can take a while: keep it off the event loop.
            self._lib = await asyncio.to_thread(_Lib)
            lib = self._lib
            self._app = lib.PyTgCalls(client)
            self._app.on_update(
                lib.filters.chat_update(lib.ChatUpdate.Status.INCOMING_CALL)
            )(self._on_incoming)
            ended = lib.ended_flags()
            if ended is not None:
                self._app.on_update(lib.filters.chat_update(ended))(self._on_ended)
            self._app.on_update(lib.frame_filter())(self._on_frames)
            await _maybe_await(self._app.start())
        except Exception as exc:
            log_error(f"[telegram_call] could not start py-tgcalls: {exc!r}")
            self._app = None
            return False
        self._provider = TelegramCallProvider(self)
        get_call_manager().register_provider(self._provider)
        log_info(
            "[telegram_call] ready: the trainer can call this account"
            + (
                " (video when a WebUI tab with the avatar is open)"
                if getattr(lib, "video_supported", False)
                else " (audio only: this py-tgcalls cannot send video)"
            )
        )
        return True

    async def stop(self) -> None:
        get_call_manager().unregister_provider(INTERFACE_ID)
        for chat_id in list(self._calls):
            await self._leave(chat_id)
        self._app = None

    # -- who may call -----------------------------------------------------

    @staticmethod
    def _allowed(user_id: int) -> bool:
        try:
            from interface.telegram.telegram import is_trainer

            if is_trainer(user_id):
                return True
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[telegram_call] trainer check failed: {exc}")
        raw = str(
            config_registry.get_value(
                "TELEGRAM_CALL_ALLOWED_IDS", "", value_type=str, component="telegram"
            )
            or ""
        )
        return str(user_id) in {p.strip() for p in raw.split(",") if p.strip()}

    # -- incoming ---------------------------------------------------------

    async def _on_incoming(self, _client: Any, update: Any) -> None:
        chat_id = int(update.chat_id)
        if not self._allowed(chat_id):
            log_info(
                f"[telegram_call] call from {chat_id} is not from the trainer: "
                "asking the trainer whether to answer"
            )
            await self._ask_trainer(chat_id)
            return
        log_info(f"[telegram_call] answering the call from {chat_id}")
        await self._open(chat_id, answer=True)

    async def _ask_trainer(self, chat_id: int) -> None:
        """Leave the call ringing and tell the trainer who is calling."""
        self._ringing[chat_id] = time.monotonic()
        try:
            from interface.telegram.telegram import get_trainer_id

            trainer = get_trainer_id()
            if trainer is None:
                log_warning("[telegram_call] no trainer to ask about the call")
                self._ringing.pop(chat_id, None)
                return
            name = await self._display_name(chat_id)
            await self._interface.send_message(
                {
                    "text": (
                        f"\U0001f4de {name} (telegram/{chat_id}) is calling me and is not "
                        "on the list of people I answer without asking. Should I pick up? "
                        f"It rings for about {int(_RING_TIMEOUT_S)} seconds."
                    ),
                    "interface_path": f"{INTERFACE_ID}/{trainer}",
                }
            )
        except Exception as exc:
            log_warning(f"[telegram_call] could not ask the trainer: {exc!r}")

    async def answer_pending(self, user_id: int, answer: bool) -> dict[str, str]:
        """Answer or decline a call that is ringing and was held for the trainer's decision."""
        started = self._ringing.pop(int(user_id), None)
        if started is None or (time.monotonic() - started) > _RING_TIMEOUT_S:
            return {"status": "failed", "message": "That call is not ringing any more"}
        if not answer:
            await self._leave(int(user_id))
            log_info(f"[telegram_call] call from {user_id} declined")
            return {"status": "success", "message": "Call declined"}
        log_info(
            f"[telegram_call] answering the call from {user_id} (trainer said yes)"
        )
        session = await self._open(int(user_id), answer=True)
        if session is None:
            return {"status": "failed", "message": "Call not answered"}
        return {"status": "success", "message": "Call answered"}

    async def _on_ended(self, _client: Any, update: Any) -> None:
        chat_id = int(update.chat_id)
        self._ringing.pop(chat_id, None)
        if chat_id not in self._calls:
            return
        log_info(f"[telegram_call] call with {chat_id} ended ({update.status})")
        session = get_call_manager().get_active_call(self._path(chat_id))
        self._calls.pop(chat_id, None)
        await self._stop_video(chat_id)
        if session is not None:
            await get_call_manager().end_call(session.call_id)

    async def _on_frames(self, _client: Any, update: Any) -> None:
        chat_id = int(update.chat_id)
        state = self._calls.get(chat_id)
        if state is None or not state.get("call_id"):
            return
        # Only what the caller says: not our own stream, not a shared screen.
        direction = getattr(update, "direction", None)
        device = getattr(update, "device", None)
        lib = self._lib
        if lib is not None:
            if (
                lib.Direction is not None
                and direction is not None
                and hasattr(lib.Direction, "INCOMING")
                and direction != lib.Direction.INCOMING
            ):
                return
            if (
                lib.Device is not None
                and device is not None
                and hasattr(lib.Device, "MICROPHONE")
                and device != lib.Device.MICROPHONE
            ):
                return
        rate = _cfg_int("TELEGRAM_CALL_RX_RATE", 48000)
        channels = 2 if _cfg_int("TELEGRAM_CALL_RX_CHANNELS", 1) == 2 else 1
        pipeline = get_call_audio_pipeline()
        for frame in getattr(update, "frames", None) or []:
            raw = getattr(frame, "frame", frame)
            if not isinstance(raw, (bytes, bytearray)) or not raw:
                continue
            if not state.get("logged_frame"):
                state["logged_frame"] = True
                log_info(
                    f"[telegram_call] first audio frame from {chat_id}: {len(raw)} bytes "
                    f"(assuming {rate} Hz, {channels} ch; set TELEGRAM_CALL_RX_RATE / "
                    "TELEGRAM_CALL_RX_CHANNELS if the call sounds wrong)"
                )
            pcm = bytes(raw)
            try:
                if channels == 2:
                    pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
                if rate != 16000:
                    pcm, state["rs"] = audioop.ratecv(
                        pcm, 2, 1, rate, 16000, state.get("rs")
                    )
            except Exception as exc:
                log_debug(f"[telegram_call] frame dropped: {exc}")
                continue
            pipeline.feed(state["call_id"], pcm, str(chat_id), state.get("name", ""))

    # -- opening and closing a call --------------------------------------

    @staticmethod
    def _path(chat_id: int) -> str:
        return f"{INTERFACE_ID}/{chat_id}"

    async def _open(self, chat_id: int, *, answer: bool) -> CallSession | None:
        """Open the call: answer (or place) it with the pickup tone, then register it."""
        manager = get_call_manager()
        # Video is decided before the first stream: the camera has to be part of the
        # stream that opens the call.
        video = await self._video_settings(chat_id)
        # The state exists before the pickup plays, so that tone counts as speech.
        state: dict[str, Any] = {
            "call_id": None,
            "name": await self._display_name(chat_id),
            "speaking_until": 0.0,
            "video": video,
        }
        self._calls[chat_id] = state
        try:
            from core.call_ack_cache import get_call_ack_cache

            pickup = await asyncio.to_thread(get_call_ack_cache().get_signal, "start")
            # The pickup is the stream that answers: that is why the call is opened
            # with announce=False below (no second pickup).
            if pickup is not None:
                await self.play_file(chat_id, pickup)
            else:  # no signal file: still answer with something playable
                log_warning("[telegram_call] no pickup file; answering without a tone")
            session = await manager.request_call(
                INTERFACE_ID,
                self._path(chat_id),
                CallKind.VIDEO if video is not None else CallKind.AUDIO,
                user_id=str(chat_id),
                announce=False,
            )
        except CallError as exc:
            log_warning(f"[telegram_call] call not opened: {exc}")
            await self._leave(chat_id)
            return None
        except Exception as exc:
            log_error(f"[telegram_call] could not open the call: {exc!r}")
            await self._leave(chat_id)
            return None
        get_call_audio_pipeline().bind_loop(asyncio.get_running_loop())
        state["call_id"] = session.call_id
        if video is not None:
            if session.kind is CallKind.VIDEO and await self._start_video(
                chat_id, session, video
            ):
                log_info(
                    f"[telegram_call] call with {chat_id} has video "
                    f"({video.width}x{video.height} @ {video.fps} fps)"
                )
            else:
                self._degrade_to_audio(chat_id, session, "the video could not start")
        return session

    # -- video ------------------------------------------------------------

    def video_available(self) -> bool:
        """Can a call answered now show the SyntH's face? (read by the provider)"""
        lib = self._lib
        return bool(
            self._app is not None
            and lib is not None
            and getattr(lib, "video_supported", False)
            and bool(
                config_registry.get_value(
                    "TELEGRAM_CALL_VIDEO_ENABLED",
                    True,
                    value_type=bool,
                    component="telegram",
                )
            )
            and get_call_video_hub().video_possible()
        )

    async def _video_settings(self, chat_id: int) -> VideoSettings | None:
        """The video of a call about to be answered, or ``None`` for an audio-only call."""
        try:
            # Whether the server can draw the avatar is learnt once, before it matters.
            await get_call_video_hub().ensure_renderer_probe()
        except Exception as exc:
            log_debug(f"[telegram_call] renderer probe failed: {exc!r}")
        if not self.video_available():
            log_info(
                f"[telegram_call] call with {chat_id} is audio-only: video needs "
                "TELEGRAM_CALL_VIDEO_ENABLED, a py-tgcalls with external frames and a "
                "source for the face (the server renderer: moderngl + Mesa/EGL, or a "
                "WebUI tab with the avatar open; see CALL_VIDEO_SOURCE)"
            )
            return None
        return VideoSettings.from_config()

    async def _start_video(
        self, chat_id: int, session: CallSession, video: VideoSettings
    ) -> bool:
        """Ask the WebUI tab for frames and start pushing them into the call."""
        try:
            feed = await get_call_video_hub().begin(
                session.call_id,
                on_lost=lambda: self._video_lost(chat_id, "the WebUI tab was closed"),
                settings=video,
            )
        except Exception as exc:
            log_error(f"[telegram_call] could not start the camera: {exc!r}")
            return False
        if feed is None:
            log_warning(
                "[telegram_call] no source for the video is available (no renderer, no free WebUI tab)"
            )
            return False
        task = asyncio.ensure_future(self._pump_video(chat_id, feed, video))
        self._video[chat_id] = {"call_id": session.call_id, "task": task}
        return True

    async def _pump_video(self, chat_id: int, feed: Any, video: VideoSettings) -> None:
        """Push the latest browser frame into the call, ``video.fps`` times a second."""
        interval = 1.0 / max(1, video.fps)
        started = time.monotonic()
        sent_seq = -1
        i420 = b""
        failures = 0
        warned_wait = warned_stale = logged_first = False
        while True:
            await asyncio.sleep(interval)
            lib, app = self._lib, self._app
            if lib is None or app is None:
                return
            latest = feed.latest()
            if latest is None:
                if (
                    not warned_wait
                    and time.monotonic() - started > _VIDEO_FIRST_FRAME_S
                ):
                    warned_wait = True
                    log_warning(
                        "[telegram_call] no video frame has arrived "
                        f"after {int(_VIDEO_FIRST_FRAME_S)} s: is the avatar rendering "
                        "(server renderer: see the [avatar_render] lines; browser source: tab "
                        "visible, or the pop-out webcam window open)?"
                    )
                continue
            jpeg, seq = latest
            age = feed.age_s()
            if age is not None and age > _VIDEO_STALE_S:
                if not warned_stale:
                    warned_stale = True
                    log_warning(
                        f"[telegram_call] no new video frame for {int(age)} s (a hidden browser "
                        "tab is throttled by the browser); repeating the last frame"
                    )
            else:
                warned_stale = False
            if seq != sent_seq:
                try:
                    # The server renderer's frames are already I420; a tab's are JPEG.
                    i420 = (
                        jpeg
                        if feed.fmt == "i420"
                        else await asyncio.to_thread(
                            jpeg_to_i420, jpeg, video.width, video.height
                        )
                    )
                except Exception as exc:
                    log_warning(f"[telegram_call] video frame dropped: {exc!r}")
                    sent_seq = seq
                    continue
                sent_seq = seq
            try:
                await _maybe_await(
                    app.send_frame(
                        chat_id, lib.Device.CAMERA, i420, lib.frame_info(video)
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                log_warning(
                    f"[telegram_call] send_frame failed ({failures}/{_VIDEO_MAX_FAILURES}): {exc!r}"
                )
                if failures >= _VIDEO_MAX_FAILURES:
                    await self._video_lost(chat_id, "py-tgcalls kept refusing frames")
                    return
                continue
            failures = 0
            if not logged_first:
                logged_first = True
                log_info(
                    f"[telegram_call] first video frame sent to {chat_id} "
                    f"({len(i420)} bytes I420 {video.width}x{video.height})"
                )

    async def _video_lost(self, chat_id: int, reason: str) -> None:
        """The video cannot go on: the call carries on as an audio call."""
        session = get_call_manager().get_active_call(self._path(chat_id))
        await self._stop_video(chat_id)
        if session is not None:
            self._degrade_to_audio(chat_id, session, reason)

    def _degrade_to_audio(
        self, chat_id: int, session: CallSession, reason: str
    ) -> None:
        state = self._calls.get(chat_id)
        if state is not None:
            state["video"] = None
        if session.kind is CallKind.VIDEO:
            session.kind = (
                CallKind.AUDIO
            )  # the model is told the caller no longer sees it
        log_warning(f"[telegram_call] call with {chat_id} is audio-only now: {reason}")

    async def _stop_video(self, chat_id: int) -> None:
        """Stop pushing frames and tell the WebUI tab to stop capturing (idempotent)."""
        running = self._video.pop(chat_id, None)
        if running is None:
            return
        task = running["task"]
        if task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await get_call_video_hub().end(running["call_id"])
        except Exception as exc:
            log_debug(f"[telegram_call] camera stop failed: {exc!r}")

    async def _display_name(self, chat_id: int) -> str:
        try:
            entity = await self._interface.client.get_entity(chat_id)
            parts = [
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            ]
            return " ".join(p for p in parts if p) or str(chat_id)
        except Exception:
            return str(chat_id)

    async def dial(self, user_id: int) -> dict[str, str]:
        """Place a call to ``user_id`` (best effort: depends on py-tgcalls' private calls)."""
        if self._app is None:
            return {"status": "failed", "message": "Telegram calls are not available"}
        if not self._allowed(int(user_id)):
            return {"status": "failed", "message": "That user is not allowed to call"}
        session = await self._open(int(user_id), answer=False)
        if session is None:
            return {"status": "failed", "message": "Call not started"}
        return {"status": "success", "message": f"Calling {user_id}"}

    async def hangup(self, user_id: int) -> dict[str, str]:
        session = get_call_manager().get_active_call(self._path(int(user_id)))
        if session is None:
            return {"status": "success", "message": "No call with that user"}
        await get_call_manager().end_call(session.call_id)
        return {"status": "success", "message": "Call ended"}

    async def _leave(self, chat_id: int) -> None:
        await self._stop_video(chat_id)
        self._calls.pop(chat_id, None)
        if self._app is None:
            return
        try:
            await _maybe_await(self._app.leave_call(chat_id))
        except Exception as exc:
            log_debug(f"[telegram_call] leave_call({chat_id}) failed: {exc!r}")

    # -- audio out --------------------------------------------------------

    async def play_file(self, chat_id: int, path: Path) -> None:
        """Play a WAV into the call (it replaces whatever was playing)."""
        if self._app is None or self._lib is None:
            return
        state = self._calls.get(chat_id)
        video = state.get("video") if state is not None else None
        try:
            # While the call has video, every stream carries the external camera too.
            stream = (
                self._lib.video_stream(path, video)
                if video is not None
                else self._lib.MediaStream(str(path))
            )
            await _maybe_await(self._app.play(chat_id, stream))
        except Exception as exc:
            log_warning(f"[telegram_call] could not play {path.name}: {exc!r}")
            return
        state = self._calls.get(chat_id)
        if state is not None:
            state["speaking_until"] = time.monotonic() + _wav_seconds(path)

    def is_speaking(self, chat_id: int) -> bool:
        state = self._calls.get(chat_id)
        return bool(state and time.monotonic() < state.get("speaking_until", 0.0))

    # -- a turn ------------------------------------------------------------

    async def deliver_turn(
        self, session: CallSession, text: str, speaker_id: str, speaker_name: str
    ) -> None:
        """What the caller said, as a normal Telegram message at ``PRIORITY_CALL``."""
        from interface.telegram._compat import TgChat, TgMessage, TgUser
        from interface.telegram.telegram import MessageWrapper, _forward_to_queue

        bot = self._interface.bot
        if bot is None:
            return
        chat_id = int(speaker_id)
        now = datetime.datetime.now(datetime.timezone.utc)
        tl = SimpleNamespace(
            id=int(time.time() * 1000) % 1_000_000_000,
            date=now,
            raw_text=text,
            media=None,
            voice=None,
            video_note=None,
            video=None,
            audio=None,
            sticker=None,
            photo=None,
            document=None,
            fwd_from=None,
        )
        first, _, last = (speaker_name or "").partition(" ")
        message = TgMessage(
            bot,
            tl,
            chat=TgChat(id=chat_id, type="private"),
            from_user=TgUser(
                id=chat_id, first_name=first or None, last_name=last or None
            ),
            thread_id=None,
            is_topic_message=False,
        )
        wrapped = MessageWrapper(
            message,
            text=text,
            is_voice_input=True,
            interface_path=session.interface_path,
        )
        get_call_manager().mark_turn(wrapped, session)
        await _forward_to_queue(bot, message, wrapped, directed=True)


class TelegramCallProvider(CallProvider):
    """Hosts Telegram private calls: audio, plus the SyntH's face when a WebUI tab can lend it."""

    name = INTERFACE_ID
    plays_speech_in_call = True

    def __init__(self, calls: TelegramCalls) -> None:
        self._calls = calls

    @property
    def capabilities(self) -> CallCapabilities:
        """``video`` only while a call answered now could really show the SyntH's face."""
        return CallCapabilities(
            audio=True, video=self._calls.video_available(), screen_share=False
        )

    @staticmethod
    def _chat_id(session: CallSession) -> int:
        return int(session.interface_path.split("/")[1])

    async def play_audio(self, session: CallSession, path: Path) -> None:
        await self._calls.play_file(self._chat_id(session), Path(path))

    async def play_speech(self, session: CallSession, path: Path) -> None:
        # The server-drawn face moves its mouth with the voice (no-op without video).
        get_call_video_hub().speak(session.call_id, Path(path))
        await self._calls.play_file(self._chat_id(session), Path(path))

    def is_agent_speaking(self, session: CallSession) -> bool:
        return self._calls.is_speaking(self._chat_id(session))

    async def deliver_turn(
        self, session: CallSession, text: str, speaker_id: str, speaker_name: str
    ) -> None:
        await self._calls.deliver_turn(session, text, speaker_id, speaker_name)

    async def on_call_ended(self, session: CallSession) -> None:
        # Stop the camera at once; the hang-up tone still plays before leaving the call.
        await self._calls._stop_video(self._chat_id(session))
        await asyncio.sleep(0.6)
        await self._calls._leave(self._chat_id(session))
