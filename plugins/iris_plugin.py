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
import os
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
        name = engine_name or self._active_engine_name

        try:
            engine = IRIS_REGISTRY.load_engine(name)
        except ValueError as exc:
            log_error(f"[iris_plugin] Cannot load engine '{name}': {exc}")
            return None

        try:
            if asyncio.iscoroutinefunction(engine.describe_image):
                result: IrisResult | None = await engine.describe_image(
                    file_path, mime_type, effective_prompt
                )
            else:
                result = await asyncio.to_thread(
                    engine.describe_image, file_path, mime_type, effective_prompt
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
                "required_fields": ["image_path"],
                "optional_fields": ["mime_type", "prompt", "engine"],
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
                        "description": "Absolute path to the image or video file to analyse.",
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
                },
            }
        return {}

    async def handle_custom_action(
        self, action_type: str, payload: dict
    ) -> dict[str, Any]:
        if action_type == "vision_describe":
            image_path: str = payload.get("image_path", "")
            mime_type: str | None = payload.get("mime_type")
            prompt: str | None = payload.get("prompt")
            engine_name: str | None = payload.get("engine")

            result = await self.describe_media(
                image_path, mime_type, prompt, engine_name
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
            return {"status": "error", "message": "Vision analysis returned no result."}

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
        except Exception as exc:
            log_warning(f"[iris_plugin] refresh_config failed: {exc}")

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
