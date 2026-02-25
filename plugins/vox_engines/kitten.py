# plugins/vox_engines/kitten.py
"""Vox TTS engine: KittenTTS (optimised for low-performance hardware).

See: https://github.com/kittenTTS (placeholder — update when confirmed)

Registration is performed at import time.
"""

from __future__ import annotations

from typing import Any

from core.logging_utils import log_error, log_info, log_warning
from core.vox_registry import register_vox_engine
from plugins.vox_base import VoxEngineBase


class KittenVoxEngine(VoxEngineBase):
    """KittenTTS engine stub — optimised for low-resource hardware."""

    display_name = "KittenTTS"

    @property
    def output_format(self) -> str:
        return "wav"

    def setup(self) -> None:
        try:
            import kittentts  # type: ignore[import]  # noqa: F401

            log_info("[vox/kitten] KittenTTS available.")
        except ImportError:
            log_warning("[vox/kitten] kittentts not installed.")

    def generate_tts(
        self,
        text: str,
        emotion: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        try:
            import kittentts  # type: ignore[import]

            # TODO: implement once the KittenTTS API is confirmed
            result = kittentts.synthesize(text)  # type: ignore[attr-defined]
            return result if isinstance(result, bytes) else None
        except ImportError:
            log_error("[vox/kitten] kittentts not installed.")
            return None
        except Exception as exc:
            log_error(f"[vox/kitten] Synthesis failed: {exc}")
            return None


ENGINE_CLASS = KittenVoxEngine

register_vox_engine(
    name="kitten",
    module_path=__name__,
    capabilities={
        "voice_cloning": False,
        "emotions": False,
        "streaming": False,
        "local": True,
    },
    label="KittenTTS — lightweight TTS optimised for low-performance hardware.",
)
