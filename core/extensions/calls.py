"""Calls for extensions: a call provider that lives in an extension process.

The Core owns the *concept* of a call (``core.call_manager``): the session, the
detection of speech and its transcription (``core.call_audio``), the spoken reply, the
SyntH's face (``core.call_video``). An interface only has to carry the sound and the
picture. For an extension that is this API, so the Core and not the extension decides
what a turn is.

Extension to Core (capability ``calls``):

``calls.provider``
    announce that one of the extension's registered interfaces hosts calls (and
    whether it can carry the SyntH's face, and whether it can play a stream, see below);
``calls.open``
    a call began (answered or placed): the Core starts the session. The tone that
    answers it arrives as a ``call.play`` request before this call returns, so keep the
    extension free to handle it while waiting. The answer says ``mode``: ``classic``
    (the Core listens, answers with a turn and sends the spoken reply as ``call.play``
    files) or ``live`` (a Live engine holds the whole conversation, see below);
``calls.audio``
    what the caller says: raw 16-bit PCM and its rate, any rate, mono or stereo. The
    Core converts it and runs it through the speech pipeline, or, in ``live`` mode, sends
    it to the Live engine;
``calls.video_frame``
    the newest frame of the SyntH's face as planar I420 (``width`` x ``height`` from
    ``calls.open``), or none yet;
``calls.end``
    the call is over on the platform side.

Core to extension: ``call.play`` (play this file into the call; returns ``{"seconds"}``)
and the notifications ``call.ended`` and ``call.video_lost``.

**Live mode.** An interface that announces ``stream=True`` can play audio as it arrives.
When ``CALL_LIVE_ENGINE_ENABLED`` is on and a Live engine is available, ``calls.open``
answers ``mode: "live"`` and the Core sends the model's voice as ``call.stream`` (raw
16-bit mono PCM and its rate, in order, queued after what is already playing) and tells the
extension to drop what it has queued with ``call.flush`` when the caller speaks over the
model. The extension keeps sending ``calls.audio`` exactly as in classic mode. Dialling out and hanging
up on request are ``interface.start_call`` / ``interface.end_call`` (see
``core.extensions.interfaces``).
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from synth_sdk.paths import match_path, parse_interface_path, path_problem
from synth_sdk.rpc import ErrorCode, RpcError

from core.call_manager import (
    CallCapabilities,
    CallError,
    CallKind,
    CallManager,
    CallProvider,
    CallSession,
)
from core.extensions.gateway import ApiMethod, CallContext, Gateway, Param, Session
from core.extensions.interfaces import MessagingPort, RemoteInterface, _owned
from core.extensions.media import MediaBridge

CAP_CALLS = "calls"

#: One frame must fit in a protocol message (1 MiB, base64 inflates by a third).
MAX_FRAME_BYTES = 700_000
#: Raw audio per ``calls.audio`` call (a second of 48 kHz stereo is 192,000 bytes).
MAX_PCM_BYTES = 400_000
PLAY_TIMEOUT = 30.0
_RATES = (8000, 16000, 24000, 32000, 44100, 48000)
_NAME = 120


@dataclass
class CallPorts:
    """The Core services calls rely on, injectable for tests."""

    manager: Callable[[], CallManager]
    pipeline: Callable[[], Any]
    video: Callable[[], Any]
    #: ``(jpeg, width, height) -> i420`` for sources that deliver JPEG.
    jpeg_to_i420: Callable[[bytes, int, int], bytes]
    #: ``() -> (fps, width, height)`` of the call video the operator configured.
    video_settings: Callable[[], Any]
    #: ``() -> LiveCalls``: the calls held by a Live engine, or ``None`` for none.
    live: Callable[[], Any] | None = None


class RemoteSink:
    """The model's voice, handed to the extension that carries the call."""

    def __init__(self, session: Session, interface_id: str, call_id: str) -> None:
        self._session = session
        self._interface_id = interface_id
        self._call_id = call_id

    async def stream(self, pcm: bytes, rate: int) -> None:
        if self._session.peer.closed:
            return
        for start in range(0, len(pcm), MAX_PCM_BYTES):
            part = pcm[start : start + MAX_PCM_BYTES]
            try:
                await self._session.notify(
                    "call.stream",
                    {
                        "interface_id": self._interface_id,
                        "call_id": self._call_id,
                        "pcm": base64.b64encode(part).decode("ascii"),
                        "rate": rate,
                    },
                )
            except RpcError:
                return

    async def flush(self) -> None:
        if self._session.peer.closed:
            return
        try:
            await self._session.notify(
                "call.flush",
                {"interface_id": self._interface_id, "call_id": self._call_id},
            )
        except RpcError:
            return


class RemoteCallProvider(CallProvider):
    """Hosts calls for one interface of one extension."""

    plays_speech_in_call = True

    def __init__(
        self,
        session: Session,
        remote: RemoteInterface,
        *,
        video: bool,
        ports: CallPorts,
        messaging: MessagingPort,
        media: MediaBridge | None,
        stream: bool = False,
    ) -> None:
        self.name = remote.spec.id
        #: The extension can play audio as it arrives (``call.stream``).
        self.streams = stream
        self.capabilities = CallCapabilities(
            audio=True, video=video, screen_share=False
        )
        self._session = session
        self._remote = remote
        self._ports = ports
        self._messaging = messaging
        self._media = media
        self._speaking_until = 0.0
        #: Calls this provider opened; an extension can only drive its own.
        self.calls: set[str] = set()

    # ------------------------------------------------------------- speech

    async def play_audio(self, session: CallSession, path: Path) -> None:
        await self._play(session, path, speech=False)

    async def play_speech(self, session: CallSession, path: Path) -> None:
        # The server-drawn face moves its mouth with the voice (no-op without video).
        self._ports.video().speak(session.call_id, Path(path))
        await self._play(session, path, speech=True)

    async def _play(self, session: CallSession, path: Path, *, speech: bool) -> None:
        if self._media is None or self._session.peer.closed:
            return
        staged = await self._media.stage_core_files(
            self._session.extension_id, [str(path)]
        )
        try:
            if not staged.items:
                return
            result = await self._session.call(
                "call.play",
                {
                    "interface_id": self.name,
                    "call_id": session.call_id,
                    "user_id": session.user_id,
                    "path": staged.items[0]["path"],
                    "speech": speech,
                    "video": self._video_params(session),
                },
                timeout=PLAY_TIMEOUT,
            )
        except RpcError:
            return
        finally:
            await asyncio.to_thread(staged.cleanup)
        seconds = result.get("seconds") if isinstance(result, dict) else None
        if isinstance(seconds, (int, float)) and 0 <= seconds < 600:
            self._speaking_until = time.monotonic() + float(seconds)

    def _video_params(self, session: CallSession) -> dict[str, int] | None:
        """The face's size when this call is, or asked to be, a video call.

        The tone that answers a call is played before ``calls.open`` returns, and the
        camera has to be part of the stream that opens the call, so it is told here."""
        if not self.capabilities.video or session.requested_kind is not CallKind.VIDEO:
            return None
        settings = self._ports.video_settings()
        if settings.width * settings.height * 3 // 2 > MAX_FRAME_BYTES:
            return None
        return {"fps": settings.fps, "width": settings.width, "height": settings.height}

    def is_agent_speaking(self, session: CallSession) -> bool:
        live = self._ports.live() if self._ports.live is not None else None
        bridge = live.get(session.call_id) if live is not None else None
        if bridge is not None and bridge.is_speaking:
            return True
        return time.monotonic() < self._speaking_until

    # --------------------------------------------------------------- turns

    async def deliver_turn(
        self, session: CallSession, text: str, speaker_id: str, speaker_name: str
    ) -> None:
        """What the caller said, as an ordinary message at call priority."""
        from datetime import datetime, timezone

        user = session.user_id or speaker_id
        message = SimpleNamespace(
            message_id=f"call-{int(time.time() * 1000) % 1_000_000_000}",
            chat_id=user,
            interface_path=session.interface_path,
            text=text,
            caption=None,
            date=datetime.now(timezone.utc),
            thread_id=None,
            from_user=SimpleNamespace(
                id=user, username=speaker_name or user, full_name=speaker_name or user
            ),
            chat=SimpleNamespace(
                id=user, type="private", title=speaker_name or user, username=None
            ),
            entities=None,
            reply_to_message=None,
        )
        self._ports.manager().mark_turn(message, session)
        await self._messaging.enqueue(self._remote, message, self.name, True)

    # ----------------------------------------------------------- lifecycle

    async def on_call_ended(self, session: CallSession) -> None:
        self.calls.discard(session.call_id)
        if self._ports.live is not None:
            await self._ports.live().end(session.call_id)
        if self._session.peer.closed:
            return
        try:
            await self._session.notify(
                "call.ended",
                {
                    "interface_id": self.name,
                    "call_id": session.call_id,
                    "user_id": session.user_id,
                },
            )
        except RpcError:
            return


def _provider(ctx: CallContext) -> RemoteCallProvider:
    providers: dict[str, RemoteCallProvider] = ctx.session.state.setdefault("calls", {})
    if not providers:
        raise RpcError(
            ErrorCode.INVALID_PARAMS, "announce calls with calls.provider first"
        )
    return next(iter(providers.values()))


def _own_call(ctx: CallContext, call_id: str) -> RemoteCallProvider:
    provider = _provider(ctx)
    if call_id not in provider.calls:
        raise RpcError(ErrorCode.PERMISSION_DENIED, "that call is not yours")
    return provider


def _to_16k_mono(pcm: bytes, rate: int, channels: int) -> bytes:
    try:
        import audioop  # removed in Python 3.13
    except ImportError:  # pragma: no cover - interpreter without audioop
        raise RpcError(
            ErrorCode.INVALID_PARAMS, "audio conversion is not available"
        ) from None
    if len(pcm) % (2 * channels):
        raise RpcError(ErrorCode.INVALID_PARAMS, "pcm is not whole 16-bit frames")
    if channels == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    if rate != 16000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 16000, None)
    return pcm


def register_call_api(
    gateway: Gateway,
    ports: CallPorts,
    messaging: MessagingPort,
    media: MediaBridge | None,
) -> None:
    """Register ``calls.*`` on the gateway."""

    async def provider(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        interface_id = args["interface_id"]
        remote = _owned(ctx.session).get(interface_id)
        if remote is None or "call" not in remote.spec.capabilities:
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                "register the interface with the 'call' capability first",
            )
        manager = ports.manager()
        existing = ctx.session.state.setdefault("calls", {})
        old = existing.pop(interface_id, None)
        if old is not None:
            manager.unregister_provider(interface_id)
        hosted = RemoteCallProvider(
            ctx.session,
            remote,
            video=bool(args["video"]),
            ports=ports,
            messaging=messaging,
            media=media,
            stream=bool(args["stream"]),
        )
        manager.register_provider(hosted)
        existing[interface_id] = hosted
        return {"ok": True}

    async def open_call(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        hosted = _provider(ctx)
        path = args["path"]
        problem = path_problem(path)
        remote = hosted._remote  # noqa: SLF001
        if problem or parse_interface_path(path)[0] != hosted.name:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"path {problem or 'must start with the interface id'}",
            )
        if not match_path(path, remote.spec.patterns):
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "path does not match a declared pattern"
            )
        languages = args["languages"]
        if languages is not None and not (
            isinstance(languages, list) and all(isinstance(x, str) for x in languages)
        ):
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "languages must be a list of strings"
            )
        manager = ports.manager()
        kind = CallKind.VIDEO if args["kind"] == "video" else CallKind.AUDIO
        try:
            session = await manager.request_call(
                hosted.name,
                path,
                kind,
                user_id=args["user_id"],
                languages=tuple(languages or ()),
                announce=bool(args["announce"]),
            )
        except CallError as exc:
            raise RpcError(ErrorCode.INVALID_PARAMS, str(exc)[:200]) from None
        hosted.calls.add(session.call_id)
        ports.pipeline().bind_loop(asyncio.get_running_loop())
        video: dict[str, int] | None = None
        if session.kind is CallKind.VIDEO:
            video = await _start_video(ctx, hosted, session)
            if video is None:
                session.kind = CallKind.AUDIO
        return {
            "call_id": session.call_id,
            "kind": session.kind.value,
            "degraded": session.degraded,
            "video": video,
            "mode": await _hand_to_live(ctx, hosted, session),
        }

    async def _hand_to_live(
        ctx: CallContext, hosted: RemoteCallProvider, session: CallSession
    ) -> str:
        """``live`` when a Live engine holds this call, else ``classic``."""
        if ports.live is None or not hosted.streams:
            return "classic"
        live = ports.live()
        if live.get(session.call_id) is not None:
            return "live"
        if not ports.manager().live_enabled():
            return "classic"
        manager = ports.manager()

        async def lost(call_id: str) -> None:
            await manager.end_call(call_id)

        bridge = await live.begin(
            session.call_id,
            hosted.name,
            session.interface_path,
            RemoteSink(ctx.session, hosted.name, session.call_id),
            on_lost=lost,
        )
        return "live" if bridge is not None else "classic"

    async def _start_video(
        ctx: CallContext, hosted: RemoteCallProvider, session: CallSession
    ) -> dict[str, int] | None:
        settings = ports.video_settings()

        async def lost() -> None:
            if session.kind is CallKind.VIDEO:
                session.kind = CallKind.AUDIO
            try:
                await ctx.session.notify(
                    "call.video_lost", {"call_id": session.call_id}
                )
            except RpcError:
                return

        try:
            feed = await ports.video().begin(
                session.call_id, on_lost=lost, settings=settings
            )
        except Exception:  # noqa: BLE001 - no video is not a failed call
            return None
        if feed is None or settings.width * settings.height * 3 // 2 > MAX_FRAME_BYTES:
            return None
        return {"fps": settings.fps, "width": settings.width, "height": settings.height}

    async def audio(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        _own_call(ctx, args["call_id"])
        try:
            raw = base64.b64decode(args["pcm"], validate=True)
        except ValueError:
            raise RpcError(ErrorCode.INVALID_PARAMS, "pcm must be base64") from None
        if len(raw) > MAX_PCM_BYTES:
            raise RpcError(ErrorCode.INVALID_PARAMS, "pcm chunk is too large")
        pcm = _to_16k_mono(raw, args["rate"], args["channels"])
        bridge = ports.live().get(args["call_id"]) if ports.live is not None else None
        if bridge is not None:
            await bridge.feed(
                pcm, args["speaker_id"] or "", args["speaker_name"] or ""
            )
            return {"ok": True}
        ports.pipeline().feed(
            args["call_id"],
            pcm,
            args["speaker_id"] or "",
            args["speaker_name"] or "",
        )
        return {"ok": True}

    async def video_frame(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        _own_call(ctx, args["call_id"])
        feed = ports.video().get_feed(args["call_id"])
        latest = feed.latest() if feed is not None else None
        if feed is None or latest is None:
            return {"available": False}
        frame, seq = latest
        if feed.fmt != "i420":
            try:
                frame = await asyncio.to_thread(
                    ports.jpeg_to_i420, frame, feed.settings.width, feed.settings.height
                )
            except Exception:  # noqa: BLE001 - one bad frame is skipped
                return {"available": False}
        if len(frame) > MAX_FRAME_BYTES:
            return {"available": False, "too_large": True}
        return {
            "available": True,
            "seq": seq,
            "width": feed.settings.width,
            "height": feed.settings.height,
            "frame": base64.b64encode(frame).decode("ascii"),
        }

    async def end(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        hosted = _own_call(ctx, args["call_id"])
        hosted.calls.discard(args["call_id"])
        await ports.video().end(args["call_id"])
        if ports.live is not None:
            await ports.live().end(args["call_id"])
        return {"ended": await ports.manager().end_call(args["call_id"])}

    ident = Param("interface_id", max_length=64)
    call = Param("call_id", max_length=64)
    for method in (
        ApiMethod(
            "calls.provider",
            CAP_CALLS,
            (
                ident,
                Param("video", "bool", required=False, default=False),
                Param("stream", "bool", required=False, default=False),
            ),
            provider,
            "Announce that an interface hosts calls.",
        ),
        ApiMethod(
            "calls.open",
            CAP_CALLS,
            (
                Param("path", max_length=512),
                Param("user_id", max_length=_NAME),
                Param(
                    "kind", required=False, default="audio", choices=("audio", "video")
                ),
                Param("languages", "json", required=False, default=None),
                Param("announce", "bool", required=False, default=True),
            ),
            open_call,
            "A call began: start its session.",
        ),
        ApiMethod(
            "calls.audio",
            CAP_CALLS,
            (
                call,
                Param("pcm", max_length=MAX_PCM_BYTES * 2),
                Param("rate", "int", required=False, default=16000, choices=_RATES),
                Param("channels", "int", required=False, default=1, choices=(1, 2)),
                Param("speaker_id", max_length=_NAME, required=False, default=""),
                Param("speaker_name", max_length=_NAME, required=False, default=""),
            ),
            audio,
            "What the caller says.",
        ),
        ApiMethod(
            "calls.video_frame",
            CAP_CALLS,
            (call,),
            video_frame,
            "The newest frame of the SyntH's face.",
        ),
        ApiMethod("calls.end", CAP_CALLS, (call,), end, "The call is over."),
    ):
        gateway.register(method)

    async def close_calls(session: Session) -> None:
        providers: dict[str, RemoteCallProvider] = session.state.get("calls", {})
        manager = ports.manager()
        for name, hosted in list(providers.items()):
            for call_id in list(hosted.calls):
                hosted.calls.discard(call_id)
                await ports.video().end(call_id)  # stop the camera too
                if ports.live is not None:
                    await ports.live().end(call_id)
                await manager.end_call(call_id)
            manager.unregister_provider(name)
        providers.clear()

    gateway.on_attach.append(
        lambda session: session.on_close(lambda: close_calls(session))
    )
