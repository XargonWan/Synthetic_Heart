"""The body of the server-drawn avatar: plays the animation the Karada state server says is playing.

The Karada state server holds *which* clip plays (state, file, descriptor, ``started_at``); the
clients draw it. This module is the server renderer's client: it times the clip like the WebUI does
(intro once, then the loop; a play-once clip holds its last pose), samples it
(:class:`core.avatar_render.fbx.MixamoClip`) and hands the renderer the VRM normalized-bone
rotations, which the renderer puts on the model's raw bones as three-vrm does.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from core.avatar_render.fbx import (
    LOWER_BODY_BONES,
    MixamoClip,
    quaternion_to_matrix,
)

#: A new clip is faded in over this long, from the pose the avatar had.
CROSSFADE_S = 0.35
_DEFAULT_FPS = 30.0


@dataclass(frozen=True)
class AnimationRequest:
    """What the Karada state server says is playing."""

    path: Path | None
    descriptor: dict[str, Any] | None
    started_at: float | None  # epoch seconds
    state: str | None = None

    @property
    def key(self) -> tuple[str | None, float | None]:
        return (str(self.path) if self.path else None, self.started_at)


def _frame(section: Any, key: str, default: float) -> float:
    if isinstance(section, dict) and isinstance(section.get(key), (int, float)):
        return float(section[key])
    return default


def clip_time(
    clip: MixamoClip,
    descriptor: dict[str, Any] | None,
    elapsed: float,
    state: str | None,
) -> float:
    """Where in the clip (seconds) the avatar is ``elapsed`` seconds after the clip started.

    Same rules as the WebUI: a descriptor with ``intro`` and ``outro`` plays its intro once and
    then repeats its ``loop`` section (without a loop the last intro pose is held); ``think``
    without a descriptor splits the clip 20/60/20; any other clip repeats whole, or plays once and
    holds when the descriptor says ``play_once``. Descriptor end frames are inclusive.
    """
    desc = descriptor if isinstance(descriptor, dict) else {}
    fps = float(desc.get("fps") or _DEFAULT_FPS)
    total = max(2, round(clip.duration * fps))
    frames = max(0.0, elapsed) * fps
    intro, loop = desc.get("intro"), desc.get("loop")
    structured = isinstance(intro, dict) and isinstance(desc.get("outro"), dict)
    if not structured and state == "think":
        intro_start, intro_end = 0.0, float(max(1, round(total * 0.2)))
        loop_start, loop_end = intro_end, float(max(1, round(total * 0.8)))
    elif structured:
        intro_start = _frame(intro, "start_frame", 0.0)
        intro_end = _frame(intro, "end_frame", total - 1.0) + 1.0
        if isinstance(loop, dict):
            loop_start = _frame(loop, "start_frame", intro_end)
            loop_end = _frame(loop, "end_frame", total - 1.0) + 1.0
        else:
            loop_start = loop_end = 0.0
    else:
        if desc.get("play_once"):
            return min(frames, total - 1.0) / fps
        return (frames % total) / fps
    if frames < intro_end - intro_start:
        return (intro_start + frames) / fps
    span = loop_end - loop_start
    if span < 2.0:  # no loop section: hold the last pose of the intro
        return (intro_end - 1.0) / fps
    return (loop_start + (frames - (intro_end - intro_start)) % span) / fps


def _slerp_blend(a: np.ndarray, b: np.ndarray, amount: float) -> np.ndarray:
    """Normalised lerp of two quaternions, on the short way round."""
    if float(np.dot(a, b)) < 0.0:
        b = -b
    out = a * (1.0 - amount) + b * amount
    return out / max(float(np.linalg.norm(out)), 1e-9)


class MotionPlayer:
    """Samples the clip the Karada state names, once per frame, for one model."""

    def __init__(
        self, humanoid: set[str], hips_height: float, faces_minus_z: bool
    ) -> None:
        self._humanoid = humanoid
        self._hips_height = hips_height
        self._vrm0 = faces_minus_z
        self._clips: dict[Path, MixamoClip | None] = {}
        self._current_key: tuple[str | None, float | None] | None = None
        self._last: dict[str, np.ndarray] = {}
        self._last_hips: np.ndarray | None = None
        self._from: dict[str, np.ndarray] = {}
        self._from_hips: np.ndarray | None = None
        self._fade_started = 0.0

    def clip(self, path: Path) -> tuple[bool, MixamoClip | None]:
        """``(known, clip)``: ``known`` is False for a path nobody has tried to load yet."""
        if path in self._clips:
            return True, self._clips[path]
        return False, None

    def set_clip(self, path: Path, clip: MixamoClip | None) -> None:
        self._clips[path] = clip

    def sample(
        self, request: AnimationRequest | None, now: float | None = None
    ) -> tuple[dict[str, np.ndarray], np.ndarray | None]:
        """``(rotations, hips position)``: VRM normalized-bone 3x3 matrices and the hips'
        translation in the model's units (``None`` when the clip does not move the hips)."""
        wall = time.time() if now is None else now
        clip = (
            self._clips.get(request.path)
            if request is not None and request.path is not None
            else None
        )
        if request is None or clip is None:
            return {}, None
        if request.key != self._current_key:
            self._current_key = request.key
            self._from, self._from_hips = dict(self._last), self._last_hips
            self._fade_started = wall
        started = request.started_at if request.started_at is not None else wall
        t = clip_time(clip, request.descriptor, wall - started, request.state)
        masked = isinstance(request.descriptor, dict) and (
            request.descriptor.get("mask") == "upper_body"
        )
        fade = (
            min(1.0, (wall - self._fade_started) / CROSSFADE_S) if self._from else 1.0
        )
        quats: dict[str, np.ndarray] = {}
        for bone in clip.rotations:
            if bone not in self._humanoid or (masked and bone in LOWER_BODY_BONES):
                continue
            q = clip.sample_rotation(bone, t)
            if q is None:
                continue
            previous = self._from.get(bone)
            if previous is not None and fade < 1.0:
                q = _slerp_blend(previous, q, fade)
            quats[bone] = q
        self._last = quats
        rotations: dict[str, np.ndarray] = {}
        for bone, q in quats.items():
            # three-vrm's VRM 0.x models are turned half a circle: x and z change sign.
            if self._vrm0:
                q = np.array([-q[0], q[1], -q[2], q[3]])
            rotations[bone] = quaternion_to_matrix(q)

        hips: np.ndarray | None = None
        position = None if masked else clip.sample_hips_position(t)
        if position is not None and clip.hips_rest_height > 0 and self._hips_height > 0:
            scaled = np.asarray(position, dtype=np.float64) * (
                self._hips_height / clip.hips_rest_height
            )
            if self._vrm0:
                scaled = np.array([-scaled[0], scaled[1], -scaled[2]])
            if self._from_hips is not None and fade < 1.0:
                scaled = self._from_hips * (1.0 - fade) + scaled * fade
            hips = scaled
        self._last_hips = hips
        return rotations, hips
