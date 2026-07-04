"""Minimal KittenTTS package used by the Vox engine.

This shimming package implements a minimal version of the real KittenTTS
engine.  The upstream model is backed by the ``kitten-tts-nano`` HuggingFace
repo; here we provide a lightweight fallback using ``gtts`` + ``pydub`` so
that the voice output sounds reasonably natural without additional
installation steps.

When the actual ``kittentts`` PyPI package is installed, it should provide a
compatible ``KittenTTS`` class with ``generate(text, voice)`` and
``list_voices()`` methods.  In the meantime this module allows the codebase to
import ``kittentts`` and behave as if a proper neural engine were present.
"""

from __future__ import annotations

import io
from typing import Any, List

# the real package will have its own dependency list; in the vendored
# stub we lazily import third-party libraries so that merely importing
# ``kittentts`` doesn't crash if they are missing (tests/early-start
# environments may not have gtts/pydub).  generate() will raise if the
# imports fail at runtime.

_DEFAULT_VOICE_LIST = [
    "Bella",
    "Jasper",
    "Luna",
    "Bruno",
    "Rosie",
    "Hugo",
    "Kiki",
    "Leo",
]


def _make_loud(seg: Any) -> Any:
    """Multi-stage compression for radio-level loudness.

    Three cascading compressors progressively crush the dynamic range, then
    peak-normalise to -1 dBFS.  **No explicit makeup gain** — all gain is
    applied transparently by the final normalise step, so there is zero
    hard clipping.  Inside the engine, the output is inherently louder
    without requiring any post-generation processing.
    """
    from pydub.effects import compress_dynamic_range

    seg = compress_dynamic_range(
        seg, threshold=-24.0, ratio=3.0, attack=5.0, release=80.0
    )
    seg = compress_dynamic_range(
        seg, threshold=-18.0, ratio=12.0, attack=1.5, release=40.0
    )
    seg = compress_dynamic_range(
        seg, threshold=-10.0, ratio=50.0, attack=0.5, release=15.0
    )
    return seg


class KittenTTS:
    """Simple KittenTTS implementation.

    The real package will load one of the small neural models from
    ``KittenML/kitten-tts-nano-*`` and synthesise directly.  This stub falls
    back to ``gtts`` so that tests and the dev container continue to work
    without large model downloads.

    The output WAV is peak-normalised to -1 dBFS with gentle compression
    so the voice sounds naturally loud without requiring downstream gain.
    """

    def __init__(self, model_id: str | None = None) -> None:
        # ``model_id`` is currently unused in this shim, but the real
        # implementation will use it to select a downloaded model.
        self.model_id = model_id or "kitten-tts-nano-0.8"

    def generate(self, text: str, voice: str = "Bella", language: str = "en") -> bytes:
        # NOTE: this is the vendored gTTS stub, used only when the real
        # kittentts package is not installed.  gTTS does not support multiple
        # voice personas — ``voice`` is accepted in the signature for API
        # compatibility but has no effect on the audio output.  Install the
        # real kittentts package for genuine multi-voice synthesis where each
        # persona (Bella, Luna, Jasper …) sounds distinctly different.
        #
        # The ``language`` parameter IS respected: Italian text will be
        # synthesised with the Italian gTTS voice, English with English, etc.
        try:
            from gtts import gTTS  # type: ignore[import]
            from pydub import AudioSegment  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "Required dependencies for vendored KittenTTS stub are missing: "
                f"{exc}. Install 'gtts' and 'pydub' or add the real package."
            )

        tts = gTTS(text=text, lang=language)
        mp3_buf = io.BytesIO()
        tts.write_to_fp(mp3_buf)
        mp3_buf.seek(0)

        seg = AudioSegment.from_file(mp3_buf, format="mp3")
        # Multi-stage compression crushes dynamic range so the final
        # normalise can bring the RMS level up without hard clipping.
        seg = _make_loud(seg)
        # Peak normalise to -1 dBFS for consistent level across calls.
        seg = seg.normalize(headroom=1.0)

        wav_buf = io.BytesIO()
        seg.export(wav_buf, format="wav")
        return wav_buf.getvalue()

    @classmethod
    def list_voices(cls) -> List[str]:
        # Mirror the voice names registered in `model_manager` so the
        # WebUI dropdown stays populated.
        return list(_DEFAULT_VOICE_LIST)
