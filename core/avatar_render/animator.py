"""What the avatar does from one frame to the next: blinking, breathing, the mouth following
the SyntH's voice and, on top, the face the Karada state server says it has.

Pure Python/numpy, no GL: ``AvatarAnimator.pose(...)`` turns the time and the inputs into an
:class:`core.avatar_render.renderer.AvatarPose`. The inputs are structural: the VRM expression
weights from :meth:`core.animation_handler.KaradaStateServer.get_vrm_expression_weights` and the
loudness of the speech being played. The animator adds no gesture of its own (no head turns):
the Karada animation clips are not played here yet, and an invented motion would not be the one
the WebUI shows.
"""

from __future__ import annotations

import math
import random
import time
import wave
from pathlib import Path
import numpy as np

from core.avatar_render.renderer import AvatarPose

_BLINK_DURATION_S = 0.16


def _rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float32)


class SpeechTrack:
    """The loudness of one spoken clip over time, to open the mouth with it."""

    def __init__(self, levels: np.ndarray, hop_s: float, started: float) -> None:
        self.levels = levels
        self.hop_s = hop_s
        self.started = started

    @classmethod
    def from_wav(
        cls, path: Path, hop_s: float = 0.04, started: float | None = None
    ) -> SpeechTrack | None:
        """The envelope of a 16-bit PCM WAV, or ``None`` when it cannot be read."""
        try:
            with wave.open(str(path), "rb") as wav:
                if wav.getsampwidth() != 2:
                    return None
                rate, channels = wav.getframerate(), wav.getnchannels()
                pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        except Exception:
            return None
        if channels > 1:
            pcm = (
                pcm[: len(pcm) // channels * channels]
                .reshape(-1, channels)
                .mean(axis=1)
            )
        hop = max(1, int(rate * hop_s))
        count = len(pcm) // hop
        if count == 0:
            return None
        frames = pcm[: count * hop].astype(np.float32).reshape(count, hop)
        rms = np.sqrt((frames**2).mean(axis=1))
        ceiling = float(np.percentile(rms, 95)) or 1.0
        levels = np.clip(rms / ceiling, 0.0, 1.0)
        return cls(levels, hop_s, time.monotonic() if started is None else started)

    def level_at(self, now: float) -> float:
        index = int((now - self.started) / self.hop_s)
        if index < 0 or index >= len(self.levels):
            return 0.0
        return float(self.levels[index])

    def over(self, now: float) -> bool:
        return (now - self.started) >= len(self.levels) * self.hop_s


class AvatarAnimator:
    """Time + inputs -> :class:`AvatarPose`."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._next_blink = 1.5
        self._blink_started: float | None = None

    def _blink(self, t: float) -> float:
        if self._blink_started is None and t >= self._next_blink:
            self._blink_started = t
        if self._blink_started is None:
            return 0.0
        phase = (t - self._blink_started) / _BLINK_DURATION_S
        if phase >= 1.0:
            self._blink_started = None
            self._next_blink = t + self._rng.uniform(2.5, 5.5)
            return 0.0
        return 1.0 - abs(2.0 * phase - 1.0)  # closes and opens again

    def pose(
        self,
        t: float,
        *,
        speech_level: float = 0.0,
        expressions: dict[str, float] | None = None,
    ) -> AvatarPose:
        """The pose at time ``t`` (seconds, increasing) for a mouth loudness in 0..1."""
        weights: dict[str, float] = dict(expressions or {})
        blink = self._blink(t)
        if blink > 0.0:
            weights["blink"] = max(weights.get("blink", 0.0), blink)
        if speech_level > 0.02:
            level = max(0.0, min(1.0, speech_level))
            # Mostly an open "aa", with the rounder and wider shapes drifting in and out.
            weights["aa"] = max(weights.get("aa", 0.0), 0.9 * level)
            weights["oh"] = max(
                weights.get("oh", 0.0), 0.35 * level * max(0.0, math.sin(t * 6.1))
            )
            weights["ee"] = max(
                weights.get("ee", 0.0), 0.3 * level * max(0.0, math.sin(t * 4.3 + 1.7))
            )
        # Only breathing: where the head and arms go is the Karada animation's business.
        deltas = {"chest": _rot_x(0.012 * math.sin(t * 1.57))}
        return AvatarPose(expressions=weights, bone_deltas=deltas)
