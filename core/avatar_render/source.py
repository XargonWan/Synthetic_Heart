"""The SyntH's face as a live video source: renders frames on a worker thread, at the
call's frame rate, from the Karada state, and hands each one to a sink.

The GL context lives on one dedicated thread (a single-thread executor): it is created
there, used there and released there.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from core.avatar_render.animator import AvatarAnimator, SpeechTrack, emotion_expressions
from core.avatar_render.renderer import AvatarPose, AvatarRenderer
from core.avatar_render.vrm import load_vrm
from core.logging_utils import log_debug, log_info, log_warning

#: Consecutive failed frames after which the source gives up.
_MAX_FAILURES = 3
_DEFAULT_MODEL = Path("skins") / "Rei" / "model.vrm"
#: The image's seed copy of Rei (``/app/skins`` is usually a volume that starts empty).
_SEED_MODEL = Path(".skins-seed") / "Rei" / "model.vrm"

StateReader = Callable[[], tuple[dict[str, float], dict[str, Any] | None]]


def karada_state() -> tuple[dict[str, float], dict[str, Any] | None]:
    """The Karada state server's emotion intensities and transient expression."""
    try:
        from core.animation_handler import get_karada_state_server

        server = get_karada_state_server()
        return server.get_face_values(), server.get_current_expression()
    except Exception as exc:
        log_debug(f"[avatar_render] no Karada state: {exc!r}")
        return {}, None


def resolve_model_path(configured: str = "") -> Path | None:
    """The VRM to draw: the configured one, else the active one, else Rei's skin (or its seed)."""
    candidates: list[Path] = []
    if configured.strip():
        candidates.append(Path(configured.strip()).expanduser())
    try:
        from core.animation_handler import get_karada_state_server

        active = get_karada_state_server().get_active_vrm_path()
        if active is not None:
            candidates.append(active)
    except Exception as exc:
        log_debug(f"[avatar_render] active VRM unknown: {exc!r}")
    candidates.extend((_DEFAULT_MODEL, _SEED_MODEL))
    for path in candidates:
        if path.is_file():
            return path
    return None


class AvatarVideoSource:
    """Renders the avatar for one call. ``start`` once, ``speak`` per spoken clip, ``stop`` once."""

    def __init__(
        self,
        model_path: Path,
        width: int,
        height: int,
        fps: int,
        state_reader: StateReader = karada_state,
    ) -> None:
        self._model_path = model_path
        self._width, self._height = width, height
        self._interval = 1.0 / max(1, fps)
        self._state_reader = state_reader
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="avatar-render"
        )
        self._renderer: AvatarRenderer | None = None
        self._animator = AvatarAnimator()
        self._speech: SpeechTrack | None = None
        self._task: asyncio.Task[None] | None = None
        self.frames = 0

    # -- lifecycle ----------------------------------------------------------

    def _build(self) -> str:
        model = load_vrm(self._model_path)
        self._renderer = AvatarRenderer(model, self._width, self._height)
        return self._renderer.gl_renderer

    async def start(
        self,
        sink: Callable[[bytes], None],
        on_failed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Load the model and the GL context, then render into ``sink`` until stopped.

        Raises (``RendererUnavailable``, ``VrmError``, ...) when it cannot start.
        """
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        try:
            gl = await loop.run_in_executor(self._pool, self._build)
        except BaseException:
            await self.stop()
            raise
        log_info(
            f"[avatar_render] {self._model_path.name} ready on {gl} in "
            f"{time.monotonic() - started:.1f}s ({self._width}x{self._height})"
        )
        self._task = asyncio.ensure_future(self._run(sink, on_failed))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        renderer, self._renderer = self._renderer, None
        if renderer is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    self._pool, renderer.close
                )
            except Exception as exc:
                log_debug(f"[avatar_render] release failed: {exc!r}")
        self._pool.shutdown(wait=False)

    def speak(self, path: Path) -> None:
        """The mouth follows ``path`` (a WAV) from now on."""
        track = SpeechTrack.from_wav(Path(path))
        if track is None:
            log_debug(
                f"[avatar_render] no loudness from {Path(path).name}; the mouth stays shut"
            )
        self._speech = track

    # -- the render loop -------------------------------------------------------

    def _render(self, pose: AvatarPose) -> bytes:
        renderer = self._renderer
        if renderer is None:
            raise RuntimeError("renderer is closed")
        return renderer.render(pose)

    async def _run(
        self,
        sink: Callable[[bytes], None],
        on_failed: Callable[[], Awaitable[None]] | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        origin = time.monotonic()
        failures = 0
        while True:
            began = time.monotonic()
            speech = self._speech
            level = 0.0
            if speech is not None:
                if speech.over(began):
                    self._speech = None
                else:
                    level = speech.level_at(began)
            face_values, override = self._state_reader()
            pose = self._animator.pose(
                began - origin,
                speech_level=level,
                expressions=emotion_expressions(face_values, override),
            )
            try:
                frame = await loop.run_in_executor(self._pool, self._render, pose)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                log_warning(
                    f"[avatar_render] frame failed ({failures}/{_MAX_FAILURES}): {exc!r}"
                )
                if failures >= _MAX_FAILURES:
                    if on_failed is not None:
                        await on_failed()
                    return
            else:
                failures = 0
                self.frames += 1
                sink(frame)
            await asyncio.sleep(max(0.0, self._interval - (time.monotonic() - began)))
