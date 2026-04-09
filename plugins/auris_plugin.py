# plugins/auris_plugin.py
"""Auris — core STT plugin.

Provides the ``stt_transcribe`` action and routes all speech-to-text work
through the Auris engine registry.  Interfaces and other plugins should call
``AurisPlugin.transcribe_audio()`` instead of invoking engines directly.

Engines are registered by importing their modules; this plugin automatically
imports the built-in engines on startup.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from core.ai_plugin_base import AIPluginBase
from core.auris_registry import AURIS_REGISTRY
from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.logging_utils import log_error, log_info, log_warning
from core.variables_engine import register_exposed_var
from plugins.auris_base import AurisTranscriptResult

# ---------------------------------------------------------------------------
# Exposed config variables
# ---------------------------------------------------------------------------


register_exposed_var(
    "ACTIVE_AURIS_ENGINE",
    label="Active Auris Engine",
    default="vosk",
    value_type=str,
    ui_type="string",
    description=(
        "Name of the active Auris STT engine (file-based only, e.g. 'vosk'). "
        "Set to 'disabled' to turn off the Auris subsystem. For real-time streaming use the Live subsystem."
    ),
    scope="plugins",
    component="auris_plugin",
    advanced=False,
)

register_exposed_var(
    "AURIS_ENGINE_SETTINGS",
    label="Auris Engine Settings (JSON)",
    default="{}",
    value_type=str,
    ui_type="string",
    description="Optional JSON dict of per-engine settings passed to the active Auris engine.",
    scope="plugins",
    component="auris_plugin",
    advanced=True,
)

register_exposed_var(
    "VOSK_MODEL_PATH",
    label="Vosk Model Path",
    default="",
    value_type=str,
    ui_type="string",
    description=(
        "Path to a Vosk model directory.  Leave blank to use the default "
        "~/.cache/vosk/vosk-model-small-en-us (auto-downloaded on first use)."
    ),
    scope="plugins",
    component="auris_plugin",
    advanced=True,
)

register_exposed_var(
    "VOSK_LANGUAGE",
    label="Vosk Language",
    default="en-us",
    value_type=str,
    ui_type="string",
    description=(
        "Language code used when auto-downloading a Vosk model. "
        "The WebUI shows a dropdown when the active Auris engine is vosk."
    ),
    scope="plugins",
    component="auris_plugin",
    advanced=True,
)

register_exposed_var(
    "MODEL_AUTO_DOWNLOAD",
    label="Auto‑download models",
    default=True,
    value_type=bool,
    ui_type="boolean",
    description=(
        "When enabled, Auris will automatically fetch missing models from the "
        "Model Manager when they are needed (e.g. a Vosk STT model). "
        "If disabled, you must manually download models via the WebUI."
    ),
    scope="plugins",
    component="auris_plugin",
    advanced=True,
)


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class AurisPlugin(AIPluginBase):
    """Core STT plugin.  Registers supported actions and delegates to engine."""

    display_name = "Auris (STT)"

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()
        # no explicit enabled flag; engine name "disabled" turns it off
        self._active_engine_name: str = "gemini"
        self._engine_settings: dict[str, Any] = {}

        # Import built-in engine modules so they self-register
        self._import_builtin_engines()

        self.refresh_config()
        register_plugin("auris_plugin", self)
        log_info("[auris_plugin] Initialized.")

    # ------------------------------------------------------------------
    # Public API — used by interfaces and other plugins
    # ------------------------------------------------------------------

    async def transcribe_audio(
        self,
        file_path: str,
        mime_type: str | None = None,
        engine_name: str | None = None,
    ) -> AurisTranscriptResult | None:
        """Transcribe an audio file and return the result with language.

        Args:
            file_path:   Path to the audio file.
            mime_type:   Optional MIME hint.
            engine_name: Override the active engine for this call.

        Returns:
            :class:`AurisTranscriptResult` with the transcribed text and
            the detected language code, or ``None``.  When the engine cannot
            determine the spoken language acoustically, the language is
            inferred from the transcribed text using ``lingua``.
        """
        self.refresh_config()

        # if the configured engine is explicitly disabled, behave identically
        if self._active_engine_name == "disabled":
            log_info("[auris_plugin] Engine disabled; skipping transcription.")
            return None

        if not os.path.exists(file_path):
            log_error(f"[auris_plugin] File not found: {file_path}")
            return None

        name = engine_name or self._active_engine_name
        try:
            engine = AURIS_REGISTRY.load_engine(name)
        except ValueError as exc:
            log_error(f"[auris_plugin] Cannot load engine '{name}': {exc}")
            return None

        try:
            import inspect

            if inspect.iscoroutinefunction(engine.transcribe):
                result: AurisTranscriptResult | None = await engine.transcribe(
                    file_path, mime_type
                )
            else:
                result = await asyncio.to_thread(
                    engine.transcribe, file_path, mime_type
                )

            if result is None:
                return None

            # If the engine did not identify the spoken language, fall back to
            # text-based detection using lingua-language-detector so that
            # downstream components (e.g. Vox engines) always receive a language
            # hint even when acoustic detection is unavailable.
            if result.language is None:
                result = AurisTranscriptResult(
                    text=result.text,
                    language=self._detect_language_from_text(result.text),
                )

            log_info(
                f"[auris_plugin] Transcription via '{name}': "
                f"{result.text[:80]!r} (lang={result.language!r})"
            )
            return result
        except Exception as exc:
            log_error(f"[auris_plugin] Transcription error ({name}): {exc}")
            return None

    # ------------------------------------------------------------------
    # Action support
    # ------------------------------------------------------------------

    @staticmethod
    def get_supported_actions() -> dict:
        return {
            "stt_transcribe": {
                "description": (
                    "Transcribe an audio file to text using the Auris STT subsystem."
                ),
                "required_fields": ["audio_path"],
                "optional_fields": ["mime_type", "engine"],
            }
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        if action_name == "stt_transcribe":
            return {
                "description": (
                    "Convert an audio file to text. Use when the user sends a voice "
                    "message or when audio transcription is needed."
                ),
                "payload": {
                    "audio_path": {
                        "type": "string",
                        "description": "Absolute path to the audio file to transcribe.",
                    },
                    "mime_type": {
                        "type": "string",
                        "description": "Optional MIME type hint, e.g. 'audio/ogg'.",
                        "optional": True,
                    },
                    "engine": {
                        "type": "string",
                        "description": "Optional: override the active Auris engine name.",
                        "optional": True,
                    },
                },
            }
        return {}

    async def handle_custom_action(
        self, action_type: str, payload: dict
    ) -> dict[str, Any]:
        if action_type == "stt_transcribe":
            audio_path: str = payload.get("audio_path", "")
            mime_type: str | None = payload.get("mime_type")
            engine_name: str | None = payload.get("engine")

            result = await self.transcribe_audio(audio_path, mime_type, engine_name)
            if result:
                response: dict[str, Any] = {"status": "success", "text": result.text}
                if result.language is not None:
                    response["language"] = result.language
                return response
            return {"status": "error", "message": "Transcription returned no result."}

        return {"status": "error", "message": f"Unknown action: {action_type}"}

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def refresh_config(self) -> None:
        """Re-read exposed variables (allows WebUI hot-changes)."""
        try:
            # active engine may be "disabled" to deactivate the subsystem
            self._active_engine_name = str(
                config_registry.get_value(
                    "ACTIVE_AURIS_ENGINE",
                    "vosk",
                    value_type=str,
                    group="plugins",
                    component="auris_plugin",
                )
            )
            import json

            raw_settings = config_registry.get_value(
                "AURIS_ENGINE_SETTINGS",
                "{}",
                value_type=str,
                group="plugins",
                component="auris_plugin",
            )
            try:
                self._engine_settings = json.loads(raw_settings or "{}")
            except Exception:
                self._engine_settings = {}

            # Auto model download flag (advanced)
            self._auto_download = bool(
                config_registry.get_value(
                    "MODEL_AUTO_DOWNLOAD",
                    True,
                    value_type=bool,
                    group="plugins",
                    component="auris_plugin",
                )
            )
        except Exception as exc:
            log_warning(f"[auris_plugin] refresh_config failed: {exc}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _import_builtin_engines() -> None:
        """Import built-in Auris engine modules so they self-register.

        Only file-based engines belong here.  Real-time/streaming engines live
        in ``plugins/live_engines/`` and are loaded by the Live registry.
        """
        builtins = [
            # Note: cloud-based engines (e.g. Gemini) are not auto-loaded here;
            # they are registered only when the user explicitly adds them as an
            # external endpoint via the External Engines UI.
            "plugins.auris_engines.vosk_engine",
        ]
        for mod in builtins:
            try:
                __import__(mod)
            except Exception as exc:
                log_warning(
                    f"[auris_plugin] Could not import engine module '{mod}': {exc}"
                )

    @staticmethod
    def _detect_language_from_text(text: str) -> str | None:
        """Return a language code detected from *text* using lingua.

        Used as a fallback when the STT engine cannot determine the spoken
        language acoustically (e.g. Gemini).  Returns ``None`` when the
        detection confidence is too low or ``lingua`` is not installed.
        """
        if not text or not text.strip():
            return None
        try:
            from lingua import LanguageDetectorBuilder  # type: ignore[import]

            detector = (
                LanguageDetectorBuilder.from_all_languages()
                .with_minimum_relative_distance(0.1)
                .build()
            )
            lang = detector.detect_language_of(text)
            if lang is not None:
                code: str = lang.iso_code_639_1.name.lower()
                log_info(f"[auris_plugin] lingua fallback detected language: {code!r}")
                return code
        except Exception as exc:
            log_warning(f"[auris_plugin] lingua language detection failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Module-level singleton (referenced by core_initializer / other plugins)
# ---------------------------------------------------------------------------

PLUGIN_CLASS = AurisPlugin
