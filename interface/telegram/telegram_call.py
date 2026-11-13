"""Telegram voice calls for the user-account interface (``telegram``).

Telegram's Bot API has no calls at all, so this only exists for the Telethon *user*
account. It uses the py-tgcalls library (``pytgcalls``, which brings the native
``ntgcalls``), installed on first use like the other engine packages
(``core.package_installer``).

How a call works here — the same way as on every interface (``core/call_audio.py``):

    the caller's audio frames -> ``CallAudioPipeline`` (voice detection, Auris)
        -> a normal turn at ``PRIORITY_CALL`` (``deliver_turn``)
    the SyntH's reply is spoken by Vox and played into the call (``play_speech``)

Only the trainer (or ids listed in ``TELEGRAM_CALL_ALLOWED_IDS``) can call: an incoming
call from anyone else is not answered.

NOTE FOR DEBUGGING. This module was written against py-tgcalls' published examples
(``example/p2p_example``: ``ChatUpdate.Status.INCOMING_CALL`` + ``play``; the
``stream_frame`` filter) without a Telegram account to run it on, and the library's
private-call documentation is thin. Every library call is isolated in ``_Lib`` or in a
guarded block and logs the exception with the ``[telegram_call]`` prefix; the two things
most likely to need adjusting are the incoming-audio format
(``TELEGRAM_CALL_RX_RATE`` / ``TELEGRAM_CALL_RX_CHANNELS``) and the names of the end-of-call
statuses (``_ENDED_STATUS_NAMES``). Video is not implemented.
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
        self.Device = getattr(types, "Device", None)

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
        self._provider: TelegramCallProvider | None = None

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
        log_info("[telegram_call] ready: the trainer can call this account")
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
            log_warning(
                f"[telegram_call] call from {chat_id} not answered (not the trainer "
                "and not in TELEGRAM_CALL_ALLOWED_IDS)"
            )
            return
        log_info(f"[telegram_call] answering the call from {chat_id}")
        await self._open(chat_id, answer=True)

    async def _on_ended(self, _client: Any, update: Any) -> None:
        chat_id = int(update.chat_id)
        if chat_id not in self._calls:
            return
        log_info(f"[telegram_call] call with {chat_id} ended ({update.status})")
        session = get_call_manager().get_active_call(self._path(chat_id))
        self._calls.pop(chat_id, None)
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
        # The state exists before the pickup plays, so that tone counts as speech.
        state: dict[str, Any] = {
            "call_id": None,
            "name": await self._display_name(chat_id),
            "speaking_until": 0.0,
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
                CallKind.AUDIO,
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
        return session

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
        try:
            await _maybe_await(
                self._app.play(chat_id, self._lib.MediaStream(str(path)))
            )
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
    """Hosts Telegram private calls (audio only: video is not implemented)."""

    name = INTERFACE_ID
    capabilities = CallCapabilities(audio=True, video=False, screen_share=False)
    plays_speech_in_call = True

    def __init__(self, calls: TelegramCalls) -> None:
        self._calls = calls

    @staticmethod
    def _chat_id(session: CallSession) -> int:
        return int(session.interface_path.split("/")[1])

    async def play_audio(self, session: CallSession, path: Path) -> None:
        await self._calls.play_file(self._chat_id(session), Path(path))

    async def play_speech(self, session: CallSession, path: Path) -> None:
        await self._calls.play_file(self._chat_id(session), Path(path))

    def is_agent_speaking(self, session: CallSession) -> bool:
        return self._calls.is_speaking(self._chat_id(session))

    async def deliver_turn(
        self, session: CallSession, text: str, speaker_id: str, speaker_name: str
    ) -> None:
        await self._calls.deliver_turn(session, text, speaker_id, speaker_name)

    async def on_call_ended(self, session: CallSession) -> None:
        # Let the hang-up tone play before leaving the call.
        await asyncio.sleep(0.6)
        await self._calls._leave(self._chat_id(session))
