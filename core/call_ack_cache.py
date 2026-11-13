"""The acoustic signals of a call.

* ``ack`` — a soft two-note "tu-din": the SyntH heard you and is working on it
  (LLM turns are slow, so it plays as soon as the caller's speech was transcribed);
* ``start`` — a short rising "pickup" when a call begins;
* ``end`` — a short falling tone when it ends.

They are synthesised here (decaying bell-like notes), not spoken: no TTS engine,
no model and no network, so they work on a fresh install and are ready instantly.
Each is written once to ``<VOX_OUTPUT_DIR>/call_ack/`` (the directory the WebUI
already serves audio from) and reused.
"""

from __future__ import annotations

import asyncio
import array
import math
import wave
from collections.abc import Iterable
from pathlib import Path

from core.logging_utils import log_debug, log_info, log_warning
from core.variables_engine import register_exposed_var

register_exposed_var(
    "CALL_ACK_ENABLED",
    label="Call Acknowledgement Sound",
    default=True,
    value_type=bool,
    ui_type="bool",
    description=(
        "When the caller stops speaking in a live call, play a short 'tu-din' "
        "signal so they know the message was received while the reply is being "
        "prepared."
    ),
    scope="core",
    component="call_manager",
)

SAMPLE_RATE = 24000

#: A note: (frequency Hz, start s, length s, decay rate 1/s: higher = shorter ring).
Note = tuple[float, float, float, float]

_SIGNALS: dict[str, tuple[Note, ...]] = {
    # E5 then A5, the second rings longer: "tu-din".
    "ack": ((659.25, 0.00, 0.16, 11.0), (880.00, 0.11, 0.50, 5.5)),
    # G5 - C6 - E6, quick and rising: a call being picked up.
    "start": (
        (783.99, 0.00, 0.10, 14.0),
        (1046.50, 0.08, 0.10, 14.0),
        (1318.51, 0.16, 0.30, 8.0),
    ),
    # C6 - G5 - C5, falling: the call is over.
    "end": (
        (1046.50, 0.00, 0.10, 14.0),
        (783.99, 0.09, 0.10, 14.0),
        (523.25, 0.18, 0.34, 8.0),
    ),
}

#: Bumped (v1 -> v2 ...) when a sound changes, so an older file on disk is replaced.
_FILE_NAMES: dict[str, str] = {
    "ack": "chime-v1.wav",
    "start": "pickup-v1.wav",
    "end": "hangup-v1.wav",
}
_SUBDIR = "call_ack"


def render_signal(kind: str) -> bytes:
    """Return the signal ``kind`` (``ack`` / ``start`` / ``end``) as mono 16-bit PCM WAV bytes."""
    notes = _SIGNALS[kind]
    total = max(start + length for _, start, length, _ in notes)
    n = int(SAMPLE_RATE * total) + 1
    mix = [0.0] * n
    for freq, start, length, decay_rate in notes:
        first = int(start * SAMPLE_RATE)
        count = int(length * SAMPLE_RATE)
        for i in range(count):
            t = i / SAMPLE_RATE
            attack = min(1.0, t / 0.006)  # 6 ms ramp: no click
            decay = math.exp(-t * decay_rate)
            tone = (
                math.sin(2 * math.pi * freq * t)
                + 0.30 * math.sin(2 * math.pi * 2 * freq * t)
                + 0.08 * math.sin(2 * math.pi * 3 * freq * t)
            )
            mix[first + i] += tone * attack * decay
    peak = max(abs(v) for v in mix) or 1.0
    scale = 0.5 / peak  # about -6 dBFS: audible, not startling
    samples = array.array("h", (int(v * scale * 32767) for v in mix))
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(samples.tobytes())
    return buf.getvalue()


def render_chime() -> bytes:
    """The "message received" signal."""
    return render_signal("ack")


class CallAckCache:
    """Holds the paths of the signal files, creating each on first use."""

    def __init__(self) -> None:
        self._path: Path | None = None  # the "ack" signal
        self._signals: dict[str, Path] = {}

    def get(self, language: str | None = None) -> Path | None:
        """Return the "message received" file (created if missing), or ``None`` if it cannot be written.

        ``language`` is accepted for API stability; the signal is language-neutral.
        """
        if self._path is not None and self._path.is_file():
            return self._path
        path = self.get_signal("ack")
        self._path = path
        return path

    def get_signal(self, kind: str) -> Path | None:
        """Return the file of signal ``kind`` (``ack`` / ``start`` / ``end``), writing it if missing."""
        known = self._signals.get(kind)
        if known is not None and known.is_file():
            return known
        try:
            from core.media_url_utils import get_vox_output_dir

            out_dir = get_vox_output_dir() / _SUBDIR
            target = out_dir / _FILE_NAMES[kind]
            if not target.is_file():
                out_dir.mkdir(parents=True, exist_ok=True)
                target.write_bytes(render_signal(kind))
                log_info(f"[call_ack] call signal '{kind}' written to {target}")
            # Drop clips left by earlier versions (spoken "Hmm", older sounds).
            keep = set(_FILE_NAMES.values())
            for old in out_dir.glob("*.wav"):
                if old.name not in keep:
                    old.unlink(missing_ok=True)
        except Exception as exc:
            log_warning(f"[call_ack] cannot prepare the '{kind}' call signal: {exc}")
            return None
        self._signals[kind] = target
        return target

    async def prepare(
        self, languages: Iterable[str | None] | None = None
    ) -> dict[str, Path]:
        """Make sure the signal file exists (off the event loop). Never raises."""
        ready = {}
        for kind in _SIGNALS:
            path = await asyncio.to_thread(self.get_signal, kind)
            if path is not None:
                ready[kind] = path
        if "ack" in ready:
            self._path = ready["ack"]
            ready["default"] = ready["ack"]
        log_debug(f"[call_ack] call signals ready: {sorted(ready)}")
        return ready

    def clear(self) -> None:
        """Forget the remembered paths (the files stay on disk)."""
        self._path = None
        self._signals.clear()


_cache: CallAckCache | None = None


def get_call_ack_cache() -> CallAckCache:
    """Return the process-wide call signal holder."""
    global _cache
    if _cache is None:
        _cache = CallAckCache()
    return _cache


def reset_call_ack_cache() -> None:
    """Drop the singleton (tests)."""
    global _cache
    _cache = None
