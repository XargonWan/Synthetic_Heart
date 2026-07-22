# cortex/external_engines/openapi.py
"""Generic OpenAPI LLM Engine for Synthetic Heart.

This engine connects to ANY OpenAI-compatible endpoint (Ollama, LM Studio,
vLLM, custom endpoints) to use external LLM services or custom skills.

Key features:
- Configurable base URL and optional API key authentication
- Auto-discovery from /v1/models endpoint OR manual model catalog
- Multimodal support (images, audio) with toggle flags
- Custom headers for special endpoint requirements
- Simplified implementation (no provider-specific features)

Use cases:
- Connect to local LLMs (Ollama, LM Studio)
- Use remote vLLM deployments
- Access custom tool-calling endpoints
- Integrate external AI services
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from core.ai_plugin_base import AIPluginBase
from core.beat_utils import is_outbound_beat
from core.config_manager import config_registry
from core.cortex_api_logger import log_cortex_request, log_cortex_response
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.prompt_request import PromptRequest

ENGINE_LABEL = "Generic OpenAPI — connect to any OpenAI-compatible endpoint"
_LEGACY_DICT_PROMPT_WARNED = False

# ---------------------------------------------------------------------------
# WebUI variable registration (always visible so keys can be set before use)
# ---------------------------------------------------------------------------
try:
    from core.variables_engine import register_exposed_var

    # Base URL, API key and default model live in the Engines tab
    # (external_endpoints), not in Settings.

    # Advanced settings
    register_exposed_var(
        "OPENAPI_TIMEOUT",
        label="Request Timeout (seconds)",
        default="120",
        value_type=int,
        ui_type="number",
        description="HTTP request timeout in seconds.",
        scope="llm",
        component="openapi",
        tags=["cortex_engine"],
        advanced=True,
    )
    register_exposed_var(
        "OPENAPI_MAX_RETRIES",
        label="Max Retries",
        default="3",
        value_type=int,
        ui_type="number",
        description="Number of retry attempts for transient errors.",
        scope="llm",
        component="openapi",
        tags=["cortex_engine"],
        advanced=True,
    )
    register_exposed_var(
        "OPENAPI_CUSTOM_HEADERS",
        label="Custom Headers",
        default="{}",
        value_type="json",
        ui_type="json",
        description='Additional HTTP headers as JSON object (e.g., {"X-Organization": "myorg"}).',
        scope="llm",
        component="openapi",
        tags=["cortex_engine"],
        advanced=True,
    )

    # Model management
    register_exposed_var(
        "OPENAPI_MODEL_CATALOG",
        label="Manual Model Catalog",
        default="[]",
        value_type="json",
        ui_type="json",
        description='Manual model list as JSON array (e.g., [{"id": "model-1", "name": "Model 1", "context_length": 32000}]).',
        scope="llm",
        component="openapi",
        tags=["cortex_engine"],
        advanced=True,
    )
    register_exposed_var(
        "OPENAPI_AUTO_DISCOVER",
        label="Auto-Discover Models",
        default="true",
        value_type=bool,
        ui_type="bool",
        description="Automatically fetch models from /v1/models endpoint.",
        scope="llm",
        component="openapi",
        tags=["cortex_engine"],
        advanced=True,
    )
    register_exposed_var(
        "OPENAPI_CATALOG_REFRESH",
        label="Catalog Refresh (minutes)",
        default="60",
        value_type=int,
        ui_type="number",
        description="Minutes between model catalog refreshes.",
        scope="llm",
        component="openapi",
        tags=["cortex_engine"],
        advanced=True,
    )

    # Capabilities
    register_exposed_var(
        "OPENAPI_SUPPORTS_VISION",
        label="Enable Vision Support",
        default="false",
        value_type=bool,
        ui_type="bool",
        description="Enable image/vision inputs (endpoint must support it).",
        scope="llm",
        component="openapi",
        tags=["cortex_engine"],
        advanced=True,
    )
    register_exposed_var(
        "OPENAPI_SUPPORTS_AUDIO",
        label="Enable Audio Support",
        default="false",
        value_type=bool,
        ui_type="bool",
        description="Enable audio inputs (endpoint must support it).",
        scope="llm",
        component="openapi",
        tags=["cortex_engine"],
        advanced=True,
    )
    register_exposed_var(
        "OPENAPI_SUPPORTS_TOOLS",
        label="Enable Tool/Function Calling",
        default="false",
        value_type=bool,
        ui_type="bool",
        description="Enable tool/function calling (endpoint must support it).",
        scope="llm",
        component="openapi",
        tags=["cortex_engine"],
        advanced=True,
    )

    # Context limits (fallback)
    register_exposed_var(
        "OPENAPI_DEFAULT_CONTEXT",
        label="Default Context Window (chars)",
        default="128000",
        value_type=int,
        ui_type="number",
        description="Default context window in characters (used if endpoint doesn't provide it).",
        scope="llm",
        component="openapi",
        tags=["cortex_engine"],
        advanced=True,
    )
    register_exposed_var(
        "OPENAPI_DEFAULT_MAX_TOKENS",
        label="Default Max Output Tokens",
        default="4096",
        value_type=int,
        ui_type="number",
        description="Default max output tokens (used if endpoint doesn't provide it).",
        scope="llm",
        component="openapi",
        tags=["cortex_engine"],
        advanced=True,
    )
except Exception:
    pass

# ---------------------------------------------------------------------------
# Config variables (auto-updating)
# ---------------------------------------------------------------------------
OPENAPI_BASE_URL = config_registry.get_var(
    "OPENAPI_BASE_URL",
    "http://localhost:8081/v1",
    label="OpenAPI Base URL",
    description="Base URL for the OpenAI-compatible endpoint.",
    group="llm",
    component="openapi",
    hidden=True,
)

OPENAPI_API_KEY = config_registry.get_var(
    "OPENAPI_API_KEY",
    "",
    label="API Key (Optional)",
    description="Bearer token for authentication.",
    group="llm",
    component="openapi",
    sensitive=True,
    hidden=True,
)

OPENAPI_DEFAULT_MODEL = config_registry.get_var(
    "OPENAPI_DEFAULT_MODEL",
    "llama3",
    label="Default Model",
    description="Model name to use.",
    group="llm",
    component="openapi",
    hidden=True,
)

OPENAPI_TIMEOUT = config_registry.get_var(
    "OPENAPI_TIMEOUT",
    120,
    label="Request Timeout",
    description="HTTP request timeout in seconds.",
    group="llm",
    component="openapi",
    value_type=int,
    advanced=True,
)

OPENAPI_MAX_RETRIES = config_registry.get_var(
    "OPENAPI_MAX_RETRIES",
    3,
    label="Max Retries",
    description="Number of retry attempts.",
    group="llm",
    component="openapi",
    value_type=int,
    advanced=True,
)

OPENAPI_CUSTOM_HEADERS = config_registry.get_var(
    "OPENAPI_CUSTOM_HEADERS",
    "{}",
    label="Custom Headers",
    description="Additional HTTP headers as JSON.",
    group="llm",
    component="openapi",
    value_type="json",
    advanced=True,
)

OPENAPI_MODEL_CATALOG = config_registry.get_var(
    "OPENAPI_MODEL_CATALOG",
    "[]",
    label="Manual Model Catalog",
    description="Manual model list as JSON array.",
    group="llm",
    component="openapi",
    value_type="json",
    advanced=True,
)

OPENAPI_AUTO_DISCOVER = config_registry.get_var(
    "OPENAPI_AUTO_DISCOVER",
    True,
    label="Auto-Discover Models",
    description="Automatically fetch models from /v1/models endpoint.",
    group="llm",
    component="openapi",
    value_type=bool,
    advanced=True,
)

OPENAPI_CATALOG_REFRESH = config_registry.get_var(
    "OPENAPI_CATALOG_REFRESH",
    60,
    label="Catalog Refresh (minutes)",
    description="Minutes between model catalog refreshes.",
    group="llm",
    component="openapi",
    value_type=int,
    advanced=True,
)

OPENAPI_SUPPORTS_VISION = config_registry.get_var(
    "OPENAPI_SUPPORTS_VISION",
    False,
    label="Enable Vision Support",
    description="Enable image/vision inputs.",
    group="llm",
    component="openapi",
    value_type=bool,
    advanced=True,
)

OPENAPI_SUPPORTS_AUDIO = config_registry.get_var(
    "OPENAPI_SUPPORTS_AUDIO",
    False,
    label="Enable Audio Support",
    description="Enable audio inputs.",
    group="llm",
    component="openapi",
    value_type=bool,
    advanced=True,
)

OPENAPI_SUPPORTS_TOOLS = config_registry.get_var(
    "OPENAPI_SUPPORTS_TOOLS",
    True,  # Enabled by default for native PromptRequest rendering
    label="Enable Tool/Function Calling",
    description="Enable tool/function calling.",
    group="llm",
    component="openapi",
    value_type=bool,
    advanced=True,
)

OPENAPI_DEFAULT_CONTEXT = config_registry.get_var(
    "OPENAPI_DEFAULT_CONTEXT",
    128000,
    label="Default Context Window",
    description="Default context window in characters.",
    group="llm",
    component="openapi",
    value_type=int,
    advanced=True,
)

OPENAPI_DEFAULT_MAX_TOKENS = config_registry.get_var(
    "OPENAPI_DEFAULT_MAX_TOKENS",
    4096,
    label="Default Max Output Tokens",
    description="Default max output tokens.",
    group="llm",
    component="openapi",
    value_type=int,
    advanced=True,
)


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------
@dataclass
class OpenAPIModel:
    """Simple model info structure."""

    id: str
    name: str
    context_length: int = 128000
    max_completion_tokens: int = 4096

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> OpenAPIModel:
        """Parse model entry from /v1/models response."""
        model_id = data.get("id", "unknown")
        name = data.get("name", model_id)

        # Try to extract context length from various possible fields
        ctx = 128000  # default
        if "context_length" in data:
            ctx = int(data["context_length"]) if data["context_length"] else ctx
        elif "max_model_len" in data:
            ctx = int(data["max_model_len"]) if data["max_model_len"] else ctx

        # Try to extract max tokens
        max_tokens = 4096  # default
        if "max_tokens" in data:
            max_tokens = int(data["max_tokens"]) if data["max_tokens"] else max_tokens
        elif "max_completion_tokens" in data:
            max_tokens = (
                int(data["max_completion_tokens"])
                if data["max_completion_tokens"]
                else max_tokens
            )

        return cls(
            id=model_id,
            name=name,
            context_length=ctx,
            max_completion_tokens=max_tokens,
        )

    @classmethod
    def from_manual(cls, data: dict[str, Any]) -> OpenAPIModel:
        """Parse model entry from manual catalog config."""
        return cls(
            id=data.get("id", "unknown"),
            name=data.get("name", data.get("id", "unknown")),
            context_length=int(data.get("context_length", 128000)),
            max_completion_tokens=int(data.get("max_completion_tokens", 4096)),
        )


@dataclass
class _ModelCatalog:
    """In-memory cache of model catalog."""

    models: dict[str, OpenAPIModel] = field(default_factory=dict)
    last_fetched: float = 0.0
    _refresh_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def get(self, model_id: str) -> OpenAPIModel | None:
        return self.models.get(model_id)

    def list_ids(self) -> list[str]:
        return sorted(self.models.keys())

    def is_stale(self, max_age_minutes: int = 60) -> bool:
        if not self.models:
            return True
        return (time.monotonic() - self.last_fetched) > max_age_minutes * 60

    def update(self, models: dict[str, OpenAPIModel]) -> None:
        self.models = models
        self.last_fetched = time.monotonic()
        log_info(f"[openapi] Model catalog updated: {len(models)} models")
        # Update exposed variable options
        try:
            from core.variables_engine import exposed_vars

            defn = exposed_vars.get_definition("OPENAPI_DEFAULT_MODEL")
            if defn is not None:
                defn.options = sorted(models.keys())
        except Exception:
            pass


# Module-level catalog singleton
_catalog = _ModelCatalog()


def _fetch_catalog_sync(base_url: str, api_key: str = "") -> dict[str, OpenAPIModel]:
    """Fetch model catalog from endpoint (blocking).

    Tries ``/v1/models`` first, then falls back to ``/models`` for servers
    that expose models at the legacy path.
    """
    base = base_url.rstrip("/")
    # Avoid double /v1/ when base_url already ends with /v1
    if base.endswith("/v1"):
        paths = ["/models"]
    else:
        paths = ["/v1/models", "/models"]

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_exc: Exception | None = None
    for path in paths:
        url = f"{base}{path}"
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            last_exc = None
            break
        except Exception as exc:
            log_warning(f"[openapi] Catalog fetch failed for {url}: {exc}")
            last_exc = exc
    else:
        if last_exc:
            log_warning(f"[openapi] Failed to fetch model catalog: {last_exc}")
        return {}

    models: dict[str, OpenAPIModel] = {}
    model_list = data.get("data", []) if isinstance(data, dict) else []
    for entry in model_list:
        try:
            m = OpenAPIModel.from_api(entry)
            models[m.id] = m
        except Exception as exc:
            log_debug(f"[openapi] Skipping model entry: {exc}")

    return models


def _parse_manual_catalog(raw: Any) -> dict[str, OpenAPIModel]:
    """Parse manual model catalog from config."""
    if not isinstance(raw, list):
        try:
            parsed = json.loads(str(raw)) if isinstance(raw, str) else raw
            if not isinstance(parsed, list):
                return {}
            raw = parsed
        except (json.JSONDecodeError, ValueError):
            return {}

    models: dict[str, OpenAPIModel] = {}
    for entry in raw:
        if isinstance(entry, dict):
            try:
                m = OpenAPIModel.from_manual(entry)
                models[m.id] = m
            except Exception as exc:
                log_debug(f"[openapi] Skipping manual model entry: {exc}")

    return models


async def _refresh_catalog(base_url: str, api_key: str = "") -> None:
    """Refresh catalog in background thread."""
    loop = asyncio.get_event_loop()
    models = await loop.run_in_executor(None, _fetch_catalog_sync, base_url, api_key)
    if models:
        _catalog.update(models)


async def _catalog_refresh_loop(
    base_url: str, api_key: str, interval_minutes: int
) -> None:
    """Periodically refresh model catalog."""
    while True:
        try:
            await asyncio.sleep(interval_minutes * 60)
            await _refresh_catalog(base_url, api_key)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log_warning(f"[openapi] Catalog refresh failed: {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Supported image MIME types for OpenAI vision format
_VISION_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# Supported audio MIME types — sent as input_audio content parts (OpenAI format)
_AUDIO_MIME_TYPES: dict[str, str] = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mp4": "mp4",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


def _parse_json(raw: Any) -> Any:
    """Safely parse JSON from config (may be str or dict/list)."""
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class OpenAPIPlugin(AIPluginBase):
    """Generic OpenAPI LLM Engine using OpenAI-compatible REST API."""

    display_name = "OpenAPI"
    supports_prompt_request = True

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

        self._current_model: str = str(OPENAPI_DEFAULT_MODEL) or "default"
        self._current_request_meta: dict[str, Any] | None = None

        # Model limits map for plugin_instance.py compatibility
        self.model_limits_map: dict[str, int] = {
            "default": int(OPENAPI_DEFAULT_CONTEXT)
        }

        # Validate config on startup
        base_url = str(OPENAPI_BASE_URL).strip()
        if not base_url:
            log_error("[openapi] OPENAPI_BASE_URL not configured")
        elif not base_url.startswith(("http://", "https://")):
            log_warning(f"[openapi] Unusual base URL format: {base_url}")

        if not self._current_model or self._current_model == "":
            log_warning("[openapi] OPENAPI_DEFAULT_MODEL not set, using 'default'")
            self._current_model = "default"

        # Kick off initial catalog fetch
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._start_catalog())
            else:
                base = str(OPENAPI_BASE_URL).strip()
                api_key = str(OPENAPI_API_KEY).strip()
                loop.run_until_complete(_refresh_catalog(base, api_key))
                self._auto_select_model()
        except Exception as exc:
            log_warning(f"[openapi] Catalog fetch on init failed: {exc}")

        log_info(f"[openapi] Initialized with model: {self._current_model}")

    async def _start_catalog(self) -> None:
        """Start catalog refresh and schedule periodic updates."""
        # Initial fetch
        if bool(OPENAPI_AUTO_DISCOVER):
            base_url = str(OPENAPI_BASE_URL).strip()
            api_key = str(OPENAPI_API_KEY).strip()
            await _refresh_catalog(base_url, api_key)
        else:
            # Use manual catalog
            manual_models = _parse_manual_catalog(OPENAPI_MODEL_CATALOG.value)
            if manual_models:
                _catalog.update(manual_models)

        # Auto-select model if current model isn't in the catalog
        self._auto_select_model()

        # Schedule periodic refresh
        if bool(OPENAPI_AUTO_DISCOVER):
            interval = int(OPENAPI_CATALOG_REFRESH) if OPENAPI_CATALOG_REFRESH else 60
            if _catalog._refresh_task is None or _catalog._refresh_task.done():
                base_url = str(OPENAPI_BASE_URL).strip()
                api_key = str(OPENAPI_API_KEY).strip()
                _catalog._refresh_task = asyncio.create_task(
                    _catalog_refresh_loop(base_url, api_key, interval)
                )

    def _auto_select_model(self) -> None:
        """Auto-select model from catalog if current model is missing."""
        ids = _catalog.list_ids()
        if not ids:
            return
        # Current model already in catalog — nothing to do
        if self._current_model in ids:
            return
        # Single model served (typical for llama.cpp) — auto-select it
        if len(ids) == 1:
            old = self._current_model
            self.set_current_model(ids[0])
            log_info(
                f"[openapi] Auto-selected model '{ids[0]}' "
                f"(previous '{old}' not found in catalog)"
            )
            return
        # Multiple models but current not found — pick first and warn
        old = self._current_model
        self.set_current_model(ids[0])
        log_warning(
            f"[openapi] Model '{old}' not in catalog. "
            f"Auto-selected '{ids[0]}'. Available: {ids}"
        )

    # ------------------------------------------------------------------
    # AIPluginBase interface
    # ------------------------------------------------------------------

    def get_health_status(self) -> tuple[bool, str]:
        """Return (ok, error_message) indicating whether engine is ready."""
        base_url = str(OPENAPI_BASE_URL).strip()
        if not base_url:
            return (
                False,
                "OPENAPI_BASE_URL not configured. Please set it to your endpoint URL (e.g., http://localhost:8081/v1 for Ollama).",
            )

        # Quick connectivity check — try /v1/models first, then /models
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            check_paths = ["/models"]
        else:
            check_paths = ["/v1/models", "/models"]

        last_exc: Exception | None = None
        for path in check_paths:
            try:
                resp = requests.get(f"{base}{path}", timeout=5)
                if resp.status_code == 401 and not str(OPENAPI_API_KEY).strip():
                    return (
                        False,
                        "Endpoint requires authentication. Please set OPENAPI_API_KEY.",
                    )
                if resp.status_code in (200, 401, 403):
                    return True, ""
                # Non-success status on this path — try next
                last_exc = None
                break
            except requests.exceptions.ConnectionError:
                return (
                    False,
                    f"Cannot connect to endpoint {base_url}. Verify the URL and ensure the service is running.",
                )
            except requests.exceptions.Timeout:
                return (
                    False,
                    f"Timeout connecting to {base_url}. The endpoint may be slow or unreachable.",
                )
            except Exception as exc:
                last_exc = exc

        if last_exc:
            return False, f"Error connecting to endpoint: {last_exc}"

        return True, ""

    def get_supported_models(self) -> list[str]:
        """Return available model IDs from catalog."""
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
            self.model_limits_map["default"] = model.context_length
        log_info(f"[openapi] Active model updated: {name}")

    def get_rate_limit(self) -> tuple[int, int, float]:
        return (60, 60, 0.5)

    def get_interface_limits(self) -> dict[str, Any]:
        model = _catalog.get(self._current_model)
        ctx = model.context_length if model else int(OPENAPI_DEFAULT_CONTEXT)
        max_out = (
            model.max_completion_tokens if model else int(OPENAPI_DEFAULT_MAX_TOKENS)
        )
        return {
            "max_prompt_chars": ctx,
            "max_response_chars": max_out,
            "supports_images": bool(OPENAPI_SUPPORTS_VISION),
            "supports_functions": bool(OPENAPI_SUPPORTS_TOOLS),
            "supports_voice_interaction": False,
            "model_name": self._current_model,
        }

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
                f"[openapi] Processing message from chat_id="
                f"{getattr(message, 'chat_id', 'unknown')}"
            )

            response = await self.generate_response(prompt)

            if response:
                preview = response[:200] + "..." if len(response) > 200 else response
                log_info(f"[openapi] Generated response: {preview}")

            return response

        except Exception as exc:
            log_error(f"[openapi] Error in handle_incoming_message: {exc!r}")
            notify_trainer(f"OpenAPI error:\n{exc}")
            return f"Error during response generation: {exc}"
        finally:
            self._current_request_meta = None

    async def generate_response(self, prompt):  # type: ignore[override]
        """Send prompt to OpenAPI endpoint and return response text."""
        base_url = str(OPENAPI_BASE_URL).strip()
        if not base_url:
            log_warning("[openapi] OPENAPI_BASE_URL not configured")
            return "OpenAPI Base URL not configured. Please set OPENAPI_BASE_URL in settings."

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
                    f"[openapi] Processing system_message type '{sm_type}' as normal prompt"
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

            # === Phase 4: PromptRequest native-format path ===
            # When build_json_prompt() has attached a PromptRequest, use OpenAIRenderer
            # to produce a native messages list instead of a single JSON blob.
            _pr: PromptRequest | None = None
            if isinstance(prompt, PromptRequest):
                _pr = prompt
            elif isinstance(prompt, dict):
                candidate = prompt.get("__prompt_request")
                if isinstance(candidate, PromptRequest):
                    _pr = candidate
            if _pr is not None:
                from core.prompt_renderers import OpenAIRenderer

                _pr.supports_tool_calling = bool(OPENAPI_SUPPORTS_TOOLS)
                _mm_parts: list[dict[str, Any]] = []
                if bool(OPENAPI_SUPPORTS_VISION) or bool(OPENAPI_SUPPORTS_AUDIO):
                    _mm_parts = self._extract_multimodal_parts(prompt)
                renderer = OpenAIRenderer(_pr)
                _messages = (
                    renderer.render_with_multimodal(
                        _mm_parts, bool(OPENAPI_SUPPORTS_VISION)
                    )
                    if _mm_parts
                    else renderer.render()
                )
                _tools = renderer.tool_schemas()
                _model = _catalog.get(self._current_model)
                _max_tok = (
                    _model.max_completion_tokens
                    if _model
                    else int(OPENAPI_DEFAULT_MAX_TOKENS)
                )
                return await self._openai_chat_completion_from_messages(
                    _messages, _tools, _max_tok
                )

            if isinstance(prompt, dict):
                global _LEGACY_DICT_PROMPT_WARNED
                if not _LEGACY_DICT_PROMPT_WARNED:
                    log_debug(
                        "[openapi] dict prompt fallback path used (missing __prompt_request)"
                    )
                    _LEGACY_DICT_PROMPT_WARNED = True

            # Normalize prompt to text
            if isinstance(prompt, dict):
                prompt_text = json.dumps(
                    prompt, ensure_ascii=False, separators=(",", ":")
                )
            elif isinstance(prompt, str):
                prompt_text = prompt
            else:
                prompt_text = str(prompt)

            # Extract and redact multimodal parts
            multimodal_parts = []
            if bool(OPENAPI_SUPPORTS_VISION) or bool(OPENAPI_SUPPORTS_AUDIO):
                multimodal_parts = self._extract_multimodal_parts(prompt)

            if isinstance(prompt, dict) and multimodal_parts:
                redacted = self._copy_and_redact_data(prompt)
                prompt_text = json.dumps(
                    redacted, ensure_ascii=False, separators=(",", ":")
                )

            log_debug(
                f"[openapi] Sending prompt ({len(prompt_text)} chars) to {self._current_model}"
            )

            system_instruction = self._build_system_instruction(prompt)

            model = _catalog.get(self._current_model)
            max_tokens = (
                model.max_completion_tokens
                if model
                else int(OPENAPI_DEFAULT_MAX_TOKENS)
            )

            if isinstance(prompt, dict):
                legacy_messages: list[dict[str, Any]] = [
                    {"role": "system", "content": system_instruction}
                ]
                if multimodal_parts:
                    has_vision = bool(OPENAPI_SUPPORTS_VISION)
                    content_parts: list[dict[str, Any]] = []
                    if has_vision:
                        for part in multimodal_parts:
                            content_parts.append(part)
                    else:
                        log_warning(
                            f"[openapi] Vision disabled; skipping {len(multimodal_parts)} image part(s)"
                        )
                    content_parts.append({"type": "text", "text": prompt_text})
                    legacy_messages.append({"role": "user", "content": content_parts})
                else:
                    legacy_messages.append({"role": "user", "content": prompt_text})

                response_text = await self._openai_chat_completion_from_messages(
                    legacy_messages, [], max_tokens
                )
                log_debug(f"[openapi] Received response ({len(response_text)} chars)")
                return response_text

            response_text = await self._openai_chat_completion(
                prompt_text=prompt_text,
                system_instruction=system_instruction,
                max_tokens=max_tokens,
                multimodal_parts=multimodal_parts if multimodal_parts else None,
            )

            log_debug(f"[openapi] Received response ({len(response_text)} chars)")
            return response_text

        except Exception as exc:
            log_error(f"[openapi] Generation failed: {exc}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {"text": f"OpenAPI endpoint error: {exc}"},
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
        multimodal_parts: list[dict[str, Any]] | None = None,
    ) -> str:
        """Send chat completion request to OpenAPI endpoint."""
        base_url = str(OPENAPI_BASE_URL).strip()
        _base = base_url.rstrip("/")
        if _base.endswith("/v1"):
            url = f"{_base}/chat/completions"
        else:
            url = f"{_base}/v1/chat/completions"

        # Build messages
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_instruction}
        ]

        # User message with optional multimodal content
        if multimodal_parts:
            has_vision = bool(OPENAPI_SUPPORTS_VISION)
            content_parts: list[dict[str, Any]] = []

            if has_vision:
                for part in multimodal_parts:
                    content_parts.append(part)
            elif multimodal_parts:
                log_warning(
                    f"[openapi] Vision disabled; skipping {len(multimodal_parts)} image part(s)"
                )

            content_parts.append({"type": "text", "text": prompt_text})
            messages.append({"role": "user", "content": content_parts})
        else:
            messages.append({"role": "user", "content": prompt_text})

        # Build headers
        headers = self._build_headers()

        payload: dict[str, Any] = {
            "model": self._current_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        # Log outgoing request
        log_cortex_request(
            "openapi",
            model=self._current_model,
            url=url,
            headers=headers,
            payload=payload,
        )
        _req_start = time.monotonic()

        def _do_request() -> requests.Response:
            timeout = int(OPENAPI_TIMEOUT) if OPENAPI_TIMEOUT else 120
            return requests.post(url, headers=headers, json=payload, timeout=timeout)

        retryable_statuses = {429, 500, 503, 504}
        max_attempts = int(OPENAPI_MAX_RETRIES) if OPENAPI_MAX_RETRIES else 3
        response: requests.Response | None = None

        for attempt in range(max_attempts):
            try:
                loop = asyncio.get_event_loop()
                resp: requests.Response = await loop.run_in_executor(None, _do_request)
                response = resp
            except Exception as exc:
                if attempt < max_attempts - 1:
                    delay = min(8, 1 * (2**attempt))
                    log_warning(
                        f"[openapi] HTTP request failed (attempt {attempt + 1}/{max_attempts}): "
                        f"{exc}. Retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                log_error(f"[openapi] HTTP request failed: {exc}")
                return json.dumps(
                    {
                        "actions": [
                            {
                                "type": "system_message",
                                "payload": {
                                    "text": f"OpenAPI HTTP request failed: {exc}"
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
                        f"[openapi] HTTP error {status} "
                        f"(attempt {attempt + 1}/{max_attempts}). Retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                log_error(f"[openapi] HTTP error {status}: {resp.text}")
                return json.dumps(
                    {
                        "actions": [
                            {
                                "type": "system_message",
                                "payload": {
                                    "text": f"OpenAPI HTTP error {status}: {resp.text[:200]}"
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
                                "text": "OpenAPI HTTP request failed: no response"
                            },
                        }
                    ]
                }
            )

        try:
            data = response.json()
        except Exception as exc:
            log_error(f"[openapi] Response JSON parse failed: {exc}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {"text": "OpenAPI response was not valid JSON"},
                        }
                    ]
                }
            )

        # Handle error responses
        if "error" in data:
            error_msg = data["error"]
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            log_error(f"[openapi] API error: {error_msg}")
            _elapsed = (time.monotonic() - _req_start) * 1000
            log_cortex_response(
                "openapi",
                model=self._current_model,
                status=int(getattr(response, "status_code", 0) or 0),
                error=str(error_msg),
                elapsed_ms=_elapsed,
            )
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {"text": f"OpenAPI endpoint error: {error_msg}"},
                        }
                    ]
                }
            )

        choices = data.get("choices") or []
        if not choices:
            log_error(f"[openapi] Response missing choices: {data}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {"text": "OpenAPI response missing choices"},
                        }
                    ]
                }
            )

        content = choices[0].get("message", {}).get("content", "")
        if not content:
            log_error(f"[openapi] Response contained no content: {data}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {
                                "text": "OpenAPI response contained no content"
                            },
                        }
                    ]
                }
            )

        # Log usage if available
        usage = data.get("usage")
        if usage:
            log_debug(
                f"[openapi] Usage: prompt_tokens={usage.get('prompt_tokens')}, "
                f"completion_tokens={usage.get('completion_tokens')}"
            )

        # Log response
        _elapsed = (time.monotonic() - _req_start) * 1000
        log_cortex_response(
            "openapi",
            model=data.get("model", self._current_model),
            status=int(getattr(response, "status_code", 0) or 0),
            body=content.strip(),
            usage=usage,
            elapsed_ms=_elapsed,
        )

        return content.strip()

    async def _openai_chat_completion_from_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> str:
        """Send a pre-built messages list to the OpenAI-compat endpoint.

        Used by the Phase-4+ PromptRequest path. Handles both plain text
        responses and ``tool_calls`` responses (converting the latter back into
        SyntH's ``{"actions": [...]}`` JSON format via OpenAIRenderer).
        """
        from core.prompt_renderers import OpenAIRenderer

        base_url = str(OPENAPI_BASE_URL).strip()
        _base = base_url.rstrip("/")
        url = (
            f"{_base}/v1/chat/completions"
            if not _base.endswith("/v1")
            else f"{_base}/chat/completions"
        )

        payload: dict[str, Any] = {
            "model": self._current_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = self._build_headers()

        log_cortex_request(
            "openapi",
            model=self._current_model,
            url=url,
            headers=headers,
            payload=payload,
        )
        _req_start = time.monotonic()

        def _do_req() -> requests.Response:
            timeout = int(OPENAPI_TIMEOUT) if OPENAPI_TIMEOUT else 120
            return requests.post(url, headers=headers, json=payload, timeout=timeout)

        max_attempts = int(OPENAPI_MAX_RETRIES) if OPENAPI_MAX_RETRIES else 3

        def _error_json(msg: str) -> str:
            return json.dumps(
                {"actions": [{"type": "system_message", "payload": {"text": msg}}]}
            )

        response: requests.Response | None = None
        for attempt in range(max_attempts):
            try:
                loop = asyncio.get_event_loop()
                resp: requests.Response = await loop.run_in_executor(None, _do_req)
                response = resp
            except Exception as exc:
                if attempt < max_attempts - 1:
                    await asyncio.sleep(min(8, 1 * (2**attempt)))
                    continue
                log_error(f"[openapi] HTTP request failed: {exc}")
                return _error_json(f"OpenAPI HTTP request failed: {exc}")

            if int(resp.status_code) >= 400:  # type: ignore[arg-type]
                if (
                    int(resp.status_code) in {429, 500, 503, 504}  # type: ignore[arg-type]
                    and attempt < max_attempts - 1
                ):
                    await asyncio.sleep(min(8, 1 * (2**attempt)))
                    continue
                log_error(f"[openapi] HTTP error {resp.status_code}: {resp.text}")
                return _error_json(f"OpenAPI HTTP error {resp.status_code}")
            break

        if response is None:
            return _error_json("OpenAPI HTTP request failed: no response")

        try:
            data = response.json()
        except Exception as exc:
            log_error(f"[openapi] Response JSON parse failed: {exc}")
            return _error_json("OpenAPI response was not valid JSON")

        if "error" in data:
            err = data["error"]
            if isinstance(err, dict):
                err = err.get("message", str(err))
            log_error(f"[openapi] API error: {err}")
            return _error_json(f"OpenAPI endpoint error: {err}")

        choices = data.get("choices") or []
        if not choices:
            log_error(
                f"[openapi] Response missing choices (PromptRequest path): {data}"
            )
            return _error_json("OpenAPI response missing choices")

        choice_msg = choices[0].get("message", {})
        tool_calls = choice_msg.get("tool_calls")
        if tool_calls:
            # Convert tool_calls to SyntH actions format
            result_text = OpenAIRenderer.parse_tool_call_response(data)
        else:
            result_text = choice_msg.get("content") or ""
            if not result_text:
                log_error(
                    f"[openapi] Response contained no content (PromptRequest path): {data}"
                )
                return _error_json("OpenAPI response contained no content")

        usage = data.get("usage")
        _elapsed = (time.monotonic() - _req_start) * 1000
        log_cortex_response(
            "openapi",
            model=data.get("model", self._current_model),
            status=int(getattr(response, "status_code", 0) or 0),
            body=result_text.strip(),
            usage=usage,
            elapsed_ms=_elapsed,
        )
        return result_text.strip()

    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers with optional auth and custom headers."""
        headers: dict[str, str] = {"Content-Type": "application/json"}

        # Optional bearer token auth
        api_key = str(OPENAPI_API_KEY).strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Custom headers from JSON config
        custom = _parse_json(
            OPENAPI_CUSTOM_HEADERS.value
            if hasattr(OPENAPI_CUSTOM_HEADERS, "value")
            else str(OPENAPI_CUSTOM_HEADERS)
        )
        if isinstance(custom, dict):
            headers.update(custom)

        return headers

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

        is_grillo = interface == "grillo" or (
            isinstance(prompt_dict, dict) and prompt_dict.get("grillo_beat")
        )
        is_grillo_internal = is_grillo and (
            not isinstance(prompt_dict, dict)
            or not is_outbound_beat(prompt_dict.get("beat_type"))
        )

        if is_grillo_internal:
            interface_hint = (
                "CURRENT INTERFACE: grillo (INTERNAL)\n"
                "This is an internal introspection beat. Do NOT output any message_* actions.\n"
                "Use ONLY internal actions like 'create_personal_diary_entry', 'update_emotion_state', etc."
            )
        else:
            interface_hint = (
                f"CURRENT INTERFACE: {interface}\n"
                f"TO SEND A MESSAGE TO THE USER: Use action type '{message_action}'"
            )

        system_instruction = (
            "You are part of the 'Synthetic Heart' AI system.\n"
            "\n"
            "CRITICAL OUTPUT FORMAT:\n"
            "1. Respond with ONLY valid JSON - nothing before or after\n"
            "2. Your response MUST start with { and end with }\n"
            '3. Use this structure: {"actions": [{"type": "action_name", "payload": {...}}]}\n'
            "4. NO markdown code blocks, NO explanations outside JSON\n"
            "\n"
            f"{interface_hint}\n"
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
            log_debug("[openapi] Grillo beat detected — internal, no interface routing")
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

        log_warning(f"[openapi] Handling correction prompt: {error_type}")
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
    # Multimodal support (reused from openrouter with simplification)
    # ------------------------------------------------------------------

    def _extract_multimodal_parts(self, prompt: Any) -> list[dict[str, Any]]:
        """Extract image/audio parts from prompt for OpenAI format.

        Returns a list of OpenAI content parts.
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
        # Keys whose subtrees are action schema definitions (not multimodal
        # data) and should be skipped during recursive attachment collection.
        schema_only_keys = {"actions", "available_actions", "schema"}
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
                for key, value in container.items():
                    if key in schema_only_keys:
                        continue
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

            is_image = mime_type in _VISION_MIME_TYPES
            is_audio = mime_type in _AUDIO_MIME_TYPES

            if not is_image and not is_audio:
                if mime_type and not mime_type.startswith("application/octet"):
                    log_debug(f"[openapi] Skipping unsupported attachment: {mime_type}")
                continue

            b64_data = att.get("data") or att.get("base64", "")
            if not b64_data and file_path:
                try:
                    p = Path(file_path)
                    if p.exists():
                        b64_data = base64.b64encode(p.read_bytes()).decode("utf-8")
                except Exception as exc:
                    log_warning(f"[openapi] Failed to read file {file_path}: {exc}")
                    continue

            if not b64_data:
                continue

            if is_image and bool(OPENAPI_SUPPORTS_VISION):
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{b64_data}"},
                    }
                )
                log_debug(f"[openapi] Added image part: {mime_type}")
            elif is_audio and bool(OPENAPI_SUPPORTS_AUDIO):
                audio_fmt = _AUDIO_MIME_TYPES[mime_type]
                parts.append(
                    {
                        "type": "input_audio",
                        "input_audio": {"data": b64_data, "format": audio_fmt},
                    }
                )
                log_debug(f"[openapi] Added audio part: {mime_type} -> {audio_fmt}")

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
            log_warning(f"[openapi] Failed to redact prompt data: {exc}")
            return prompt


# Capability declaration for external_engines_base multi-registry discovery.
ENGINE_CAPABILITIES: dict[str, bool] = {"llm": True, "stt": False, "tts": False}

PLUGIN_CLASS = OpenAPIPlugin
