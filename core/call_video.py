"""The SyntH's webcam for calls hosted outside the browser (``/api/call/camera``).

The "face" of the SyntH is the Karada render, drawn only in the browser (the WebUI
canvas, the same one the pop-out webcam shows). The server keeps the avatar *state* but
never produces images. A call that happens on another interface (Telegram's user
account, ...) and wants to show the SyntH's face therefore borrows an open WebUI tab as a
camera:

    browser tab (Karada canvas) --JPEG frames over a WebSocket--> :class:`CallVideoHub`
        --> :class:`CallVideoFeed` (only the latest frame is kept) --> the provider
            (it converts a frame with :func:`jpeg_to_i420` and pushes it into its call)

A browser is not required. By default the server draws the face itself
(``core.avatar_render``: a small headless renderer of the active VRM model, on moderngl/EGL)
and feeds the call from that; a browser tab is the alternative source (``CALL_VIDEO_SOURCE``).

The tab connects to ``/api/call/camera`` as soon as the avatar is on screen and then
sits idle: it costs nothing until a call asks for video. When a call does,
:meth:`CallVideoHub.begin` tells the tab (``{"type": "start", ...}``) to capture at the
configured fps and size, and :meth:`CallVideoHub.end` tells it to stop. A tab that is
not connected means "no camera": the provider then keeps the call audio-only.

Wire protocol (all behind the WebUI auth token, like ``/api/call/screen``):

* server -> tab, text JSON: ``{"type": "start", "call_id", "fps", "width", "height",
  "quality"}`` and ``{"type": "stop", "call_id"}``;
* tab -> server, binary: one JPEG per message. A frame is accepted only from the tab
  that was told to start and only while that call's feed is open.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect

from core import webui_auth
from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning
from core.variables_engine import register_exposed_var

register_exposed_var(
    "CALL_VIDEO_FPS",
    label="Call Video Frame Rate",
    default=12,
    value_type=int,
    ui_type="number",
    description=(
        "Frames per second the WebUI sends to a call that shows the SyntH's face "
        "(the Karada render). Clamped to 1-30."
    ),
    scope="core",
    component="call_video",
    advanced=True,
)
register_exposed_var(
    "CALL_VIDEO_WIDTH",
    label="Call Video Width (px)",
    default=854,
    value_type=int,
    ui_type="number",
    description="Width of the SyntH's call video. Rounded down to an even number, 64-1920.",
    scope="core",
    component="call_video",
    advanced=True,
)
register_exposed_var(
    "CALL_VIDEO_HEIGHT",
    label="Call Video Height (px)",
    default=480,
    value_type=int,
    ui_type="number",
    description="Height of the SyntH's call video. Rounded down to an even number, 64-1080.",
    scope="core",
    component="call_video",
    advanced=True,
)
register_exposed_var(
    "CALL_VIDEO_JPEG_QUALITY",
    label="Call Video JPEG Quality",
    default=0.7,
    value_type=float,
    ui_type="number",
    description="JPEG quality (0.1-1.0) the browser uses for each call video frame.",
    scope="core",
    component="call_video",
    advanced=True,
)

register_exposed_var(
    "CALL_VIDEO_SOURCE",
    label="Call Video Source",
    default="auto",
    value_type=str,
    ui_type="string",
    description=(
        "Where the SyntH's face in a call comes from: 'server' (drawn headless on the "
        "server, no browser needed; needs moderngl and Mesa/EGL), 'browser' (an open WebUI "
        "tab with the avatar) or 'auto' (server, and a tab if the server cannot)."
    ),
    scope="core",
    component="call_video",
    advanced=True,
)
register_exposed_var(
    "CALL_VIDEO_MODEL",
    label="Call Video Model",
    default="",
    value_type=str,
    ui_type="string",
    description=(
        "Path of the .vrm the server draws in calls. Empty: the active avatar, else "
        "skins/Rei/model.vrm."
    ),
    scope="core",
    component="call_video",
    advanced=True,
)

#: A JPEG larger than this is dropped (a 854x480 frame is a few tens of KB).
MAX_FRAME_BYTES = 2 * 1024 * 1024
_JPEG_MAGIC = b"\xff\xd8"


def _cfg(key: str, default: Any, value_type: type) -> Any:
    try:
        return config_registry.get_value(
            key, default, value_type=value_type, component="call_video"
        )
    except Exception:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class VideoSettings:
    """What a call's video looks like: size (always even, as I420 needs) and pace."""

    fps: int
    width: int
    height: int
    quality: float

    @classmethod
    def from_config(cls) -> VideoSettings:
        def _num(key: str, default: int) -> int:
            try:
                return int(_cfg(key, default, int))
            except (TypeError, ValueError):
                return default

        try:
            quality = float(_cfg("CALL_VIDEO_JPEG_QUALITY", 0.7, float))
        except (TypeError, ValueError):
            quality = 0.7
        width = int(_clamp(_num("CALL_VIDEO_WIDTH", 854), 64, 1920)) // 2 * 2
        height = int(_clamp(_num("CALL_VIDEO_HEIGHT", 480), 64, 1080)) // 2 * 2
        return cls(
            fps=int(_clamp(_num("CALL_VIDEO_FPS", 12), 1, 30)),
            width=width,
            height=height,
            quality=round(_clamp(quality, 0.1, 1.0), 2),
        )


class CameraSource:
    """One connected browser tab that can capture the avatar."""

    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        #: The call this tab was told to capture for (``None`` while idle).
        self.call_id: str | None = None

    async def send(self, payload: dict[str, Any]) -> None:
        await self.websocket.send_json(payload)


@dataclass
class CallVideoFeed:
    """The video of one call: its latest frame, from a browser tab or the server renderer.

    ``fmt`` says what the frame is: ``"jpeg"`` (a browser tab's) or ``"i420"`` (planar
    frames of exactly ``settings.width`` x ``settings.height``, from the server renderer).
    """

    call_id: str
    settings: VideoSettings
    source: CameraSource | None
    on_lost: Callable[[], Awaitable[None]] | None = None
    fmt: str = "jpeg"
    #: the server renderer (``AvatarVideoSource``) when there is no tab
    producer: Any = None
    seq: int = 0
    updated_at: float = 0.0
    _jpeg: bytes | None = field(default=None, repr=False)

    def put(self, jpeg: bytes) -> None:
        self._jpeg = jpeg
        self.seq += 1
        self.updated_at = time.monotonic()

    def put_i420(self, frame: bytes) -> None:
        """A frame already in planar I420 (the server renderer's)."""
        self.put(frame)

    def latest(self) -> tuple[bytes, int] | None:
        """The newest frame and its sequence number, or ``None`` before the first one."""
        if self._jpeg is None:
            return None
        return self._jpeg, self.seq

    def age_s(self) -> float | None:
        """Seconds since the last frame arrived (``None`` before the first one)."""
        return None if self._jpeg is None else time.monotonic() - self.updated_at


class CallVideoHub:
    """Camera tabs that are connected, and the call feeds they serve."""

    def __init__(self) -> None:
        self._sources: list[CameraSource] = []
        self._feeds: dict[str, CallVideoFeed] = {}
        #: Can this host render the avatar? ``None`` until probed.
        self._renderer_ok: bool | None = None
        self._renderer_present: bool | None = None

    # -- tabs --------------------------------------------------------------

    def register_source(self, websocket: Any) -> CameraSource:
        source = CameraSource(websocket)
        self._sources.append(source)
        log_info(f"[call_video] camera tab connected ({len(self._sources)} open)")
        return source

    async def unregister_source(self, source: CameraSource) -> None:
        """A tab went away: its feed (if any) is lost and the call is told."""
        if source in self._sources:
            self._sources.remove(source)
        log_info(f"[call_video] camera tab gone ({len(self._sources)} open)")
        feed = self._feeds.get(source.call_id) if source.call_id else None
        if feed is None or feed.source is not source:
            return
        self._feeds.pop(feed.call_id, None)
        source.call_id = None
        log_warning(f"[call_video] the camera tab of call {feed.call_id} closed")
        if feed.on_lost is not None:
            try:
                await feed.on_lost()
            except Exception as exc:
                log_warning(f"[call_video] on_lost handler failed: {exc!r}")

    def has_source(self) -> bool:
        """True when a tab is connected and not already serving a call."""
        return any(s.call_id is None for s in self._sources)

    # -- the server renderer -----------------------------------------------------

    @staticmethod
    def _mode() -> str:
        mode = str(_cfg("CALL_VIDEO_SOURCE", "auto", str) or "auto").strip().lower()
        return mode if mode in ("auto", "server", "browser") else "auto"

    def _renderer_possible(self) -> bool:
        """Could the server draw the avatar? (moderngl present, a model, GL not known broken)"""
        if self._renderer_ok is False:
            return False
        if self._renderer_present is None:
            self._renderer_present = importlib.util.find_spec("moderngl") is not None
        if not self._renderer_present:
            return False
        from core.avatar_render.source import resolve_model_path

        return (
            resolve_model_path(str(_cfg("CALL_VIDEO_MODEL", "", str) or "")) is not None
        )

    async def ensure_renderer_probe(self) -> None:
        """Try a GL context once (on a worker thread) and remember whether it works."""
        if self._renderer_ok is not None or self._mode() == "browser":
            return
        if not self._renderer_possible():
            return
        from core.avatar_render.renderer import RendererUnavailable, probe

        try:
            gl = await asyncio.to_thread(probe)
        except RendererUnavailable as exc:
            self._renderer_ok = False
            log_warning(f"[call_video] the server cannot draw the avatar: {exc}")
        except Exception as exc:
            self._renderer_ok = False
            log_warning(f"[call_video] renderer probe failed: {exc!r}")
        else:
            self._renderer_ok = True
            log_info(f"[call_video] the server can draw the avatar (GL: {gl})")

    def video_possible(self) -> bool:
        """Is there a source for the SyntH's face right now (renderer or a free tab)?"""
        mode = self._mode()
        return (mode != "browser" and self._renderer_possible()) or (
            mode != "server" and self.has_source()
        )

    # -- calls -------------------------------------------------------------

    async def begin(
        self,
        call_id: str,
        on_lost: Callable[[], Awaitable[None]] | None = None,
        settings: VideoSettings | None = None,
    ) -> CallVideoFeed | None:
        """Ask the most recently connected free tab to capture for ``call_id``.

        Returns the feed, or ``None`` when no tab is available (the caller then keeps
        the call audio-only).
        """
        if call_id in self._feeds:
            return self._feeds[call_id]
        cfg = settings or VideoSettings.from_config()
        mode = self._mode()
        if mode != "browser" and self._renderer_possible():
            feed = await self._begin_rendered(call_id, on_lost, cfg)
            if feed is not None:
                return feed
        if mode == "server":
            return None
        for source in reversed(self._sources):
            if source.call_id is not None:
                continue
            try:
                await source.send(
                    {
                        "type": "start",
                        "call_id": call_id,
                        "fps": cfg.fps,
                        "width": cfg.width,
                        "height": cfg.height,
                        "quality": cfg.quality,
                    }
                )
            except Exception as exc:
                log_warning(f"[call_video] camera tab did not take the start: {exc!r}")
                continue
            source.call_id = call_id
            feed = CallVideoFeed(call_id, cfg, source, on_lost)
            self._feeds[call_id] = feed
            log_info(
                f"[call_video] capturing for call {call_id}: "
                f"{cfg.width}x{cfg.height} @ {cfg.fps} fps"
            )
            return feed
        return None

    async def _begin_rendered(
        self,
        call_id: str,
        on_lost: Callable[[], Awaitable[None]] | None,
        cfg: VideoSettings,
    ) -> CallVideoFeed | None:
        """Draw the avatar on the server for ``call_id``; ``None`` when that cannot start."""
        from core.avatar_render.renderer import RendererUnavailable
        from core.avatar_render.source import AvatarVideoSource, resolve_model_path

        path = resolve_model_path(str(_cfg("CALL_VIDEO_MODEL", "", str) or ""))
        if path is None:
            log_warning("[call_video] no VRM model to draw (set CALL_VIDEO_MODEL)")
            return None
        producer = AvatarVideoSource(path, cfg.width, cfg.height, cfg.fps)
        feed = CallVideoFeed(call_id, cfg, None, on_lost, fmt="i420", producer=producer)

        async def _failed() -> None:
            await self._producer_failed(call_id)

        try:
            await producer.start(feed.put_i420, on_failed=_failed)
        except RendererUnavailable as exc:
            self._renderer_ok = False
            log_warning(f"[call_video] the server cannot draw the avatar: {exc}")
            return None
        except Exception as exc:
            log_warning(f"[call_video] the avatar renderer could not start: {exc!r}")
            return None
        self._feeds[call_id] = feed
        log_info(
            f"[call_video] drawing the avatar for call {call_id}: "
            f"{cfg.width}x{cfg.height} @ {cfg.fps} fps ({path.name})"
        )
        return feed

    async def _producer_failed(self, call_id: str) -> None:
        feed = self._feeds.pop(call_id, None)
        if feed is None:
            return
        log_warning(f"[call_video] the avatar renderer of call {call_id} stopped")
        if feed.producer is not None:
            await feed.producer.stop()
        if feed.on_lost is not None:
            try:
                await feed.on_lost()
            except Exception as exc:
                log_warning(f"[call_video] on_lost handler failed: {exc!r}")

    def speak(self, call_id: str | None, path: Any) -> None:
        """The avatar of ``call_id`` (if the server draws it) speaks the WAV ``path``."""
        feed = self._feeds.get(call_id) if call_id else None
        if feed is not None and feed.producer is not None:
            try:
                feed.producer.speak(path)
            except Exception as exc:
                log_debug(f"[call_video] lip-sync skipped: {exc!r}")

    async def end(self, call_id: str) -> None:
        """Stop capturing for ``call_id`` (idempotent)."""
        feed = self._feeds.pop(call_id, None)
        if feed is None:
            return
        if feed.producer is not None:
            try:
                await feed.producer.stop()
            except Exception as exc:
                log_debug(f"[call_video] renderer stop failed: {exc!r}")
            return
        if feed.source is None:
            return
        feed.source.call_id = None
        try:
            await feed.source.send({"type": "stop", "call_id": call_id})
        except Exception as exc:
            log_debug(f"[call_video] stop not delivered (tab gone?): {exc!r}")

    def get_feed(self, call_id: str | None) -> CallVideoFeed | None:
        return self._feeds.get(call_id) if call_id else None

    def submit_frame(self, source: CameraSource, jpeg: bytes) -> bool:
        """Offer a frame from ``source``; ``True`` when it was kept as the latest."""
        feed = self._feeds.get(source.call_id) if source.call_id else None
        if feed is None or feed.source is not source or feed.fmt != "jpeg":
            return False
        if not jpeg or len(jpeg) > MAX_FRAME_BYTES or not jpeg.startswith(_JPEG_MAGIC):
            return False
        feed.put(bytes(jpeg))
        return True


_hub: CallVideoHub | None = None


def get_call_video_hub() -> CallVideoHub:
    """Return the process-wide hub."""
    global _hub
    if _hub is None:
        _hub = CallVideoHub()
    return _hub


def reset_call_video_hub() -> None:
    """Drop the singleton (tests)."""
    global _hub
    _hub = None


# -- frame conversion -----------------------------------------------------

# I420 as WebRTC expects it is limited-range BT.601; Pillow's YCbCr is full-range.
_LUMA_LUT = [16 + round(v * 219 / 255) for v in range(256)]
_CHROMA_LUT = [16 + round(v * 224 / 255) for v in range(256)]


def jpeg_to_i420(jpeg: bytes, width: int, height: int) -> bytes:
    """Decode ``jpeg`` into planar I420 (Y, U, V) of exactly ``width`` x ``height``.

    Both dimensions must be even. A frame of another size is scaled to fit (letterboxed
    on black), so a tab that ignores the requested size still produces a valid frame.
    Raises ``ValueError`` for an undecodable image or odd dimensions.
    """
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError(f"I420 needs even dimensions, got {width}x{height}")
    from PIL import Image

    try:
        with Image.open(io.BytesIO(jpeg)) as raw:
            img = raw.convert("RGB")
    except Exception as exc:
        raise ValueError(f"undecodable video frame: {exc}") from exc
    if img.size != (width, height):
        fitted = img.copy()
        fitted.thumbnail((width, height))
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        canvas.paste(
            fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2)
        )
        img = canvas
    y, cb, cr = img.convert("YCbCr").split()
    half = (width // 2, height // 2)
    y = y.point(_LUMA_LUT)
    cb = cb.resize(half, Image.Resampling.BOX).point(_CHROMA_LUT)
    cr = cr.resize(half, Image.Resampling.BOX).point(_CHROMA_LUT)
    return y.tobytes() + cb.tobytes() + cr.tobytes()


# -- the WebSocket ---------------------------------------------------------


def create_call_video_router() -> APIRouter:
    router = APIRouter()

    @router.websocket("/api/call/camera")
    async def camera_ws(websocket: WebSocket) -> None:
        if not webui_auth.websocket_authorized(websocket):
            await websocket.close(code=4401, reason="Invalid or missing API token")
            return
        await websocket.accept()
        hub = get_call_video_hub()
        source = hub.register_source(websocket)
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data:
                    hub.submit_frame(source, data)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log_debug(f"[call_video] camera stream closed: {exc}")
        finally:
            await hub.unregister_source(source)

    return router


def mount_call_video_api(app: FastAPI) -> None:
    """Mount ``/api/call/camera`` on ``app`` (non-fatal when it cannot be)."""
    try:
        app.include_router(create_call_video_router())
        log_info("[call_video] camera socket mounted at /api/call/camera")
    except Exception as exc:
        log_warning(f"[call_video] camera socket failed to mount (non-fatal): {exc}")


__all__ = [
    "CallVideoFeed",
    "CallVideoHub",
    "CameraSource",
    "VideoSettings",
    "create_call_video_router",
    "get_call_video_hub",
    "jpeg_to_i420",
    "mount_call_video_api",
    "reset_call_video_hub",
]
