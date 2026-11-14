"""What the avatar does from one frame to the next: blinking, breathing, a little head sway,
the mouth following the SyntH's voice, and the face following the Karada emotion state.

Pure Python/numpy, no GL: ``AvatarAnimator.pose(...)`` turns the time and the inputs into an
:class:`core.avatar_render.renderer.AvatarPose`. The inputs are structural: the numeric
emotion intensities of the Karada state server and the loudness of the speech being played.
"""

from __future__ import annotations

import math
import random
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from core.avatar_render.renderer import AvatarPose

#: Emotion names (the emotion manager's canonical set) that have a face. Values are VRM 1.0
#: expression presets. Anything else (love, fear, ...) has no preset here and is ignored.
EMOTION_EXPRESSIONS = {
    "happy": "happy",
    "sad": "sad",
    "angry": "angry",
    "relaxed": "relaxed",
    "surprised": "surprised",
}
#: A transient expression override from the Karada server uses the same names (VRM 1.0
#: names, or the VRM 0.x ones).
_EXPRESSION_ALIASES = {
    "joy": "happy",
    "sorrow": "sad",
    "fun": "relaxed",
    "aa": "aa",
}
_BLINK_DURATION_S = 0.16
_MAX_EMOTION_WEIGHT = (
    0.7  # a full-intensity emotion is shown at this much of the preset
)


def _rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float32)


def _rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float32)


def emotion_expressions(
    face_values: dict[str, float], override: dict[str, Any] | None = None
) -> dict[str, float]:
    """VRM expression weights for the Karada state's emotions and transient expression."""
    out: dict[str, float] = {}
    for name, value in (face_values or {}).items():
        preset = EMOTION_EXPRESSIONS.get(str(name).lower())
        if preset is None:
            continue
        try:
            weight = max(0.0, min(1.0, float(value))) * _MAX_EMOTION_WEIGHT
        except (TypeError, ValueError):
            continue
        out[preset] = max(out.get(preset, 0.0), weight)
    if override and override.get("type") == "vrm_expression_set":
        targets = override.get("targets")
        pairs = (
            targets.items()
            if isinstance(targets, dict)
            else [(override.get("name"), override.get("intensity", 1.0))]
        )
        for name, value in pairs:
            key = str(name or "").lower()
            preset = _EXPRESSION_ALIASES.get(key, key)
            try:
                out[preset] = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                continue
    return out


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
        # Smile a little when nothing else says how the face should be.
        if not any(
            weights.get(k, 0.0) > 0.05
            for k in ("happy", "sad", "angry", "relaxed", "surprised")
        ):
            weights["relaxed"] = 0.12
        deltas = {
            "head": _rot_y(0.06 * math.sin(t * 0.9))
            @ _rot_x(0.03 * math.sin(t * 0.67 + 0.8)),
            "neck": _rot_y(0.02 * math.sin(t * 0.9)),
            "chest": _rot_x(0.012 * math.sin(t * 1.57)),
        }
        return AvatarPose(expressions=weights, bone_deltas=deltas)
