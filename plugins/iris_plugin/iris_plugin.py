# plugins/iris_plugin.py
"""Iris — core vision plugin.

Provides the ``vision_describe`` action and routes all image/video analysis
through the Iris engine registry.  Interfaces and other plugins should call
``IrisPlugin.describe_media()`` instead of invoking engines directly.

Engines are registered by importing their modules; this plugin automatically
imports any built-in engines on startup.  Currently all shipping engines are
external-endpoint bridges — no local model is bundled.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from core.ai_plugin_base import AIPluginBase
from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.iris_registry import IRIS_REGISTRY
from core.logging_utils import log_error, log_info, log_warning
from plugins.iris_base import IrisResult

# ---------------------------------------------------------------------------
# Durable media cache — Iris keeps a copy of analysed media so the synth can
# re-inspect it on later turns (the vision_describe action resolves a cached
# path recorded in chat-history metadata).  Managed centrally here, at the
# Iris subsystem level, so every interface shares the same behaviour.
# ---------------------------------------------------------------------------

_IRIS_CACHE_DIR = Path("tmp/iris_cache")
_IRIS_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

# ---------------------------------------------------------------------------
# Config variables (hidden from Settings — Iris is configured via the Engines
# tab / external_endpoints, not the Settings page).
# ---------------------------------------------------------------------------

# Default instruction sent to the vision engine when no custom prompt is
# configured (IRIS_DEFAULT_PROMPT).  A neutral plain-text framing stops
# session-aware LLM backends from replying with structured/JSON output
# instead of a description.
_IRIS_DEFAULT_PROMPT_TEXT = (
    "IMPORTANT: Respond in plain conversational text only. "
    "Do NOT use JSON, XML or any structured format. "
    "Simply describe what you see in the image."
)


config_registry.get_value(
    "ACTIVE_IRIS_ENGINE",
    "disabled",
    value_type=str,
    group="plugins",
    component="iris_plugin",
    hidden=True,
)
config_registry.get_value(
    "IRIS_ENGINE_SETTINGS",
    "{}",
    value_type=str,
    group="plugins",
    component="iris_plugin",
    hidden=True,
)
config_registry.get_value(
    "IRIS_DEFAULT_PROMPT",
    _IRIS_DEFAULT_PROMPT_TEXT,
    value_type=str,
    group="plugins",
    component="iris_plugin",
    hidden=True,
)
config_registry.get_value(
    "IRIS_DEFAULT_MODEL",
    "",
    value_type=str,
    group="plugins",
    component="iris_plugin",
    hidden=True,
)

# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class IrisPlugin(AIPluginBase):
    """Core vision plugin.  Registers supported actions and delegates to engine."""

    display_name = "Iris (Vision)"

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()
        self._active_engine_name: str = "disabled"
        self._engine_settings: dict[str, Any] = {}
        self._default_prompt: str = _IRIS_DEFAULT_PROMPT_TEXT
        self._default_model: str = ""

        # Import built-in engine modules so they self-register.
        # Currently empty — all engines arrive via external endpoints.
        self._import_builtin_engines()

        self.refresh_config()
        register_plugin("iris_plugin", self)
        log_info("[iris_plugin] Initialized.")

    # ------------------------------------------------------------------
    # Public API — used by interfaces and other plugins
    # ------------------------------------------------------------------

    async def describe_media(
        self,
        file_path: str,
        mime_type: str | None = None,
        prompt: str | None = None,
        engine_name: str | None = None,
        model: str | None = None,
    ) -> IrisResult | None:
        """Analyse an image or video file and return a textual description.

        Args:
            file_path:   Absolute or relative path to the media file.
            mime_type:   Optional MIME hint, e.g. ``"image/jpeg"``.
            prompt:      Optional instruction for the engine.  Falls back to
                         ``IRIS_DEFAULT_PROMPT`` when omitted.
            engine_name: Override the active engine for this call.

        Returns:
            :class:`IrisResult` with the description, or ``None`` when the
            engine is disabled or analysis fails.
        """
        self.refresh_config()

        if self._active_engine_name in ("disabled", "inline"):
            # 'inline' has no description engine — image bytes are forwarded to
            # the Cortex engine directly by the message chain instead.
            log_info(
                f"[iris_plugin] Engine '{self._active_engine_name}'; "
                "skipping vision analysis."
            )
            return None

        if not os.path.exists(file_path):
            log_error(f"[iris_plugin] File not found: {file_path}")
            return None

        effective_prompt = prompt or self._default_prompt
        effective_model = model if model is not None else self._default_model or None
        name = engine_name or self._active_engine_name

        try:
            engine = IRIS_REGISTRY.load_engine(name)
        except ValueError as exc:
            log_error(f"[iris_plugin] Cannot load engine '{name}': {exc}")
            return None

        try:
            kwargs: dict[str, str] = {}
            if effective_model is not None:
                kwargs["model"] = effective_model

            if asyncio.iscoroutinefunction(engine.describe_image):
                result: IrisResult | None = await engine.describe_image(
                    file_path,
                    mime_type,
                    effective_prompt,
                    **kwargs,
                )
            else:
                result = await asyncio.to_thread(
                    engine.describe_image,
                    file_path,
                    mime_type,
                    effective_prompt,
                    **kwargs,
                )

            if result is None:
                return None

            # Sanity-check: reject responses that look like JSON action payloads
            # (e.g. {"actions": [...]}).  This happens when the vision engine is
            # backed by a session-aware LLM (Gemini Web via selenium) that
            # responds in the trained SyntH action schema instead of describing
            # the image in plain text.
            desc = result.description.strip() if result.description else ""
            if desc.startswith("{"):
                try:
                    import json as _json

                    _parsed = _json.loads(desc)
                    if isinstance(_parsed, dict) and "actions" in _parsed:
                        log_warning(
                            f"[iris_plugin] Engine '{name}' returned JSON actions instead of "
                            "a vision description — discarding invalid response"
                        )
                        return None
                except (ValueError, TypeError):
                    pass  # Not valid JSON — keep the response as-is

            log_info(
                f"[iris_plugin] Vision analysis via '{name}': "
                f"{result.description[:80]!r} (lang={result.language!r})"
            )
            return result
        except Exception as exc:
            log_error(f"[iris_plugin] Vision analysis error ({name}): {exc}")
            return None

    # ------------------------------------------------------------------
    # Durable media cache
    # ------------------------------------------------------------------

    @classmethod
    def cache_media_bytes(
        cls,
        media_bytes: bytes,
        mime_type: str | None = None,
    ) -> str | None:
        """Persist media bytes into the Iris cache and return the file path.

        The path is content-addressed (SHA-256 of the bytes), so re-caching the
        same media reuses the existing file.  Stale entries are pruned lazily on
        every call.  Returns ``None`` when *media_bytes* is empty or the write
        fails.
        """
        if not media_bytes:
            return None
        try:
            cls._prune_cache()
            _IRIS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(media_bytes).hexdigest()
            suffix = cls._mime_suffix(mime_type)
            target = _IRIS_CACHE_DIR / f"{digest}{suffix}"
            if not target.exists():
                target.write_bytes(media_bytes)
            else:
                # Refresh mtime so the TTL sweep keeps recently-used media.
                os.utime(target, None)
            return str(target.resolve())
        except Exception as exc:
            log_warning(f"[iris_plugin] Failed to cache media: {exc}")
            return None

    @staticmethod
    def _prune_cache() -> None:
        """Remove cached media files older than the configured TTL."""
        try:
            if not _IRIS_CACHE_DIR.exists():
                return
            cutoff = time.time() - _IRIS_CACHE_TTL_SECONDS
            for entry in _IRIS_CACHE_DIR.iterdir():
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        entry.unlink()
                except OSError:
                    continue
        except Exception as exc:
            log_warning(f"[iris_plugin] Cache prune skipped: {exc}")

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def refresh_config(self) -> None:
        """Re-read exposed variables (allows WebUI hot-changes)."""
        try:
            self._active_engine_name = str(
                config_registry.get_value(
                    "ACTIVE_IRIS_ENGINE",
                    "disabled",
                    value_type=str,
                    group="plugins",
                    component="iris_plugin",
                )
            )

            import json

            raw_settings = config_registry.get_value(
                "IRIS_ENGINE_SETTINGS",
                "{}",
                value_type=str,
                group="plugins",
                component="iris_plugin",
            )
            try:
                self._engine_settings = json.loads(raw_settings or "{}")
            except Exception:
                self._engine_settings = {}

            self._default_prompt = str(
                config_registry.get_value(
                    "IRIS_DEFAULT_PROMPT",
                    _IRIS_DEFAULT_PROMPT_TEXT,
                    value_type=str,
                    group="plugins",
                    component="iris_plugin",
                )
            )
            self._default_model = str(
                config_registry.get_value(
                    "IRIS_DEFAULT_MODEL",
                    "",
                    value_type=str,
                    group="plugins",
                    component="iris_plugin",
                )
            )
        except Exception as exc:
            log_warning(f"[iris_plugin] refresh_config failed: {exc}")

    # ------------------------------------------------------------------
    # Vision helpers
    # ------------------------------------------------------------------

    async def _resolve_media_target(
        self,
        image_path: str,
        mime_type: str | None,
        *,
        bot: Any | None = None,
        original_message: Any | None = None,
    ) -> tuple[str | None, bool, str | None]:
        if image_path and os.path.exists(image_path):
            return image_path, False, mime_type

        inline_path, inline_mime = self._materialize_inline_media(image_path, mime_type)
        if inline_path:
            return inline_path, True, inline_mime

        if original_message is not None:
            (
                message_path,
                should_cleanup,
                message_mime,
            ) = await self._materialize_message_media(
                bot,
                original_message,
            )
            if message_path:
                return message_path, should_cleanup, message_mime or mime_type

        # Final fallback: re-inspect a previously cached image referenced by the
        # conversation history. This lets the synth look again at an image it was
        # already shown in an earlier turn (e.g. "look at the menu again") even
        # when no fresh attachment is present on the current message.
        if original_message is not None:
            cached_path, cached_mime = await self._resolve_cached_media(
                original_message
            )
            if cached_path:
                return cached_path, False, cached_mime or mime_type

        return None, False, mime_type

    @classmethod
    async def _resolve_cached_media(
        cls,
        original_message: Any,
    ) -> tuple[str | None, str | None]:
        """Find the most recent cached Iris image for this conversation.

        Scans the chat history for the message's ``interface_path`` and returns
        the newest ``iris_cached_path`` (recorded when the image was first
        described) whose cached file still exists on disk, so the vision engine
        can re-inspect it. Returns ``(None, None)`` when nothing is available.
        """
        interface_path = str(getattr(original_message, "interface_path", "") or "")
        if not interface_path:
            return None, None

        try:
            from core.chat_history_cache import load_chat_history

            history = await load_chat_history(
                interface_path,
                limit=50,
                match_chat_level=True,
            )
        except Exception as exc:
            log_warning(f"[iris_plugin] Failed to load history for cache lookup: {exc}")
            return None, None

        for msg in reversed(list(history)):
            if not isinstance(msg, dict):
                continue
            meta = msg.get("metadata")
            if isinstance(meta, str):
                try:
                    import json as _json

                    meta = _json.loads(meta)
                except Exception:
                    continue
            if not isinstance(meta, dict):
                continue
            cached_path = meta.get("iris_cached_path")
            if not cached_path:
                continue
            if os.path.exists(str(cached_path)):
                return str(cached_path), meta.get("iris_cached_mime")

        return None, None

    @staticmethod
    def _infer_interface_name(original_message: Any) -> str:
        interface_name = getattr(original_message, "interface_name", None) or getattr(
            original_message, "interface", None
        )
        if interface_name:
            return str(interface_name)
        interface_path = str(getattr(original_message, "interface_path", "") or "")
        if "/" in interface_path:
            return interface_path.split("/", 1)[0]
        return interface_path

    @staticmethod
    def _mime_suffix(mime_type: str | None) -> str:
        if not mime_type or "/" not in mime_type:
            return ""
        return f".{mime_type.split('/', 1)[1].split(';', 1)[0]}"

    @classmethod
    def _write_temp_media(
        cls,
        media_bytes: bytes,
        mime_type: str | None,
    ) -> str | None:
        if not media_bytes:
            return None
        suffix = cls._mime_suffix(mime_type)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(media_bytes)
            return tmp.name

    @classmethod
    def _materialize_inline_media(
        cls,
        image_path: str,
        mime_type: str | None,
    ) -> tuple[str | None, str | None]:
        if not image_path:
            return None, mime_type

        raw_value = image_path.strip()
        effective_mime = mime_type
        if raw_value.startswith("data:"):
            header, _, raw_value = raw_value.partition(",")
            if ";base64" not in header or not raw_value:
                return None, mime_type
            if effective_mime is None:
                effective_mime = header[5:].split(";", 1)[0] or mime_type

        compact_value = "".join(raw_value.split())
        media_hint = bool(mime_type and mime_type.startswith(("image/", "video/")))
        if not media_hint and len(compact_value) < 64:
            return None, effective_mime

        try:
            media_bytes = base64.b64decode(compact_value, validate=True)
        except Exception:
            return None, effective_mime

        return cls._write_temp_media(media_bytes, effective_mime), effective_mime

    async def _materialize_message_media(
        self,
        bot: Any | None,
        original_message: Any,
    ) -> tuple[str | None, bool, str | None]:
        try:
            from core.plugin_instance import _extract_multimodal_attachments

            interface_name = self._infer_interface_name(original_message)
            attachments = await _extract_multimodal_attachments(
                bot,
                original_message,
                interface_name,
            )
        except Exception as exc:
            log_warning(f"[iris_plugin] Failed to extract message attachments: {exc}")
            return None, False, None

        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            attachment_mime = str(attachment.get("mime_type") or "")
            if not attachment_mime.startswith(("image/", "video/")):
                continue

            file_path = str(attachment.get("path") or attachment.get("file_path") or "")
            if file_path and os.path.exists(file_path):
                return file_path, False, attachment_mime

            inline_data = str(attachment.get("data") or attachment.get("base64") or "")
            inline_path, effective_mime = self._materialize_inline_media(
                inline_data,
                attachment_mime,
            )
            if inline_path:
                return inline_path, True, effective_mime or attachment_mime

        return None, False, None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _import_builtin_engines() -> None:
        """Import built-in Iris engine modules so they self-register.

        Currently empty — all engines are registered via external endpoints.
        Local vision models may be added here in the future.
        """
        builtins: list[str] = []
        for mod in builtins:
            try:
                __import__(mod)
            except Exception as exc:
                log_warning(
                    f"[iris_plugin] Could not import engine module '{mod}': {exc}"
                )


# ---------------------------------------------------------------------------
# Module-level sentinel (referenced by core_initializer / other plugins)
# ---------------------------------------------------------------------------

PLUGIN_CLASS = IrisPlugin
