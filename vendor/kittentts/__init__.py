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
from typing import List

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


class KittenTTS:
    """Simple KittenTTS implementation.

    The real package will load one of the small neural models from
    ``KittenML/kitten-tts-nano-*`` and synthesise directly.  This stub falls
    back to ``gtts`` so that tests and the dev container continue to work
    without large model downloads.
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

        wav_buf = io.BytesIO()
        # convert MP3 -> WAV so callers always receive PCM data
        AudioSegment.from_file(mp3_buf, format="mp3").export(wav_buf, format="wav")
        return wav_buf.getvalue()

    @classmethod
    def list_voices(cls) -> List[str]:
        # Mirror the voice names registered in `model_manager` so the
        # WebUI dropdown stays populated.
        return list(_DEFAULT_VOICE_LIST)
