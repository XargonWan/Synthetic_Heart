# cortex/llm_provider/openrouter.py
"""OpenRouter LLM Engine for Synthetic Heart.

This engine uses the OpenRouter API (OpenAI-compatible) to communicate with
a wide range of LLM providers through a single unified endpoint.

Key features:
- Auto-fetches model catalog with capabilities (modality, context, pricing)
- Multi-model routing: different models per scope and action type
- Multimodal support: images for vision-capable models
- Full bidirectional context injection via standard SyntH engine contract
"""

from __future__ import annotations

import asyncio
import base64
import copy
import fnmatch
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from core.ai_plugin_base import AIPluginBase
from core.config_manager import config_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning

ENGINE_LABEL = "OpenRouter — multi-provider LLM gateway (OpenAI-compatible)"

# ---------------------------------------------------------------------------
# WebUI variable registration (always visible so keys can be set before use)
# ---------------------------------------------------------------------------
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "OPENROUTER_API_KEY",
        label="OpenRouter API Key",
        default="",
        value_type=str,
        ui_type="password",
        description="API key for OpenRouter (https://openrouter.ai/keys).",
        scope="llm",
        component="openrouter",
        tags=["cortex_engine", "sensitive"],
        needs_component_reload=True,
    )
    register_exposed_var(
        "OPENROUTER_BASE_URL",
        label="OpenRouter Base URL",
        default="https://openrouter.ai/api/v1",
        value_type=str,
        ui_type="string",
        description="Base URL for the OpenRouter API.",
        scope="llm",
        component="openrouter",
        tags=["cortex_engine"],
        advanced=True,
        needs_component_reload=True,
    )
    register_exposed_var(
        "OPENROUTER_DEFAULT_MODEL",
        label="Default Model",
        default="x-ai/grok-4.1-fast",
        value_type=str,
        ui_type="combobox",
        description="Default OpenRouter model used when no scope/action override matches.",
        scope="llm",
        component="openrouter",
        tags=["cortex_engine"],
        needs_component_reload=False,
    )
    register_exposed_var(
        "OPENROUTER_SITE_URL",
        label="Site URL (Referer)",
        default="",
        value_type=str,
        ui_type="string",
        description="Sent as HTTP-Referer header. Used for OpenRouter rankings.",
        scope="llm",
        component="openrouter",
        tags=["cortex_engine"],
        advanced=True,
    )
    register_exposed_var(
        "OPENROUTER_APP_NAME",
        label="App Name (X-Title)",
        default="Synthetic Heart",
        value_type=str,
        ui_type="string",
        description="Sent as X-Title header. Shown on OpenRouter rankings.",
        scope="llm",
        component="openrouter",
        tags=["cortex_engine"],
        advanced=True,
    )
    register_exposed_var(
        "OPENROUTER_MODEL_ROUTES",
        label="Model Routes",
        default="{}",
        value_type="json",
        ui_type="json",
        description=(
            "JSON mapping of scopes and action types to models. "
            'Example: {"scopes": {"grillo": "anthropic/claude-haiku-3.5"}, '
            '"actions": {"create_personal_diary_entry": "anthropic/claude-haiku-3.5"}}'
        ),
        scope="llm",
        component="openrouter",
        tags=["cortex_engine"],
        advanced=True,
    )
    register_exposed_var(
        "OPENROUTER_CATALOG_REFRESH_MINUTES",
        label="Catalog Refresh (min)",
        default="30",
        value_type=int,
        ui_type="number",
        description="How often to refresh the model catalog from OpenRouter (minutes).",
        scope="llm",
        component="openrouter",
        tags=["cortex_engine"],
        advanced=True,
    )
except Exception:
    pass

# ---------------------------------------------------------------------------
# Config variables (auto-updating)
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = config_registry.get_var(
    "OPENROUTER_API_KEY",
    "",
    label="OpenRouter API Key",
    description="API key for OpenRouter.",
    group="llm",
    component="openrouter",
    sensitive=True,
)

OPENROUTER_BASE_URL = config_registry.get_var(
    "OPENROUTER_BASE_URL",
    "https://openrouter.ai/api/v1",
    label="OpenRouter Base URL",
    description="Base URL for the OpenRouter API.",
    group="llm",
    component="openrouter",
    advanced=True,
)

OPENROUTER_DEFAULT_MODEL = config_registry.get_var(
    "OPENROUTER_DEFAULT_MODEL",
    "x-ai/grok-4.1-fast",
    label="Default Model",
    description="Default OpenRouter model.",
    group="llm",
    component="openrouter",
)

OPENROUTER_SITE_URL = config_registry.get_var(
    "OPENROUTER_SITE_URL",
    "",
    label="Site URL",
    description="HTTP-Referer header for OpenRouter.",
    group="llm",
    component="openrouter",
    advanced=True,
)

OPENROUTER_APP_NAME = config_registry.get_var(
    "OPENROUTER_APP_NAME",
    "Synthetic Heart",
    label="App Name",
    description="X-Title header for OpenRouter.",
    group="llm",
    component="openrouter",
    advanced=True,
)

OPENROUTER_MODEL_ROUTES = config_registry.get_var(
    "OPENROUTER_MODEL_ROUTES",
    "{}",
    label="Model Routes",
    description="Per-scope and per-action model overrides (JSON).",
    group="llm",
    component="openrouter",
    value_type="json",
    advanced=True,
)

OPENROUTER_CATALOG_REFRESH_MINUTES = config_registry.get_var(
    "OPENROUTER_CATALOG_REFRESH_MINUTES",
    30,
    label="Catalog Refresh (min)",
    description="Model catalog refresh interval in minutes.",
    group="llm",
    component="openrouter",
    value_type=int,
    advanced=True,
)


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------
@dataclass
class OpenRouterModel:
    """Parsed model entry from the OpenRouter /models endpoint."""

    id: str
    name: str
    context_length: int = 4096
    max_completion_tokens: int = 4096
    modality: str = "text->text"
    supports_vision: bool = False
    supports_audio: bool = False
    supports_tool_use: bool = False
    pricing_prompt: float = 0.0
    pricing_completion: float = 0.0

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> OpenRouterModel:
        """Parse a single model entry from the OpenRouter /models response."""
        model_id = data.get("id", "unknown")
        name = data.get("name", model_id)

        arch = data.get("architecture", {}) or {}
        modality = arch.get("modality", "text->text") or "text->text"

        # Parse modality string: "text+image->text", "text+image+audio->text", etc.
        input_side = modality.split("->")[0] if "->" in modality else modality
        supports_vision = "image" in input_side
        supports_audio = "audio" in input_side

        top = data.get("top_provider", {}) or {}
        ctx = data.get("context_length", 4096) or 4096
        max_completion = top.get("max_completion_tokens") or 4096

        pricing = data.get("pricing", {}) or {}
        try:
            p_prompt = float(pricing.get("prompt", "0") or "0")
        except (ValueError, TypeError):
            p_prompt = 0.0
        try:
            p_completion = float(pricing.get("completion", "0") or "0")
        except (ValueError, TypeError):
            p_completion = 0.0

        # Tool use: check supported_parameters or known model families
        supported_params = data.get("supported_parameters", []) or []
        supports_tools = (
            "tools" in supported_params or "tool_choice" in supported_params
        )

        return cls(
            id=model_id,
            name=name,
            context_length=ctx,
            max_completion_tokens=max_completion,
            modality=modality,
            supports_vision=supports_vision,
            supports_audio=supports_audio,
            supports_tool_use=supports_tools,
            pricing_prompt=p_prompt,
            pricing_completion=p_completion,
        )


@dataclass
class _ModelCatalog:
    """In-memory cache of the OpenRouter model catalog."""

    models: dict[str, OpenRouterModel] = field(default_factory=dict)
    last_fetched: float = 0.0
    _refresh_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def get(self, model_id: str) -> OpenRouterModel | None:
        return self.models.get(model_id)

    def list_ids(self) -> list[str]:
        return sorted(self.models.keys())

    def is_stale(self, max_age_minutes: int = 30) -> bool:
        if not self.models:
            return True
        return (time.monotonic() - self.last_fetched) > max_age_minutes * 60

    def update(self, models: dict[str, OpenRouterModel]) -> None:
        self.models = models
        self.last_fetched = time.monotonic()
        log_info(f"[openrouter] Model catalog updated: {len(models)} models")
        # Push model IDs into the exposed variable options for the WebUI
        try:
            from core.variables_engine import exposed_vars

            defn = exposed_vars.get_definition("OPENROUTER_DEFAULT_MODEL")
            if defn is not None:
                defn.options = sorted(models.keys())
        except Exception:
            pass


# Module-level catalog singleton
_catalog = _ModelCatalog()


def _fetch_catalog_sync(base_url: str) -> dict[str, OpenRouterModel]:
    """Fetch the model catalog from OpenRouter (blocking)."""
    url = f"{base_url.rstrip('/')}/models"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log_warning(f"[openrouter] Failed to fetch model catalog: {exc}")
        return {}

    models: dict[str, OpenRouterModel] = {}
    for entry in data.get("data", []):
        try:
            m = OpenRouterModel.from_api(entry)
            models[m.id] = m
        except Exception as exc:
            log_debug(f"[openrouter] Skipping model entry: {exc}")
    return models


async def _refresh_catalog(base_url: str) -> None:
    """Refresh the catalog in a background thread."""
    loop = asyncio.get_event_loop()
    models = await loop.run_in_executor(None, _fetch_catalog_sync, base_url)
    if models:
        _catalog.update(models)


async def _catalog_refresh_loop(base_url: str, interval_minutes: int) -> None:
    """Periodically refresh the model catalog."""
    while True:
        try:
            await asyncio.sleep(interval_minutes * 60)
            await _refresh_catalog(base_url)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log_warning(f"[openrouter] Catalog refresh failed: {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Supported image MIME types for OpenAI vision format
_VISION_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _parse_routes(raw: Any) -> dict[str, Any]:
    """Safely parse model routes from config (may be str or dict)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class OpenRouterPlugin(AIPluginBase):
    """OpenRouter LLM Engine using the OpenAI-compatible REST API."""

    display_name = "OpenRouter"

    def __init__(self, notify_fn: Any = None) -> None:
        from core.notifier import set_notifier

        if notify_fn:
            set_notifier(notify_fn)
            self._notify_fn = notify_fn
        else:
            self._notify_fn = lambda chat_id, message: log_info(
                f"[NOTIFY fallback] {message}"
            )
            set_notifier(self._notify_fn)

        self._current_model: str = str(OPENROUTER_DEFAULT_MODEL) or "x-ai/grok-4.1-fast"
        self._current_request_meta: dict[str, Any] | None = None

        # Model limits map for plugin_instance.py compatibility
        self.model_limits_map: dict[str, int] = {"default": 200000}

        # Kick off initial catalog fetch
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                base = (
                    str(OPENROUTER_BASE_URL).strip() or "https://openrouter.ai/api/v1"
                )
                asyncio.ensure_future(self._start_catalog(base))
            else:
                base = (
                    str(OPENROUTER_BASE_URL).strip() or "https://openrouter.ai/api/v1"
                )
                loop.run_until_complete(_refresh_catalog(base))
        except Exception as exc:
            log_warning(f"[openrouter] Catalog fetch on init failed: {exc}")

        log_info(f"[openrouter] Initialized with default model: {self._current_model}")

    async def _start_catalog(self, base_url: str) -> None:
        """Start the catalog refresh and schedule periodic updates."""
        await _refresh_catalog(base_url)
        interval = (
            int(OPENROUTER_CATALOG_REFRESH_MINUTES)
            if OPENROUTER_CATALOG_REFRESH_MINUTES
            else 30
        )
        if _catalog._refresh_task is None or _catalog._refresh_task.done():
            _catalog._refresh_task = asyncio.create_task(
                _catalog_refresh_loop(base_url, interval)
            )

    # ------------------------------------------------------------------
    # AIPluginBase interface
    # ------------------------------------------------------------------

    def get_health_status(self) -> tuple[bool, str]:
        """Return (ok, error_message) indicating whether the engine is ready."""
        if not OPENROUTER_API_KEY or not str(OPENROUTER_API_KEY).strip():
            return False, "OPENROUTER_API_KEY not configured"
        return True, ""

    def get_supported_models(self) -> list[str]:
        """Return available model IDs from catalog, falling back to current."""
        ids = _catalog.list_ids()
        if ids:
            return ids
        return [self._current_model]

    def get_current_model(self) -> str:
        return self._current_model

    def set_current_model(self, name: str) -> None:
        self._current_model = name
        # Update limits from catalog
        model = _catalog.get(name)
        if model:
            self.model_limits_map["default"] = (
                model.context_length * 3
            )  # chars ≈ 3× tokens
        log_info(f"[openrouter] Active model updated: {name}")

    def get_rate_limit(self) -> tuple[int, int, float]:
        return (60, 60, 0.5)

    def get_interface_limits(self) -> dict[str, Any]:
        model = _catalog.get(self._current_model)
        ctx = model.context_length if model else 128000
        max_out = model.max_completion_tokens if model else 4096
        has_vision = model.supports_vision if model else False
        return {
            "max_prompt_chars": ctx * 3,
            "max_response_chars": max_out,
            "supports_images": has_vision,
            "supports_functions": True,
            "supports_voice_interaction": False,
            "model_name": self._current_model,
        }

    # --- Agent hooks ---
    def supports_agent(self) -> bool:
        return True

    def agent_execute(self, action_dict: dict, context: dict | None = None) -> dict:
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            agent_plugin = (
                PLUGIN_REGISTRY.get("agent")
                if isinstance(PLUGIN_REGISTRY, dict)
                else None
            )
            if agent_plugin and hasattr(agent_plugin, "execute_action"):
                res = agent_plugin.execute_action(
                    action_dict, context or {}, None, None
                )
                if hasattr(res, "__await__"):
                    return {
                        "status": "pending_async",
                        "note": "Agent plugin returned coroutine",
                    }
                return res or {"status": "ok"}
        except Exception as exc:
            log_warning(f"[openrouter] agent_execute adapter failed: {exc}")
        return {"status": "unsupported", "reason": "agent plugin not available"}

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def _resolve_model(
        self, scope: str | None = None, action_type: str | None = None
    ) -> str:
        """Resolve which model to use based on routes config.

        Priority: per-action override > per-scope override > default model.
        """
        routes = _parse_routes(
            OPENROUTER_MODEL_ROUTES.value
            if hasattr(OPENROUTER_MODEL_ROUTES, "value")
            else str(OPENROUTER_MODEL_ROUTES)
        )

        # 1. Per-action override (supports glob patterns like "message_*")
        if action_type and "actions" in routes:
            action_routes = routes["actions"]
            if isinstance(action_routes, dict):
                # Exact match first
                if action_type in action_routes:
                    return action_routes[action_type]
                # Glob match
                for pattern, model_id in action_routes.items():
                    if fnmatch.fnmatch(action_type, pattern):
                        return model_id

        # 2. Per-scope override
        if scope and "scopes" in routes:
            scope_routes = routes["scopes"]
            if isinstance(scope_routes, dict) and scope in scope_routes:
                return scope_routes[scope]

        # 3. Default
        return self._current_model

    # ------------------------------------------------------------------
    # Main entry points
    # ------------------------------------------------------------------

    async def handle_incoming_message(self, bot: Any, message: Any, prompt: Any) -> str:
        """Process a message using a pre-built prompt.

        Returns the LLM response text. The message_chain handles JSON parsing,
        action execution, diary entries, emotion extraction, and DB persistence.
        """
        from core.notifier import notify_trainer

        try:
            self._current_request_meta = {
                "bot": bot,
                "message": message,
                "interface": getattr(message, "interface", None)
                or getattr(message, "interface_path", None),
                "chat_id": getattr(message, "chat_id", None),
                "interface_path": getattr(message, "interface_path", None),
            }

            log_debug(
                f"[openrouter] Processing message from chat_id="
                f"{getattr(message, 'chat_id', 'unknown')}"
            )

            response = await self.generate_response(prompt)

            if response:
                preview = response[:200] + "..." if len(response) > 200 else response
                log_info(f"[openrouter] Generated response: {preview}")

            return response

        except Exception as exc:
            log_error(f"[openrouter] Error in handle_incoming_message: {exc!r}")
            notify_trainer(f"OpenRouter error:\n{exc}")
            return f"Error during response generation: {exc}"
        finally:
            self._current_request_meta = None

    async def generate_response(self, prompt):  # type: ignore[override]
        """Send prompt to OpenRouter and return the response text."""
        if not OPENROUTER_API_KEY:
            log_warning("[openrouter] OPENROUTER_API_KEY not configured")
            return "OpenRouter API Key not configured. Please set OPENROUTER_API_KEY in settings or .env"

        try:
            # Handle correction prompts
            if isinstance(prompt, dict) and "system_message" in prompt:
                sm = prompt.get("system_message", {})
                sm_type = sm.get("type", "") if isinstance(sm, dict) else ""
                if sm_type in (
                    "error",
                    "correction",
                    "invalid_json",
                    "validation_error",
                ):
                    return await self._handle_correction_prompt(prompt)
                log_debug(
                    f"[openrouter] Processing system_message type '{sm_type}' as normal prompt"
                )
            elif isinstance(prompt, str):
                try:
                    parsed = json.loads(prompt)
                    if isinstance(parsed, dict) and "system_message" in parsed:
                        sm = parsed.get("system_message", {})
                        sm_type = sm.get("type", "") if isinstance(sm, dict) else ""
                        if sm_type in (
                            "error",
                            "correction",
                            "invalid_json",
                            "validation_error",
                        ):
                            return await self._handle_correction_prompt(parsed)
                except (json.JSONDecodeError, ValueError):
                    pass

            # Normalize prompt to text
            if isinstance(prompt, dict):
                prompt_text = json.dumps(prompt, indent=2, ensure_ascii=False)
            elif isinstance(prompt, str):
                prompt_text = prompt
            else:
                prompt_text = str(prompt)

            # Extract and redact multimodal parts
            multimodal_parts = self._extract_multimodal_parts(prompt)

            if isinstance(prompt, dict):
                redacted = self._copy_and_redact_data(prompt)
                prompt_text = json.dumps(redacted, indent=2, ensure_ascii=False)

            log_debug(
                f"[openrouter] Sending prompt ({len(prompt_text)} chars) to {self._current_model}"
            )

            system_instruction = self._build_system_instruction(prompt)

            # Determine scope from prompt context
            scope = None
            if isinstance(prompt, dict):
                scope = prompt.get("scope") or prompt.get("action_scope")

            model = self._resolve_model(scope=scope)
            model_info = _catalog.get(model)
            max_tokens = model_info.max_completion_tokens if model_info else 4096

            response_text = await self._openai_chat_completion(
                prompt_text=prompt_text,
                system_instruction=system_instruction,
                max_tokens=max_tokens,
                model=model,
                multimodal_parts=multimodal_parts if multimodal_parts else None,
            )

            log_debug(f"[openrouter] Received response ({len(response_text)} chars)")
            return response_text

        except Exception as exc:
            log_error(f"[openrouter] Generation failed: {exc}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {"text": f"OpenRouter API error: {exc}"},
                        }
                    ]
                }
            )

    # ------------------------------------------------------------------
    # OpenAI-compatible chat completion
    # ------------------------------------------------------------------

    async def _openai_chat_completion(
        self,
        prompt_text: str,
        system_instruction: str,
        max_tokens: int,
        model: str | None = None,
        multimodal_parts: list[dict[str, Any]] | None = None,
    ) -> str:
        """Send a chat completion request to OpenRouter."""
        base_url = str(OPENROUTER_BASE_URL).strip() or "https://openrouter.ai/api/v1"
        api_key = str(OPENROUTER_API_KEY).strip()
        url = f"{base_url.rstrip('/')}/chat/completions"

        resolved_model = model or self._current_model

        # Build messages — use multipart format with cache_control on the
        # system message so providers that support explicit prompt caching
        # (Anthropic, Gemini) can cache the large static system instruction.
        # Grok/xAI caching is automatic and ignores this harmlessly.
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": system_instruction,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ]

        # User message — with optional vision content
        if multimodal_parts:
            # Check if model supports vision
            model_info = _catalog.get(resolved_model)
            has_vision = model_info.supports_vision if model_info else False

            content_parts: list[dict[str, Any]] = []
            if has_vision:
                for part in multimodal_parts:
                    content_parts.append(part)
            elif multimodal_parts:
                log_warning(
                    f"[openrouter] Model {resolved_model} does not support vision; "
                    f"skipping {len(multimodal_parts)} image part(s)"
                )
            content_parts.append({"type": "text", "text": prompt_text})
            messages.append({"role": "user", "content": content_parts})
        else:
            messages.append({"role": "user", "content": prompt_text})

        # Build headers
        headers: dict[str, str] = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        site_url = str(OPENROUTER_SITE_URL).strip()
        if site_url:
            headers["HTTP-Referer"] = site_url
        app_name = str(OPENROUTER_APP_NAME).strip()
        if app_name:
            headers["X-Title"] = app_name

        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "max_tokens": max_tokens,
            # Disable safety filters for providers that support it (Gemini).
            # Grok has no adjustable safety — it's already minimally filtered.
            "safety_settings": [
                {
                    "category": "HARM_CATEGORY_HARASSMENT",
                    "threshold": "BLOCK_NONE",
                },
                {
                    "category": "HARM_CATEGORY_HATE_SPEECH",
                    "threshold": "BLOCK_NONE",
                },
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_NONE",
                },
                {
                    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "threshold": "BLOCK_NONE",
                },
            ],
        }

        def _do_request() -> requests.Response:
            return requests.post(url, headers=headers, json=payload, timeout=120)

        retryable_statuses = {429, 500, 503, 504}
        max_attempts = 3
        response: requests.Response | None = None  # narrowed after loop

        for attempt in range(max_attempts):
            try:
                loop = asyncio.get_event_loop()
                resp: requests.Response = await loop.run_in_executor(None, _do_request)
                response = resp
            except Exception as exc:
                if attempt < max_attempts - 1:
                    delay = min(8, 1 * (2**attempt))
                    log_warning(
                        f"[openrouter] HTTP request failed (attempt {attempt + 1}/{max_attempts}): "
                        f"{exc}. Retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                log_error(f"[openrouter] HTTP request failed: {exc}")
                return json.dumps(
                    {
                        "actions": [
                            {
                                "type": "system_message",
                                "payload": {
                                    "text": f"OpenRouter HTTP request failed: {exc}"
                                },
                            }
                        ]
                    }
                )

            status = int(resp.status_code)  # type: ignore[arg-type]
            if status >= 400:
                if status in retryable_statuses and attempt < max_attempts - 1:
                    delay = min(8, 1 * (2**attempt))
                    log_warning(
                        f"[openrouter] HTTP error {status} "
                        f"(attempt {attempt + 1}/{max_attempts}). Retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                log_error(f"[openrouter] HTTP error {status}: {resp.text}")
                return json.dumps(
                    {
                        "actions": [
                            {
                                "type": "system_message",
                                "payload": {
                                    "text": f"OpenRouter HTTP error {status}: {resp.text}"
                                },
                            }
                        ]
                    }
                )
            break

        if response is None:
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {
                                "text": "OpenRouter HTTP request failed: no response"
                            },
                        }
                    ]
                }
            )

        try:
            data = response.json()
        except Exception as exc:
            log_error(f"[openrouter] Response JSON parse failed: {exc}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {
                                "text": "OpenRouter response was not valid JSON"
                            },
                        }
                    ]
                }
            )

        # Handle OpenRouter error responses
        if "error" in data:
            error_msg = data["error"]
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            log_error(f"[openrouter] API error: {error_msg}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {"text": f"OpenRouter API error: {error_msg}"},
                        }
                    ]
                }
            )

        choices = data.get("choices") or []
        if not choices:
            log_error(f"[openrouter] Response missing choices: {data}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {"text": "OpenRouter response missing choices"},
                        }
                    ]
                }
            )

        content = choices[0].get("message", {}).get("content", "")
        if not content:
            log_error(f"[openrouter] Response contained no content: {data}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {
                                "text": "OpenRouter response contained no content"
                            },
                        }
                    ]
                }
            )

        # Log usage if available
        usage = data.get("usage")
        if usage:
            log_debug(
                f"[openrouter] Usage: prompt_tokens={usage.get('prompt_tokens')}, "
                f"completion_tokens={usage.get('completion_tokens')}, "
                f"model={data.get('model', resolved_model)}"
            )

        return content.strip()

    # ------------------------------------------------------------------
    # System instruction
    # ------------------------------------------------------------------

    def _build_system_instruction(self, prompt: Any) -> str:
        """Build the system instruction based on prompt context."""
        interface = "unknown"
        verbose_instructions = None
        prompt_dict: dict[str, Any] | None = None

        if isinstance(prompt, dict):
            prompt_dict = prompt
        elif isinstance(prompt, str):
            try:
                parsed = json.loads(prompt)
                if isinstance(parsed, dict):
                    prompt_dict = parsed
            except (json.JSONDecodeError, ValueError):
                pass

        if isinstance(prompt_dict, dict):
            interface = prompt_dict.get("interface") or prompt_dict.get(
                "current_interface"
            )
            verbose_instructions = prompt_dict.get("instructions_verbose")

            if not interface or interface == "unknown":
                input_section = prompt_dict.get("input", {})
                if isinstance(input_section, dict):
                    source = input_section.get("source", {})
                    if isinstance(source, dict):
                        interface = source.get("interface") or interface
            if not interface or interface == "unknown":
                input_section = prompt_dict.get("input", {})
                if isinstance(input_section, dict):
                    interface = input_section.get("interface") or interface

        if not interface:
            interface = "unknown"

        interface_to_action = {
            "synth_webui": "message_synth_webui",
            "telegram_bot": "message_telegram_bot",
            "discord_bot": "message_discord_bot",
            "ollama_serve": "message_ollama_serve",
        }
        message_action = interface_to_action.get(interface, f"message_{interface}")

        system_instruction = (
            "You are part of the 'Synthetic Heart' AI system.\n"
            "\n"
            "CRITICAL OUTPUT FORMAT:\n"
            "1. Respond with ONLY valid JSON - nothing before or after\n"
            "2. Your response MUST start with { and end with }\n"
            '3. Use this structure: {"actions": [{"type": "action_name", "payload": {...}}]}\n'
            "4. NO markdown code blocks, NO explanations outside JSON\n"
            "\n"
            f"CURRENT INTERFACE: {interface}\n"
            f"TO SEND A MESSAGE TO THE USER: Use action type '{message_action}'\n"
            "\n"
            "The prompt contains a complete action schema with available actions.\n"
            "Follow those instructions precisely.\n"
            "\n"
            "Remember: Output ONLY valid JSON. The system will parse your JSON and execute the actions."
        )

        if verbose_instructions:
            system_instruction = f"{verbose_instructions}\n\n{system_instruction}"

        return system_instruction

    # ------------------------------------------------------------------
    # Correction handling
    # ------------------------------------------------------------------

    async def _handle_correction_prompt(self, prompt: dict[str, Any]) -> str:
        """Handle a correction/system_message prompt."""
        system_message = prompt.get("system_message", {})
        error_type = system_message.get("type", "error")
        error_message = system_message.get("message", "Unknown error")
        original_user_message = system_message.get("original_user_message", "")
        your_reply = system_message.get("your_reply", "")
        required_format = system_message.get("required_format", {})

        interface = (
            system_message.get("target_interface")
            or system_message.get("interface")
            or prompt.get("interface")
            or None
        )

        if not interface:
            action_hint = system_message.get("action_type_hint", "")
            if "message_telegram_bot" in action_hint:
                interface = "telegram_bot"
            elif "message_discord_bot" in action_hint:
                interface = "discord_bot"
            elif "message_synth_webui" in action_hint:
                interface = "synth_webui"
            else:
                interface = "synth_webui"

        # Grillo beats are internal
        if interface == "grillo":
            log_debug(
                "[openrouter] Grillo beat detected — internal, no interface routing"
            )
            correction_prompt = (
                f"CORRECTION REQUIRED - INTERNAL BEAT\n\n"
                f"Error: {error_message}\n\n"
                f"This is an internal Grillo beat. You should NOT output any message action.\n"
                f"Just output valid JSON with internal actions like 'create_personal_diary_entry'.\n\n"
                f"Your previous (invalid) reply:\n"
                f"{your_reply[:500] if your_reply else '(none)'}...\n\n"
                f"Respond with ONLY valid JSON containing internal actions.\n"
                f"Do NOT include any message_* actions."
            )
            return await self._openai_chat_completion(
                prompt_text=correction_prompt,
                system_instruction=(
                    "You are a JSON correction assistant for internal Grillo beats. "
                    "Output ONLY valid JSON with internal actions like 'create_personal_diary_entry'. "
                    "Do NOT output any message_* actions."
                ),
                max_tokens=4096,
            )

        interface_to_action = {
            "synth_webui": "message_synth_webui",
            "telegram_bot": "message_telegram_bot",
            "discord_bot": "message_discord_bot",
            "ollama_serve": "message_ollama_serve",
        }
        message_action = interface_to_action.get(interface, f"message_{interface}")

        correction_prompt = f"CORRECTION REQUIRED\n\nError: {error_message}\n\n"
        if original_user_message:
            correction_prompt += f'Original user message you should respond to:\n"{original_user_message}"\n\n'
        if your_reply:
            correction_prompt += (
                f"Your previous (invalid) reply:\n{your_reply[:500]}...\n\n"
            )
        correction_prompt += (
            f"REQUIREMENTS:\n"
            f"1. Respond with ONLY valid JSON\n"
            f"2. Follow this exact structure:\n"
            f"{json.dumps(required_format, indent=2)}\n"
            f"\n"
            f"IMPORTANT: To send a message to the user, use action type '{message_action}'\n"
            f"\n"
            f"Respond NOW with valid JSON only."
        )

        log_warning(f"[openrouter] Handling correction prompt: {error_type}")
        return await self._openai_chat_completion(
            prompt_text=correction_prompt,
            system_instruction=(
                "You are a JSON correction assistant. "
                "Your ONLY task is to output valid JSON following the exact structure shown. "
                f"CURRENT INTERFACE: {interface}. "
                f"TO SEND A MESSAGE TO THE USER: Use action type '{message_action}'. "
                "NO explanations. NO markdown. ONLY valid JSON starting with {{ and ending with }}."
            ),
            max_tokens=8192,
        )

    # ------------------------------------------------------------------
    # Multimodal support
    # ------------------------------------------------------------------

    def _extract_multimodal_parts(self, prompt: Any) -> list[dict[str, Any]]:
        """Extract image parts from prompt for OpenAI vision format.

        Returns a list of OpenAI content parts:
        [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]
        """
        parts: list[dict[str, Any]] = []

        if isinstance(prompt, str):
            try:
                prompt = json.loads(prompt)
            except (json.JSONDecodeError, ValueError):
                return parts

        if not isinstance(prompt, dict):
            return parts

        multimodal_keys = {"attachments", "images", "audio", "documents", "videos"}
        attachments: list[dict[str, Any]] = []

        def collect_recursive(container: Any) -> None:
            if isinstance(container, dict):
                for key in multimodal_keys:
                    if key in container:
                        items = container[key]
                        if isinstance(items, list):
                            for item in items:
                                if isinstance(item, dict):
                                    attachments.append(item)
                                elif isinstance(item, str):
                                    default_mime = {
                                        "images": "image/jpeg",
                                        "audio": "audio/mpeg",
                                        "videos": "video/mp4",
                                        "documents": "application/pdf",
                                    }.get(key, "application/octet-stream")
                                    attachments.append(
                                        {"path": item, "mime_type": default_mime}
                                    )
                        elif isinstance(items, dict):
                            attachments.append(items)
                for value in container.values():
                    collect_recursive(value)
            elif isinstance(container, list):
                for item in container:
                    collect_recursive(item)

        collect_recursive(prompt)

        for att in attachments:
            if not isinstance(att, dict):
                continue

            mime_type = att.get("mime_type") or att.get("mimeType", "")
            file_path = att.get("path") or att.get("file_path", "")

            if file_path and not mime_type:
                import mimetypes as mt

                mime_type, _ = mt.guess_type(str(file_path))
                mime_type = mime_type or "application/octet-stream"

            # Only include images (OpenAI vision format)
            if mime_type not in _VISION_MIME_TYPES:
                if mime_type and not mime_type.startswith("application/octet"):
                    log_debug(
                        f"[openrouter] Skipping non-image attachment: {mime_type}"
                    )
                continue

            b64_data = att.get("data") or att.get("base64", "")
            if not b64_data and file_path:
                try:
                    p = Path(file_path)
                    if p.exists():
                        b64_data = base64.b64encode(p.read_bytes()).decode("utf-8")
                except Exception as exc:
                    log_warning(f"[openrouter] Failed to read file {file_path}: {exc}")
                    continue

            if not b64_data:
                continue

            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{b64_data}"},
                }
            )
            log_debug(f"[openrouter] Added image part: {mime_type}")

        return parts

    def _copy_and_redact_data(self, prompt: dict[str, Any]) -> dict[str, Any]:
        """Deep copy prompt and redact heavy base64 data."""
        try:
            redacted = copy.deepcopy(prompt)
            multimodal_keys = {"attachments", "images", "audio", "documents", "videos"}
            data_fields = {"data", "base64"}
            attachment_fields = {
                "mime_type",
                "mimeType",
                "path",
                "file_path",
                "data",
                "base64",
            }

            def redact_item(item: dict[str, Any]) -> None:
                if not isinstance(item, dict) or not (item.keys() & attachment_fields):
                    return
                for fld in data_fields:
                    if fld in item:
                        item[fld] = f"<redacted: {len(str(item[fld]))} chars>"

            def redact_recursive(container: Any) -> None:
                if isinstance(container, dict):
                    for key in multimodal_keys:
                        if key in container:
                            items = container[key]
                            if isinstance(items, list):
                                for item in items:
                                    redact_item(item)
                            elif isinstance(items, dict):
                                redact_item(items)
                    for value in container.values():
                        redact_recursive(value)
                elif isinstance(container, list):
                    for item in container:
                        redact_recursive(item)

            redact_recursive(redacted)
            return redacted
        except Exception as exc:
            log_warning(f"[openrouter] Failed to redact prompt data: {exc}")
            return prompt

    # ------------------------------------------------------------------
    # Catalog access (for external consumers)
    # ------------------------------------------------------------------

    def get_model_capabilities(self, model_id: str | None = None) -> dict[str, Any]:
        """Return capability info for a model from the catalog."""
        mid = model_id or self._current_model
        model = _catalog.get(mid)
        if not model:
            return {"model": mid, "available": False}
        return {
            "model": model.id,
            "name": model.name,
            "available": True,
            "context_length": model.context_length,
            "max_completion_tokens": model.max_completion_tokens,
            "modality": model.modality,
            "supports_vision": model.supports_vision,
            "supports_audio": model.supports_audio,
            "supports_tool_use": model.supports_tool_use,
            "pricing_prompt": model.pricing_prompt,
            "pricing_completion": model.pricing_completion,
        }

    def get_catalog_summary(self) -> dict[str, Any]:
        """Return a summary of the cached model catalog."""
        return {
            "total_models": len(_catalog.models),
            "last_fetched_ago_s": round(time.monotonic() - _catalog.last_fetched, 1)
            if _catalog.last_fetched
            else None,
            "vision_models": sum(
                1 for m in _catalog.models.values() if m.supports_vision
            ),
            "audio_models": sum(
                1 for m in _catalog.models.values() if m.supports_audio
            ),
            "tool_models": sum(
                1 for m in _catalog.models.values() if m.supports_tool_use
            ),
        }


PLUGIN_CLASS = OpenRouterPlugin
