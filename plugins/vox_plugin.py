# plugins/vox_plugin.py
"""Vox — core TTS + lip-sync plugin.

Centralises the complete text-to-speech output pipeline:
  1. Clean text (strip emoji / markup).
  2. Delegate to the active Vox engine for audio generation.
  3. Write the audio blob to disk (handles WAV and raw-PCM engines).
  4. Dispatch audio to the requesting interface (WebUI, Discord, Telegram, …).
  5. Trigger lip-sync animation in the WebUI if available.
  6. Optionally fall back to plain-text when generation fails.

Engines only need to produce bytes.  Everything else is handled here.
No interface, no plugin, and no engine should re-implement this pipeline.

Backward-compatibility: existing ``TTS_ENABLED`` / ``TTS_ENDPOINTS`` /
``TTS_FALLBACK_TO_TEXT`` variables are preserved and mapped to their Vox
equivalents so deployments that used ``tts_lipsync`` continue to work
without configuration changes.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import wave
from pathlib import Path
from typing import Any

from core.ai_plugin_base import AIPluginBase
from core.config_manager import config_registry
from core.core_initializer import register_plugin, INTERFACE_REGISTRY
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.variables_engine import register_exposed_var
from core.vox_registry import VOX_REGISTRY

# ---------------------------------------------------------------------------
# Language detection helper (lingua — replaces langdetect for better accuracy)
# ---------------------------------------------------------------------------

_lingua_detector: Any | None = None
_lingua_detector_lock = threading.Lock()


def _get_lingua_detector() -> Any | None:
    """Return the singleton ``lingua`` detector, building it on first call.

    Uses ``lingua-language-detector`` (much more accurate than ``langdetect``
    for short texts and closely-related languages such as Italian / Spanish).
    Returns ``None`` gracefully when the package is not installed.
    """
    global _lingua_detector
    if _lingua_detector is not None:
        return _lingua_detector
    with _lingua_detector_lock:
        if _lingua_detector is not None:  # double-checked
            return _lingua_detector
        try:
            from lingua import LanguageDetectorBuilder

            _lingua_detector = (
                LanguageDetectorBuilder.from_all_languages()
                .with_minimum_relative_distance(0.1)
                .build()
            )
            log_info("[vox_plugin] lingua detector initialized.")
        except Exception as exc:
            log_warning(f"[vox_plugin] lingua not available: {exc}")
    return _lingua_detector


def _detect_language(text: str) -> str | None:
    """Return a BCP-47 / ISO-639-1 language code or ``None``.

    Uses ``lingua-language-detector`` for accurate detection, especially on
    short texts and closely-related language pairs (e.g. Italian / Spanish).
    Returns ``None`` when confidence is below the internal threshold or the
    detector is unavailable.
    """
    if not text or not text.strip():
        return None

    detector = _get_lingua_detector()
    if detector is not None:
        try:
            lang = detector.detect_language_of(text)
            if lang is not None:
                code = lang.iso_code_639_1.name.lower()
                log_debug(f"[vox_plugin] lingua detected: {code!r} for {text[:50]!r}")
                return code
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Exposed config variables
# ---------------------------------------------------------------------------


register_exposed_var(
    "ACTIVE_VOX_ENGINE",
    label="Active Vox Engine",
    default="disabled",
    value_type=str,
    ui_type="string",
    description=(
        "Name of the active Vox TTS engine (e.g. 'disabled', 'kitten', 'http'). "
        "Set to 'disabled' to turn off speech output."
    ),
    scope="plugins",
    component="vox_plugin",
    advanced=False,
)

register_exposed_var(
    "VOX_ENGINE_SETTINGS",
    label="Vox Engine Settings (JSON)",
    default="{}",
    value_type=str,
    ui_type="string",
    description="Optional JSON dict of per-engine settings forwarded to the active Vox engine.",
    scope="plugins",
    component="vox_plugin",
    advanced=True,
)

register_exposed_var(
    "VOX_OUTPUT_DIR",
    label="Vox Audio Output Directory",
    default="res/synth_webui/static/audio/tts",
    value_type=str,
    ui_type="string",
    description="Directory where generated TTS audio files are stored.",
    scope="plugins",
    component="vox_plugin",
    advanced=True,
)

register_exposed_var(
    "VOX_TIMEOUT_SECONDS",
    label="Vox Generation Timeout (s)",
    default=300,
    value_type=int,
    ui_type="number",
    description="Timeout for TTS generation requests in seconds.",
    scope="plugins",
    component="vox_plugin",
    advanced=True,
)

register_exposed_var(
    "VOX_FALLBACK_TO_TEXT",
    label="Vox Fallback to Text",
    default=True,
    value_type=bool,
    ui_type="boolean",
    description="Send a plain-text message when TTS generation fails.",
    scope="plugins",
    component="vox_plugin",
    advanced=False,
)

register_exposed_var(
    "VOX_AUDIO_CACHE_SIZE",
    label="Vox Audio Cache Size",
    default=40,
    value_type=int,
    ui_type="number",
    description=(
        "Maximum number of TTS audio clips the WebUI keeps in memory for replay. "
        "When the limit is exceeded, older clips are silently dropped. Default: 40."
    ),
    scope="plugins",
    component="vox_plugin",
    advanced=True,
)

# Legacy aliases (read by the HTTP engine via its original config keys)
# tts_lipsync variables are intentionally kept so existing .env files keep working.

# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

_EMOJI_RE = re.compile(r"[^\w\s,.!?;:\'\-\"\u2018\u2019]+")
_MULTI_SPACE_RE = re.compile(r"\s+")


class VoxPlugin(AIPluginBase):
    """Core TTS + lip-sync plugin.  Interfaces and agents call ``speak()``."""

    display_name = "Vox (TTS + Lip-Sync)"

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()
        # explicit enabled flag removed; use engine name "disabled" instead
        self._active_engine_name: str = "http"
        self._engine_settings: dict[str, Any] = {}
        self._output_dir: Path = Path("res/synth_webui/static/audio/tts")
        self._fallback_to_text: bool = True

        # Pre-warm the lingua detector in a daemon thread so it is ready before
        # the first detect_language() call arrives from recon.  Building all
        # language models can take 1-3 s on first run; doing it eagerly avoids
        # cold-start timeouts (default LANGUAGE_DETECTOR_TIMEOUT = 2 s).
        threading.Thread(
            target=_get_lingua_detector,
            daemon=True,
            name="lingua-warmup",
        ).start()

        # Import built-in engine modules so they self-register
        self._import_builtin_engines()

        self.refresh_config()
        register_plugin("vox_plugin", self)
        log_info("[vox_plugin] Initialized.")

    # ------------------------------------------------------------------
    # Public API — used by interfaces and other plugins
    # ------------------------------------------------------------------

    async def speak(
        self,
        text: str,
        interface_path: str | None = None,
        context: dict | None = None,
        original_message: Any = None,
        emotion: str | None = None,
        engine_name: str | None = None,
        merged_text: str | None = None,
        allow_fallback: bool = True,
    ) -> dict[str, Any]:
        """Full TTS pipeline: generate → write → dispatch → lip-sync.

        Args:
            text:             Text to synthesise.
            interface_path:   Destination interface path (e.g. ``synth_webui/session_id``).
            context:          Message chain context dict.
            original_message: Original message object (used to recover interface_path).
            emotion:          Optional emotion/style hint forwarded to the engine.
            engine_name:      Override the active engine for this call.
            merged_text:      Pre-resolved display text (used as fallback caption).

        Returns:
            ``{"status": "success"|"skipped"|"error", ...}``
        """
        self.refresh_config()

        # Determine which engine is actually being used for this call.
        chosen = engine_name or self._active_engine_name
        # A value of "disabled" means the subsystem is turned off.
        if chosen == "disabled":
            log_info("[vox_plugin] Engine disabled; skipping.")
            if allow_fallback:
                _ip = interface_path
                if not _ip and context:
                    _ip = str(context.get("interface_path") or "")
                if not _ip and original_message:
                    _ip = getattr(original_message, "interface_path", None)
                _fallback_text = merged_text or text
                if _ip and _fallback_text:
                    log_debug(
                        "[vox_plugin] Disabled but allow_fallback=True — sending text fallback."
                    )
                    return await self._send_fallback(_fallback_text, str(_ip))
            return {"status": "skipped", "reason": "vox_disabled"}

        # --- Resolve interface_path from context / message if not provided ---
        if not interface_path and context:
            interface_path = context.get("interface_path")
        if not interface_path and original_message:
            interface_path = getattr(original_message, "interface_path", None)

        async def _fallback(msg_text: str) -> dict[str, Any]:
            """Delegate to _send_fallback only when allowed.

            When ``allow_fallback=False`` (auto-injected TTS actions) we suppress
            the text fallback to avoid sending a duplicate: the text was already
            dispatched by the preceding ``message_*_bot`` action.
            """
            if allow_fallback:
                return await self._send_fallback(msg_text, interface_path)
            log_warning(
                "[vox_plugin] TTS failed (auto-injected action); "
                "suppressing text fallback to avoid duplicate message."
            )
            return {"status": "error", "reason": "tts_failed_no_fallback_allowed"}

        # --- Clean text for the engine ---
        clean = _EMOJI_RE.sub("", text)
        clean = _MULTI_SPACE_RE.sub(" ", clean).strip()
        if not clean:
            return {"status": "skipped", "reason": "empty_text"}

        # Attempt language detection on the cleaned text so that downstream
        # engines (e.g. cloud APIs) can pick an appropriate voice or model.
        # Uses lingua-language-detector for much better accuracy on short texts
        # and on closely-related language pairs (e.g. Italian / Spanish).
        detected_lang: str | None = _detect_language(clean)
        if detected_lang:
            log_info(
                f"[vox_plugin] detected text language: '{detected_lang}'"
            )  # pragma: no branch

        # --- Load engine ---
        name = engine_name or self._active_engine_name
        try:
            engine = VOX_REGISTRY.load_engine(name)
        except ValueError as exc:
            log_error(f"[vox_plugin] Cannot load engine '{name}': {exc}")
            return await _fallback(merged_text or text)

        # --- Generate audio ---
        try:
            # pass detected language hint to engine if available; engines may
            # ignore unexpected kwargs.
            kwargs: dict[str, Any] = {}
            if detected_lang:
                kwargs["language"] = detected_lang
            audio_bytes: bytes | None = await asyncio.to_thread(
                engine.generate_tts, clean, emotion, **kwargs
            )
        except Exception as exc:
            log_error(f"[vox_plugin] Engine '{name}' generation error: {exc}")
            audio_bytes = None

        if not audio_bytes:
            log_warning(f"[vox_plugin] Engine '{name}' returned no audio.")
            return await _fallback(merged_text or text)

        # --- Write to disk ---
        filename = f"vox_{int(time.time())}.wav"
        out_path = self._output_dir / filename
        try:
            self._write_audio(out_path, audio_bytes, engine)
        except Exception as exc:
            log_error(f"[vox_plugin] Failed to write audio file: {exc}")
            return await _fallback(merged_text or text)

        if not out_path.exists():
            log_error(f"[vox_plugin] Written file not found: {out_path}")
            return await _fallback(merged_text or text)

        # --- Optional lip-sync metadata ---
        lipsync_data: dict | None = None
        get_ls = getattr(engine, "get_lipsync_data", None)
        if callable(get_ls):
            try:
                lipsync_data = get_ls(audio_bytes)
            except Exception:
                pass

        # --- Dispatch to interface ---
        await self._dispatch(
            audio_path=out_path,
            interface_path=interface_path,
            caption=merged_text or text,
            lipsync_data=lipsync_data,
            context=context,
            original_message=original_message,
        )

        return {"status": "success", "audio_path": str(out_path), "filename": filename}

    # ------------------------------------------------------------------
    # Language detection helper (used by recon and other components)
    # ------------------------------------------------------------------

    def detect_language(
        self, message: Any, interface_path: str | None = None
    ) -> str | None:
        """Return a detected language code (e.g. 'en' or 'it').

        This method implements the *language detector plugin* contract used by
        :mod:`core.recon`.  Uses ``lingua-language-detector`` for significantly
        better accuracy on short texts and on closely-related language pairs
        (e.g. Italian / Spanish / Portuguese).  Returns ``None`` gracefully
        when detection fails or confidence is too low.

        ``message`` may be a plain string, a dict (with a ``"text"`` key), or
        any object that exposes a ``.text`` attribute (e.g. ``MessageWrapper``).
        """
        text: str
        if isinstance(message, str):
            text = message
        elif isinstance(message, dict):
            text = str(message.get("text") or "")
        elif hasattr(message, "text"):
            # handles MessageWrapper and any similar proxy object;
            # .text may be None for voice messages before transcription
            raw = message.text
            text = str(raw) if raw is not None else ""
        else:
            # last resort: str() — at least we tried
            text = str(message)
        result = _detect_language(text)
        log_debug(f"[vox_plugin] detect_language: input={text[:60]!r} → {result!r}")
        return result

    # ------------------------------------------------------------------
    # Action wiring
    # ------------------------------------------------------------------

    @staticmethod
    def get_supported_actions() -> dict:
        return {
            "tts_speak": {
                "description": "Generate speech from text and send it to the active interface.",
                "required_fields": ["text"],
                "optional_fields": ["emo"],
            }
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        if action_name == "tts_speak":
            return {
                "description": (
                    "Generate speech audio from text. The audio will be automatically "
                    "dispatched to the chat with lip-sync animation."
                ),
                "payload": {
                    "text": {"type": "string", "description": "Text to synthesise."},
                    "emo": {
                        "type": "string",
                        "description": "Optional emotion style hint.",
                        "optional": True,
                    },
                },
            }
        return {}

    async def handle_custom_action(
        self, action_type: str, payload: dict
    ) -> dict[str, Any]:
        if action_type == "tts_speak":
            return await self.speak(
                text=payload.get("text", ""),
                emotion=payload.get("emo"),
                merged_text=payload.get("__merged_text"),
            )
        return {"status": "error", "message": f"Unknown action: {action_type}"}

    async def execute_action(
        self,
        action: dict,
        context: dict,
        bot: Any,
        original_message: Any,
    ) -> dict[str, Any]:
        payload = action.get("payload", {})

        # Skip TTS for Grillo internal beats
        is_grillo_internal = context.get("grillo_beat", False) and context.get(
            "beat_type"
        ) not in ("outreach", None)
        if is_grillo_internal:
            return {"status": "skipped", "reason": "grillo_internal_beat"}

        return await self.speak(
            text=payload.get("text", ""),
            emotion=payload.get("emo"),
            interface_path=context.get("interface_path"),
            context=context,
            original_message=original_message,
            merged_text=payload.get("__merged_text"),
            # Suppress text fallback for auto-injected TTS: text was already
            # dispatched by message_*_bot and a duplicate would confuse the user.
            allow_fallback=not payload.get("__auto_injected", False),
        )

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def refresh_config(self) -> None:
        """Re-read exposed variables (allows WebUI hot-changes)."""
        try:
            import json

            # read active engine; "disabled" deactivates the subsystem
            self._active_engine_name = str(
                config_registry.get_value(
                    "ACTIVE_VOX_ENGINE",
                    "http",
                    value_type=str,
                    group="plugins",
                    component="vox_plugin",
                )
            )

            raw_settings = config_registry.get_value(
                "VOX_ENGINE_SETTINGS",
                "{}",
                value_type=str,
                group="plugins",
                component="vox_plugin",
            )
            try:
                self._engine_settings = json.loads(raw_settings or "{}")
            except Exception:
                self._engine_settings = {}

            output_dir_raw = config_registry.get_value(
                "VOX_OUTPUT_DIR",
                "res/synth_webui/static/audio/tts",
                value_type=str,
                group="plugins",
                component="vox_plugin",
            )
            self._output_dir = Path(str(output_dir_raw))
            self._output_dir.mkdir(parents=True, exist_ok=True)

            self._fallback_to_text = bool(
                config_registry.get_value(
                    "VOX_FALLBACK_TO_TEXT",
                    True,
                    value_type=bool,
                    group="plugins",
                    component="vox_plugin",
                )
            )
        except Exception as exc:
            log_warning(f"[vox_plugin] refresh_config failed: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_audio(self, path: Path, audio_bytes: bytes, engine: Any) -> None:
        """Write audio bytes to disk, wrapping raw PCM in WAV if needed."""
        fmt = getattr(engine, "output_format", "wav")
        if fmt == "pcm" or not audio_bytes.startswith(b"RIFF"):
            sr = getattr(engine, "sample_rate", 22050)
            ch = getattr(engine, "channels", 1)
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(ch)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(audio_bytes)
        else:
            path.write_bytes(audio_bytes)

    async def _dispatch(
        self,
        audio_path: Path,
        interface_path: str | None,
        caption: str,
        lipsync_data: dict | None,
        context: dict | None,
        original_message: Any,
    ) -> None:
        """Dispatch generated audio to the correct interface."""
        if not interface_path:
            log_warning("[vox_plugin] No interface_path; cannot dispatch audio.")
            return

        try:
            from core.interface_path_utils import parse_interface_path

            iface_name, levels = parse_interface_path(interface_path)
            target_iface = INTERFACE_REGISTRY.get(iface_name)

            if not target_iface:
                log_warning(
                    f"[vox_plugin] Interface '{iface_name}' not found in registry."
                )
                return

            if iface_name == "synth_webui" and hasattr(target_iface, "send_tts_audio"):
                session_id = levels[0] if levels else None
                if session_id:
                    await target_iface.send_tts_audio(
                        session_id=session_id,
                        audio_path=str(audio_path),
                        text=caption,
                        lipsync_data=lipsync_data,
                    )

            elif iface_name == "discord_bot" and hasattr(target_iface, "send_message"):
                await target_iface.send_message(
                    {
                        "interface_path": interface_path,
                        "audio": str(audio_path),
                        "text": caption,
                    }
                )

            elif iface_name == "telegram_bot" and hasattr(
                target_iface, "execute_action"
            ):
                _, levels_ = parse_interface_path(interface_path)
                if levels_:
                    await target_iface.execute_action(
                        {
                            "type": "audio_telegram_bot",
                            "payload": {
                                "interface_path": interface_path,
                                "audio": str(audio_path),
                                "caption": caption,
                            },
                        },
                        context or {},
                        None,
                        original_message,
                    )

            else:
                # Generic fallback: send audio first, then text as a separate
                # message immediately after (for interfaces that don't support
                # native audio+caption in a single call).
                if hasattr(target_iface, "send_message"):
                    await target_iface.send_message(
                        {
                            "interface_path": interface_path,
                            "audio": str(audio_path),
                        }
                    )
                    if caption:
                        await target_iface.send_message(
                            {
                                "interface_path": interface_path,
                                "text": caption,
                            }
                        )

        except Exception as exc:
            log_error(f"[vox_plugin] Dispatch error: {exc}")

    async def _send_fallback(
        self, text: str, interface_path: str | None
    ) -> dict[str, Any]:
        """Send a plain-text message when TTS fails (if fallback is enabled)."""
        if not self._fallback_to_text or not text or not interface_path:
            return {"status": "error", "reason": "tts_failed_no_fallback"}

        try:
            from core.interface_path_utils import parse_interface_path

            iface_name, _ = parse_interface_path(interface_path)
            target_iface = INTERFACE_REGISTRY.get(iface_name)
            if target_iface and hasattr(target_iface, "send_message"):
                await target_iface.send_message(
                    {"interface_path": interface_path, "text": text}
                )
                log_info("[vox_plugin] Text-only fallback sent.")
        except Exception as exc:
            log_error(f"[vox_plugin] Fallback send failed: {exc}")

        return {"status": "error", "reason": "tts_failed_fallback_sent"}

    @staticmethod
    def _import_builtin_engines() -> None:
        """Import built-in Vox engine modules so they self-register."""
        builtins = [
            "plugins.vox_engines.http",
            # chatterbox moved to _dev; not imported by default
            "plugins.vox_engines.kitten",
        ]
        for mod in builtins:
            try:
                __import__(mod)
            except Exception as exc:
                log_warning(
                    f"[vox_plugin] Could not import engine module '{mod}': {exc}"
                )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

PLUGIN_CLASS = VoxPlugin
