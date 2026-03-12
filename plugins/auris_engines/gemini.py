# plugins/auris_engines/gemini.py
"""Auris STT engine: Gemini (file-based).

Wraps the existing ``GeminiAPIPlugin.handle_live_processing`` method to provide
file-based transcription via the Gemini standard GenerateContent API.  No
streaming/VAD support — for that see ``silero.py``.

Registration is performed at import time so loading this module is enough.
"""

from __future__ import annotations

from typing import Any

from core.auris_registry import register_auris_engine
from core.logging_utils import log_error, log_warning
from plugins.auris_base import AurisEngineBase, AurisTranscriptResult


class GeminiAurisEngine(AurisEngineBase):
    """STT engine that delegates to Gemini's GenerateContent API."""

    display_name = "Gemini STT (file-based)"

    def __init__(self) -> None:
        self._engine_instance: Any = None

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    def _get_gemini_engine(self) -> Any | None:
        """Return the active Gemini cortex engine instance, or None."""
        try:
            from core.cortex_registry import CORTEX_REGISTRY  # type: ignore[import]

            # Try to find a loaded Gemini engine via the cortex registry
            for name in CORTEX_REGISTRY.get_available_engines():
                if "gemini" in name.lower():
                    try:
                        return CORTEX_REGISTRY.load_engine(name)
                    except Exception:
                        continue
        except Exception as exc:
            log_warning(f"[auris/gemini] Could not access cortex registry: {exc}")
        return None

    # ------------------------------------------------------------------
    # AurisEngineBase implementation
    # ------------------------------------------------------------------

    async def transcribe(
        self, file_path: str, mime_type: str | None = None
    ) -> AurisTranscriptResult | None:
        """Transcribe *file_path* by calling ``handle_live_processing`` on the Gemini engine.

        Gemini does not expose the detected spoken language in the standard
        GenerateContent API response, so ``language`` is always ``None`` in the
        returned :class:`AurisTranscriptResult`.  Downstream components
        (e.g. :mod:`plugins.auris_plugin`) will apply text-based language
        detection as a fallback.
        """
        engine = self._get_gemini_engine()
        if engine is None:
            log_error("[auris/gemini] No Gemini engine found in cortex registry.")
            return None

        handler = getattr(engine, "handle_live_processing", None)
        if handler is None:
            log_error("[auris/gemini] Gemini engine lacks handle_live_processing.")
            return None

        try:
            text: str | None = await handler(file_path, mime_type_hint=mime_type)
        except Exception as exc:
            log_error(f"[auris/gemini] Transcription failed: {exc}")
            return None

        if not text:
            return None
        # Gemini doesn't expose the spoken language; return language=None so
        # AurisPlugin can fill it in via text-based detection.
        return AurisTranscriptResult(text=text, language=None)


# ---------------------------------------------------------------------------
# Export + auto-registration
# ---------------------------------------------------------------------------

ENGINE_CLASS = GeminiAurisEngine

register_auris_engine(
    name="gemini",
    module_path=__name__,
    capabilities={"file_based": True, "realtime": False, "vad": False, "local": False},
    label="Gemini STT via GenerateContent API (file-based, requires GEMINI_API_KEY).",
)
