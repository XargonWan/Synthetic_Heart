# plugins/vox_engines/harmony.py
"""Vox TTS engine: Harmony Speech V1 (CPU voice cloner).

See: https://github.com/harmony-ai-solutions/harmony-ai-app

Installation:
    uv add harmony-speech-engine   # or follow the repo README

Registration is performed at import time.
"""

from __future__ import annotations

from typing import Any

from core.logging_utils import log_error, log_info, log_warning
from core.vox_registry import register_vox_engine
from plugins.vox_base import VoxEngineBase


class HarmonyVoxEngine(VoxEngineBase):
    """Harmony Speech V1 TTS engine (CPU-only voice cloner)."""

    display_name = "Harmony Speech V1"

    @property
    def output_format(self) -> str:
        return "wav"

    def setup(self) -> None:
        try:
            import harmony_speech  # type: ignore[import]  # noqa: F401

            log_info("[vox/harmony] Harmony Speech SDK available.")
        except ImportError:
            log_warning(
                "[vox/harmony] harmony-speech-engine not installed. "
                "Run: uv add harmony-speech-engine"
            )

    def generate_tts(
        self,
        text: str,
        emotion: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        try:
            import harmony_speech  # type: ignore[import]

            # TODO: configure voice reference via VOX_ENGINE_SETTINGS
            result = harmony_speech.synthesize(text=text)  # type: ignore[attr-defined]
            return result if isinstance(result, bytes) else None
        except ImportError:
            log_error("[vox/harmony] harmony-speech-engine not installed.")
            return None
        except Exception as exc:
            log_error(f"[vox/harmony] Synthesis failed: {exc}")
            return None


ENGINE_CLASS = HarmonyVoxEngine

register_vox_engine(
    name="harmony",
    module_path=__name__,
    capabilities={
        "voice_cloning": True,
        "emotions": False,
        "streaming": False,
        "local": True,
    },
    label="Harmony Speech V1 — CPU-only voice cloner. "
    "Requires harmony-speech-engine package.",
)
