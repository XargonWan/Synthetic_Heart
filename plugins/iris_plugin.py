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
import os
import tempfile
from typing import Any

from core.ai_plugin_base import AIPluginBase
from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.iris_registry import IRIS_REGISTRY
from core.logging_utils import log_error, log_info, log_warning
from core.variables_engine import register_exposed_var
from plugins.iris_base import IrisResult

# ---------------------------------------------------------------------------
# Exposed config variables
# ---------------------------------------------------------------------------


register_exposed_var(
    "ACTIVE_IRIS_ENGINE",
    label="Active Iris Engine",
    default="disabled",
    value_type=str,
    ui_type="string",
    description=(
        "Name of the active Iris vision engine (e.g. 'selenium-llm-engine'). "
        "Set to 'disabled' to turn off the Iris subsystem."
    ),
    scope="plugins",
    component="iris_plugin",
    advanced=False,
)

register_exposed_var(
    "IRIS_ENGINE_SETTINGS",
    label="Iris Engine Settings (JSON)",
    default="{}",
    value_type=str,
    ui_type="string",
    description="Optional JSON dict of per-engine settings passed to the active Iris engine.",
    scope="plugins",
    component="iris_plugin",
    advanced=True,
)

register_exposed_var(
    "IRIS_DEFAULT_PROMPT",
    label="Iris Default Prompt",
    default="Describe this image in detail.",
    value_type=str,
    ui_type="string",
    description=(
        "Default instruction sent to the vision engine when no explicit prompt "
        "is provided by the action payload."
    ),
    scope="plugins",
    component="iris_plugin",
    advanced=True,
)
register_exposed_var(
    "IRIS_DEFAULT_MODEL",
    label="Iris Default Model",
    default="",
    value_type=str,
    ui_type="string",
    description=(
        "Optional model name used for Iris vision requests. "
        "When set, this model is used instead of the endpoint's default model."
    ),
    scope="plugins",
    component="iris_plugin",
    advanced=True,
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
        self._default_prompt: str = "Describe this image in detail."
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

        if self._active_engine_name == "disabled":
            log_info("[iris_plugin] Engine disabled; skipping vision analysis.")
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

            log_info(
                f"[iris_plugin] Vision analysis via '{name}': "
                f"{result.description[:80]!r} (lang={result.language!r})"
            )
            return result
        except Exception as exc:
            log_error(f"[iris_plugin] Vision analysis error ({name}): {exc}")
            return None

    # ------------------------------------------------------------------
    # Action support
    # ------------------------------------------------------------------

    @staticmethod
    def get_supported_actions() -> dict:
        return {
            "vision_describe": {
                "description": (
                    "Analyse an image or video file and return a textual description "
                    "using the Iris vision subsystem."
                ),
                "required_fields": [],
                "optional_fields": [
                    "image_path",
                    "mime_type",
                    "prompt",
                    "engine",
                    "model",
                ],
            }
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        if action_name == "vision_describe":
            return {
                "description": (
                    "Analyse an image or video file.  Use when the user sends an image "
                    "or when visual understanding is needed."
                ),
                "payload": {
                    "image_path": {
                        "type": "string",
                        "description": (
                            "Optional absolute path or base64 data URL for the image/video. "
                            "When omitted, the current message attachment is analysed automatically."
                        ),
                        "optional": True,
                    },
                    "mime_type": {
                        "type": "string",
                        "description": "Optional MIME type hint, e.g. 'image/jpeg'.",
                        "optional": True,
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Optional instruction for the vision engine.",
                        "optional": True,
                    },
                    "engine": {
                        "type": "string",
                        "description": "Optional: override the active Iris engine name.",
                        "optional": True,
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional: override the model used by the Iris vision engine.",
                        "optional": True,
                    },
                },
            }
        return {}

    async def execute_action(
        self,
        action: dict,
        context: dict,
        bot: Any,
        original_message: Any,
    ) -> dict[str, Any]:
        action_type = action.get("type")
        payload = action.get("payload", {}) or {}
        if action_type != "vision_describe":
            return await super().execute_action(action, context, bot, original_message)
        if not isinstance(payload, dict):
            payload = dict(payload)
        return await self._run_vision_describe(
            payload,
            bot=bot,
            original_message=original_message,
        )

    async def handle_custom_action(
        self, action_type: str, payload: dict
    ) -> dict[str, Any]:
        if action_type == "vision_describe":
            return await self._run_vision_describe(payload)

        return {"status": "error", "message": f"Unknown action: {action_type}"}

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
                    "Describe this image in detail.",
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

    async def _run_vision_describe(
        self,
        payload: dict[str, Any],
        *,
        bot: Any | None = None,
        original_message: Any | None = None,
    ) -> dict[str, Any]:
        image_path = str(payload.get("image_path", "") or "").strip()
        mime_type: str | None = payload.get("mime_type")
        prompt: str | None = payload.get("prompt")
        engine_name: str | None = payload.get("engine")
        model: str | None = payload.get("model")

        (
            resolved_path,
            should_cleanup,
            effective_mime,
        ) = await self._resolve_media_target(
            image_path,
            mime_type,
            bot=bot,
            original_message=original_message,
        )
        if not resolved_path:
            return {
                "status": "error",
                "message": "No image available for vision analysis.",
            }

        try:
            result = await self.describe_media(
                resolved_path,
                effective_mime,
                prompt,
                engine_name,
                model,
            )
            if result:
                response: dict[str, Any] = {
                    "status": "success",
                    "description": result.description,
                }
                if result.language is not None:
                    response["language"] = result.language
                if result.confidence is not None:
                    response["confidence"] = result.confidence
                return response
            return {
                "status": "error",
                "message": "Vision analysis returned no result.",
            }
        finally:
            if should_cleanup and resolved_path:
                try:
                    os.remove(resolved_path)
                except OSError:
                    pass

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

        return None, False, mime_type

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
