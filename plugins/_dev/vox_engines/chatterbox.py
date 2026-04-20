# plugins/vox_engines/chatterbox.py
"""Vox TTS engine: Chatterbox TTS.

See: https://github.com/resemble-ai/chatterbox

Installation:
    uv add chatterbox-tts

Registration is performed at import time.
"""

from __future__ import annotations

from typing import Any

from core.logging_utils import log_error, log_info, log_warning
from core.vox_registry import register_vox_engine
from plugins.vox_base import VoxEngineBase


class ChatterboxVoxEngine(VoxEngineBase):
    """Chatterbox TTS engine stub."""

    display_name = "Chatterbox TTS"

    @property
    def output_format(self) -> str:
        return "wav"

    def setup(self) -> None:
        try:
            import chatterbox  # type: ignore[import]  # noqa: F401

            log_info("[vox/chatterbox] Chatterbox TTS available.")
        except ImportError:
            log_warning(
                "[vox/chatterbox] chatterbox-tts not installed. Run: uv add chatterbox-tts"
            )

    def generate_tts(
        self,
        text: str,
        emotion: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        try:
            import chatterbox  # type: ignore[import]

            # TODO: implement once chatterbox API is confirmed
            result = chatterbox.synthesize(text)  # type: ignore[attr-defined]
            return result if isinstance(result, bytes) else None
        except ImportError:
            log_error("[vox/chatterbox] chatterbox-tts not installed.")
            return None
        except Exception as exc:
            log_error(f"[vox/chatterbox] Synthesis failed: {exc}")
            return None


ENGINE_CLASS = ChatterboxVoxEngine

register_vox_engine(
    name="chatterbox",
    module_path=__name__,
    capabilities={
        "voice_cloning": False,
        "emotions": True,
        "streaming": False,
        "local": True,
    },
    label="Chatterbox TTS — expressive local TTS. Requires chatterbox-tts package.",
)
