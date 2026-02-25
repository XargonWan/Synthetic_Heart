# plugins/vox_engines/qwen3tts.py
"""Vox TTS engine: Qwen3TTS.

See: https://huggingface.co/Qwen/Qwen3-TTS

Installation:
    uv add transformers torch soundfile

Registration is performed at import time.
"""

from __future__ import annotations

from typing import Any

from core.logging_utils import log_error
from core.vox_registry import register_vox_engine
from plugins.vox_base import VoxEngineBase


class Qwen3TTSVoxEngine(VoxEngineBase):
    """Qwen3TTS engine stub."""

    display_name = "Qwen3TTS"

    @property
    def output_format(self) -> str:
        return "wav"

    def generate_tts(
        self,
        text: str,
        emotion: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        try:
            from transformers import pipeline  # type: ignore[import]
            import io
            import soundfile as sf  # type: ignore[import]

            # TODO: cache pipeline; configure model path via VOX_ENGINE_SETTINGS
            tts = pipeline("text-to-speech", model="Qwen/Qwen3-TTS")
            output = tts(text)
            buf = io.BytesIO()
            sf.write(buf, output["audio"], output["sampling_rate"], format="WAV")
            return buf.getvalue()
        except ImportError:
            log_error(
                "[vox/qwen3tts] Missing dependencies. Run: uv add transformers torch soundfile"
            )
            return None
        except Exception as exc:
            log_error(f"[vox/qwen3tts] Synthesis failed: {exc}")
            return None


ENGINE_CLASS = Qwen3TTSVoxEngine

register_vox_engine(
    name="qwen3tts",
    module_path=__name__,
    capabilities={
        "voice_cloning": False,
        "emotions": False,
        "streaming": False,
        "local": True,
    },
    label="Qwen3TTS — Alibaba Qwen 3 text-to-speech model. Requires transformers + torch.",
)
