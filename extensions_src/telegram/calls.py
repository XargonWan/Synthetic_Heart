"""Voice (and video) calls for the Telegram user account, with py-tgcalls.

The Core owns the call: the session, the detection and transcription of speech, the
spoken reply and the SyntH's face. This module only carries the sound and the picture
(see ``core/extensions/calls.py`` for the other side):

    caller's audio frames -> ``ctx.calls.audio`` -> the Core (voice detection, STT)
    the SyntH's reply     <- ``call.play``       <- the Core (Vox)
    the SyntH's face      <- ``ctx.calls.video_frame`` pushed into the call

The trainer (or an id in ``TELEGRAM_CALL_ALLOWED_IDS``) is answered at once. A call from
anyone else is not answered: the trainer is told who is calling, and the SyntH answers or
declines with the ``answer_call_telegram`` action once the trainer has said what to do.

NOTE. Like the module it replaces, this was written against py-tgcalls' published
examples (``INCOMING_CALL`` + ``play``, the ``stream_frame`` filter, ``record``) without a
Telegram account to run it on. Every library call is isolated in :class:`Lib` or in a
guarded block and logs the exception; the two things most likely to need adjusting are the
incoming-audio format (``TELEGRAM_CALL_RX_RATE`` / ``TELEGRAM_CALL_RX_CHANNELS``) and the
names of the end-of-call statuses (``ENDED_STATUS_NAMES``).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import shutil
import time
import wave
from pathlib import Path
from typing import Any

from synth_sdk.runtime import Context

#: How long an unanswered call from a stranger is still worth answering.
RING_TIMEOUT = 45.0
#: Video pump limits: how long to wait for the first frame, how old a frame may get
#: before it is reported, and how many send failures in a row end the video.
VIDEO_FIRST_FRAME = 8.0
VIDEO_STALE = 5.0
VIDEO_MAX_FAILURES = 5
#: Audio goes to the Core in chunks of about this many seconds (the gateway rate-limits).
AUDIO_CHUNK_SECONDS = 0.1
#: ``ChatUpdate.Status`` members that mean the call is over, resolved by name because the
#: library's set of statuses is not documented. Unknown names are skipped.
ENDED_STATUS_NAMES = (
    "CLOSED_VOICE_CHAT",
    "LEFT_CALL",
    "KICKED",
    "DISCARDED_CALL",
    "BUSY_CALL",
    "CALL_DECLINED",
    "CALL_ENDED",
)


async def maybe_await(value: Any) -> Any:
    """py-tgcalls' calls are coroutines in some versions and plain calls in others."""
    return await value if inspect.isawaitable(value) else value


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return 0.0


def write_silence(path: Path, seconds: float = 0.3, rate: int = 16000) -> None:
    """A short silent WAV: a stream to answer a call with when the Core sent no tone."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(seconds * rate))


class Lib:
    """The pieces of py-tgcalls this module uses, resolved defensively."""

    def __init__(self) -> None:
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
        self.Direction = getattr(types, "Direction", None)
        self.Device: Any = getattr(types, "Device", None)
        # py-tgcalls >= 3 delivers the caller's audio only after ``record(...)``.
        self.RecordStream: Any = getattr(types, "RecordStream", None)
        try:
            from pytgcalls.types.raw import AudioParameters  # type: ignore[import-not-found]

            self.AudioParameters: Any = AudioParameters
        except Exception:  # noqa: BLE001
            self.AudioParameters = None
        # Optional: only needed to show video (external frames, py-tgcalls >= 3).
        self.ExternalMedia: Any = getattr(types, "ExternalMedia", None)
        self.Frame: Any = getattr(types, "Frame", None)
        try:
            from pytgcalls.types.raw import VideoParameters  # type: ignore[import-not-found]

            self.VideoParameters: Any = VideoParameters
        except Exception:  # noqa: BLE001
            self.VideoParameters = None

    @property
    def video_supported(self) -> bool:
        return bool(
            self.ExternalMedia is not None
            and self.VideoParameters is not None
            and self.Frame is not None
            and self.Device is not None
            and hasattr(self.Device, "CAMERA")
            and hasattr(self.ExternalMedia, "VIDEO")
        )

    @property
    def can_record(self) -> bool:
        return self.RecordStream is not None and self.AudioParameters is not None

    def record_stream(self, rate: int, channels: int) -> Any:
        return self.RecordStream(
            audio=True, audio_parameters=self.AudioParameters(rate, channels)
        )

    def audio_stream(self, path: Path) -> Any:
        return self.MediaStream(str(path))

    def video_stream(self, path: Path, video: dict[str, int]) -> Any:
        """``path`` as the audio, plus an external (frame-by-frame) camera."""
        return self.MediaStream(
            self.ExternalMedia.VIDEO,
            audio_path=str(path),
            video_parameters=self.VideoParameters(
                video["width"], video["height"], video["fps"]
            ),
        )

    def frame_info(self, video: dict[str, int]) -> Any:
        return self.Frame.Info(int(time.time() * 1000), video["width"], video["height"])

    def ended_flags(self) -> Any:
        flags = None
        status = self.ChatUpdate.Status
        for name in ENDED_STATUS_NAMES:
            member = getattr(status, name, None)
            if member is not None:
                flags = member if flags is None else (flags | member)
        return flags

    def frame_filter(self) -> Any:
        kwargs: dict[str, Any] = {}
        if self.Direction is not None and hasattr(self.Direction, "INCOMING"):
            kwargs["directions"] = self.Direction.INCOMING
        if self.Device is not None and hasattr(self.Device, "MICROPHONE"):
            devices = self.Device.MICROPHONE
            if hasattr(self.Device, "SPEAKER"):
                devices = devices | self.Device.SPEAKER
            kwargs["devices"] = devices
        try:
            return self.filters.stream_frame(**kwargs)
        except TypeError:
            return self.filters.stream_frame()


class TelegramCalls:
    """The Telegram call engine: py-tgcalls on the account's Telethon client."""

    def __init__(self, ctx: Context, interface_id: str, lib: Any = None) -> None:
        self.ctx = ctx
        self.interface_id = interface_id
        self._lib: Any = lib
        self._app: Any = None
        self._client: Any = None
        #: chat_id -> call state
        self._calls: dict[int, dict[str, Any]] = {}
        #: Calls from users who may not call (yet): chat_id -> when it started ringing.
        self._ringing: dict[int, float] = {}
        #: chat_id -> the running video pump of that call
        self._video: dict[int, asyncio.Task[None]] = {}
        self._cleanup: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ lifecycle

    async def start(self, client: Any) -> bool:
        """Start py-tgcalls and hook the Core's call callbacks. Never raises."""
        self._client = client
        try:
            self._lib = self._lib or await asyncio.to_thread(Lib)
            lib = self._lib
            self._app = lib.PyTgCalls(client)
            self._app.on_update(
                lib.filters.chat_update(lib.ChatUpdate.Status.INCOMING_CALL)
            )(self._on_incoming)
            ended = lib.ended_flags()
            if ended is not None:
                self._app.on_update(lib.filters.chat_update(ended))(self._on_ended)
            self._app.on_update(lib.frame_filter())(self._on_frames)
            await maybe_await(self._app.start())
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.error(
                f"could not start py-tgcalls: {type(exc).__name__}: {exc}"
            )
            self._app = None
            return False
        self.ctx.calls.on_play(self._on_play)
        self.ctx.calls.on_ended(self._on_core_ended)
        self.ctx.calls.on_video_lost(self._on_video_lost)
        return True

    async def stop(self) -> None:
        for chat_id in list(self._calls):
            await self._leave(chat_id)
        for task in list(self._cleanup):
            task.cancel()
        self._app = None

    def video_available(self) -> bool:
        return bool(
            self._app is not None
            and self._lib is not None
            and self._lib.video_supported
        )

    # -------------------------------------------------------------- who may call

    async def allowed(self, user_id: int) -> bool:
        trainer = await self.ctx.interfaces.trainer(self.interface_id)
        if trainer is not None and str(user_id) == trainer:
            return True
        raw = str(await self.ctx.config.get("TELEGRAM_CALL_ALLOWED_IDS") or "")
        return str(user_id) in {p.strip() for p in raw.split(",") if p.strip()}

    # ------------------------------------------------------------------ incoming

    async def _on_incoming(self, _client: Any, update: Any) -> None:
        chat_id = int(update.chat_id)
        if not await self.allowed(chat_id):
            self.ctx.log.info(
                f"call from {chat_id} is not from the trainer: asking the trainer"
            )
            await self._ask_trainer(chat_id)
            return
        self.ctx.log.info(f"answering the call from {chat_id}")
        await self._open(chat_id, answer=True)

    async def _ask_trainer(self, chat_id: int) -> None:
        """Leave the call ringing and tell the trainer who is calling."""
        self._ringing[chat_id] = time.monotonic()
        try:
            trainer = await self.ctx.interfaces.trainer(self.interface_id)
            if trainer is None or not trainer.lstrip("-").isdigit():
                self.ctx.log.warning("no trainer to ask about the call")
                self._ringing.pop(chat_id, None)
                return
            name = await self._display_name(chat_id)
            await self._client.send_message(
                int(trainer),
                f"\U0001f4de {name} (telegram/{chat_id}) is calling me and is not on the "
                "list of people I answer without asking. Should I pick up? "
                f"It rings for about {int(RING_TIMEOUT)} seconds.",
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.warning(f"could not ask the trainer: {type(exc).__name__}")

    async def answer_pending(self, user_id: int, answer: bool) -> dict[str, str]:
        """Answer or decline a call that is ringing and was held for the trainer."""
        started = self._ringing.pop(int(user_id), None)
        if started is None or (time.monotonic() - started) > RING_TIMEOUT:
            return {"status": "failed", "message": "That call is not ringing any more"}
        if not answer:
            await self._leave(int(user_id))
            return {"status": "success", "message": "Call declined"}
        opened = await self._open(int(user_id), answer=True)
        if opened is None:
            return {"status": "failed", "message": "Call not answered"}
        return {"status": "success", "message": "Call answered"}

    async def _on_ended(self, _client: Any, update: Any) -> None:
        """The platform says the call is over."""
        chat_id = int(update.chat_id)
        self._ringing.pop(chat_id, None)
        state = self._calls.get(chat_id)
        if state is None:
            return
        self.ctx.log.info(
            f"call with {chat_id} ended ({getattr(update, 'status', '')})"
        )
        call_id = state.get("call_id")
        await self._leave(chat_id)
        if call_id:
            with contextlib.suppress(Exception):
                await self.ctx.calls.end(call_id)

    async def _on_core_ended(
        self, interface_id: str, call_id: str, user_id: str | None
    ) -> None:
        """The Core ended the call (the conversation is over, or the operator hung up)."""
        for chat_id, state in list(self._calls.items()):
            if state.get("call_id") == call_id or (
                user_id and str(chat_id) == str(user_id)
            ):
                await asyncio.sleep(0.6)  # let the hang-up tone finish
                await self._leave(chat_id)

    # --------------------------------------------------------------------- audio in

    async def _on_frames(self, _client: Any, update: Any) -> None:
        chat_id = int(update.chat_id)
        state = self._calls.get(chat_id)
        if state is None or not state.get("call_id"):
            return
        lib = self._lib
        direction = getattr(update, "direction", None)
        device = getattr(update, "device", None)
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
                and device
                not in (lib.Device.MICROPHONE, getattr(lib.Device, "SPEAKER", None))
            ):
                return
        rate = int(state["rate"])
        channels = int(state["channels"])
        buffer: bytearray = state["buffer"]
        for frame in getattr(update, "frames", None) or []:
            raw = getattr(frame, "frame", frame)
            if not isinstance(raw, (bytes, bytearray)) or not raw:
                continue
            if not state.get("logged_frame"):
                state["logged_frame"] = True
                self.ctx.log.info(
                    f"first audio frame from {chat_id}: {len(raw)} bytes (assuming {rate} Hz, "
                    f"{channels} ch; set TELEGRAM_CALL_RX_RATE / TELEGRAM_CALL_RX_CHANNELS "
                    "if the call sounds wrong)"
                )
            buffer.extend(raw)
        wanted = int(rate * channels * 2 * AUDIO_CHUNK_SECONDS)
        wanted -= wanted % (2 * channels)
        while len(buffer) >= wanted > 0:
            chunk = bytes(buffer[:wanted])
            del buffer[:wanted]
            try:
                await self.ctx.calls.audio(
                    state["call_id"],
                    chunk,
                    rate=rate,
                    channels=channels,
                    speaker_id=str(chat_id),
                    speaker_name=state.get("name", ""),
                )
            except Exception as exc:  # noqa: BLE001 - a lost chunk must not end the call
                self.ctx.log.debug(f"audio chunk dropped: {type(exc).__name__}")
                return

    # ------------------------------------------------------------- opening a call

    @staticmethod
    def _path(chat_id: int) -> str:
        return f"telegram/{chat_id}"

    async def _open(self, chat_id: int, *, answer: bool) -> dict[str, Any] | None:
        """Open the call: the Core's answering tone is the stream that picks it up."""
        want_video = self.video_available() and bool(
            await self.ctx.config.get("TELEGRAM_CALL_VIDEO_ENABLED")
        )
        rate = int(await self.ctx.config.get("TELEGRAM_CALL_RX_RATE") or 48000)
        channels = (
            2
            if int(await self.ctx.config.get("TELEGRAM_CALL_RX_CHANNELS") or 1) == 2
            else 1
        )
        state: dict[str, Any] = {
            "call_id": None,
            "name": await self._display_name(chat_id),
            "speaking_until": 0.0,
            "video": None,
            "played": False,
            "rate": rate,
            "channels": channels,
            "buffer": bytearray(),
        }
        self._calls[chat_id] = state
        try:
            opened = await self.ctx.calls.open(
                self._path(chat_id),
                str(chat_id),
                kind="video" if want_video else "audio",
                announce=True,
            )
            state["call_id"] = opened["call_id"]
            if not state["played"]:
                # The Core sent no tone: still answer with something playable.
                silence = self.ctx.inbox_path("silence.wav")
                write_silence(silence)
                await self._play(chat_id, silence, opened.get("video"))
                silence.unlink(missing_ok=True)
            await self._start_listening(chat_id)
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.error(f"could not open the call: {type(exc).__name__}: {exc}")
            await self._leave(chat_id)
            return None
        video = opened.get("video")
        if video and state["video"] is not None:
            self._video[chat_id] = asyncio.ensure_future(
                self._pump_video(chat_id, video)
            )
            self.ctx.log.info(
                f"call with {chat_id} has video ({video['width']}x{video['height']} @ {video['fps']} fps)"
            )
        else:
            state["video"] = None
        return dict(opened)

    async def _start_listening(self, chat_id: int) -> None:
        """Ask py-tgcalls for the caller's voice. Without this a >= 3 call is deaf."""
        lib, app = self._lib, self._app
        if lib is None or app is None or not lib.can_record:
            return
        state = self._calls[chat_id]
        for attempt in (1, 2):
            try:
                await maybe_await(
                    app.record(
                        chat_id, lib.record_stream(state["rate"], state["channels"])
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.ctx.log.warning(
                    f"could not listen to {chat_id} (attempt {attempt}/2): {type(exc).__name__}"
                )
                if attempt == 1:
                    await asyncio.sleep(1.0)
                continue
            return

    async def _display_name(self, chat_id: int) -> str:
        try:
            entity = await self._client.get_entity(chat_id)
            parts = [
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            ]
            return " ".join(p for p in parts if p) or str(chat_id)
        except Exception:  # noqa: BLE001
            return str(chat_id)

    async def dial(self, user_id: int) -> dict[str, str]:
        """Place a call to ``user_id`` (best effort: depends on py-tgcalls' private calls)."""
        if self._app is None:
            return {"status": "failed", "message": "Telegram calls are not available"}
        if not await self.allowed(int(user_id)):
            return {"status": "failed", "message": "That user is not allowed to call"}
        if await self._open(int(user_id), answer=False) is None:
            return {"status": "failed", "message": "Call not started"}
        return {"status": "success", "message": f"Calling {user_id}"}

    async def hangup(self, user_id: int) -> dict[str, str]:
        state = self._calls.get(int(user_id))
        if state is None or not state.get("call_id"):
            return {"status": "success", "message": "No call with that user"}
        call_id = state["call_id"]
        await self._leave(int(user_id))
        with contextlib.suppress(Exception):
            await self.ctx.calls.end(call_id)
        return {"status": "success", "message": "Call ended"}

    async def _leave(self, chat_id: int) -> None:
        await self._stop_video(chat_id)
        self._calls.pop(chat_id, None)
        if self._app is None:
            return
        try:
            await maybe_await(self._app.leave_call(chat_id))
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.debug(f"leave_call({chat_id}) failed: {type(exc).__name__}")

    # ---------------------------------------------------------------------- audio out

    async def _on_play(
        self,
        interface_id: str,
        call_id: str,
        user_id: str | None,
        path: Path,
        speech: bool,
        video: dict[str, int] | None = None,
    ) -> float:
        """The Core wants this file played into a call."""
        chat_id = self._chat_of(call_id, user_id)
        if chat_id is None:
            return 0.0
        # The Core deletes ``path`` when this handler returns, but playback goes on:
        # keep our own copy until it has finished.
        keep = self.ctx.inbox_path("play.wav")
        shutil.copyfile(path, keep)
        seconds = await self._play(chat_id, keep, video)
        task = asyncio.ensure_future(self._delete_later(keep, seconds + 30.0))
        self._cleanup.add(task)
        task.add_done_callback(self._cleanup.discard)
        return seconds

    def _chat_of(self, call_id: str, user_id: str | None) -> int | None:
        for chat_id, state in self._calls.items():
            if state.get("call_id") == call_id:
                return chat_id
        if (
            user_id
            and str(user_id).lstrip("-").isdigit()
            and int(user_id) in self._calls
        ):
            return int(user_id)  # the tone that answers arrives before open() returns
        return None

    @staticmethod
    async def _delete_later(path: Path, delay: float) -> None:
        await asyncio.sleep(delay)
        path.unlink(missing_ok=True)

    async def _play(
        self, chat_id: int, path: Path, video: dict[str, int] | None
    ) -> float:
        """Play a WAV into the call (it replaces whatever was playing)."""
        lib, app = self._lib, self._app
        state = self._calls.get(chat_id)
        if app is None or lib is None or state is None:
            # No call with this chat: py-tgcalls' play() on a chat that is not in a call
            # starts a new outgoing call, which would ring the person back right after
            # they hung up (the end tone, a reply still being generated).
            return 0.0
        if video is not None and state["video"] is None and lib.video_supported:
            state["video"] = video
        try:
            stream = (
                lib.video_stream(path, state["video"])
                if state["video"] is not None
                else lib.audio_stream(path)
            )
            await maybe_await(app.play(chat_id, stream))
        except Exception as exc:  # noqa: BLE001
            self.ctx.log.warning(
                f"could not play {path.name}: {type(exc).__name__}: {exc}"
            )
            return 0.0
        state["played"] = True
        seconds = wav_seconds(path)
        state["speaking_until"] = time.monotonic() + seconds
        return seconds

    # ----------------------------------------------------------------------- video

    async def _pump_video(self, chat_id: int, video: dict[str, int]) -> None:
        """Push the newest face frame into the call, ``fps`` times a second."""
        state = self._calls.get(chat_id)
        if state is None:
            return
        interval = 1.0 / max(1, int(video["fps"]))
        started = time.monotonic()
        sent_seq = -1
        failures = 0
        warned = False
        while True:
            await asyncio.sleep(interval)
            lib, app, state = self._lib, self._app, self._calls.get(chat_id)
            if lib is None or app is None or state is None or not state.get("call_id"):
                return
            try:
                latest = await self.ctx.calls.video_frame(state["call_id"])
            except Exception as exc:  # noqa: BLE001
                self.ctx.log.debug(f"video frame not fetched: {type(exc).__name__}")
                continue
            if latest is None:
                if not warned and time.monotonic() - started > VIDEO_FIRST_FRAME:
                    warned = True
                    self.ctx.log.warning(
                        f"no video frame after {int(VIDEO_FIRST_FRAME)} s: is the avatar rendering?"
                    )
                continue
            if latest["seq"] == sent_seq:
                continue
            sent_seq = latest["seq"]
            try:
                await maybe_await(
                    app.send_frame(
                        chat_id,
                        lib.Device.CAMERA,
                        latest["frame"],
                        lib.frame_info(video),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failures += 1
                self.ctx.log.warning(
                    f"send_frame failed ({failures}/{VIDEO_MAX_FAILURES}): {type(exc).__name__}"
                )
                if failures >= VIDEO_MAX_FAILURES:
                    self._degrade(chat_id, "py-tgcalls kept refusing frames")
                    return
                continue
            failures = 0

    async def _on_video_lost(self, call_id: str) -> None:
        for chat_id, state in self._calls.items():
            if state.get("call_id") == call_id:
                await self._stop_video(chat_id)
                self._degrade(chat_id, "the face source is gone")

    def _degrade(self, chat_id: int, reason: str) -> None:
        state = self._calls.get(chat_id)
        if state is not None:
            state["video"] = None
        self.ctx.log.warning(f"call with {chat_id} is audio-only now: {reason}")

    async def _stop_video(self, chat_id: int) -> None:
        task = self._video.pop(chat_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
