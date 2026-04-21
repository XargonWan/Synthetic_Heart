# cortex/external_engines/anthropic.py
"""Anthropic Claude LLM Engine for Synthetic Heart.

Uses the Anthropic Messages REST API (no SDK dependency) to communicate
with Claude models.

Key features:
- Pure REST via ``requests`` — no ``anthropic`` SDK required
- Configurable model, API key and base URL
- Handles SyntH correction prompts (invalid-JSON retry loop)
- Multimodal vision: inline base64 images for supported models
- ``get_supported_models()`` returns a static curated list; the list can
  be overridden at any time via ``ANTHROPIC_MODELS`` config
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import time
from pathlib import Path
from typing import Any

import requests

from core.ai_plugin_base import AIPluginBase
from core.config_manager import config_registry
from core.cortex_api_logger import log_cortex_request, log_cortex_response
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.prompt_request import PromptRequest

ENGINE_LABEL = "Anthropic Claude — Claude-family models via Messages API"

# Supported image MIME types for vision-capable Claude models
_VISION_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# ---------------------------------------------------------------------------
# Model catalogue
# ---------------------------------------------------------------------------

MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "claude-opus-4-5": {
        "description": "Claude Opus 4.5 — most capable, best for complex tasks",
        "context_length": 200000,
        "max_output_tokens": 8192,
        "supports_vision": True,
    },
    "claude-sonnet-4-5": {
        "description": "Claude Sonnet 4.5 — balanced performance and speed",
        "context_length": 200000,
        "max_output_tokens": 8192,
        "supports_vision": True,
    },
    "claude-haiku-3-5": {
        "description": "Claude Haiku 3.5 — fast and cost-efficient",
        "context_length": 200000,
        "max_output_tokens": 4096,
        "supports_vision": True,
    },
    "claude-3-5-sonnet-latest": {
        "description": "Claude 3.5 Sonnet (latest stable alias)",
        "context_length": 200000,
        "max_output_tokens": 8192,
        "supports_vision": True,
    },
    "claude-3-5-haiku-latest": {
        "description": "Claude 3.5 Haiku (latest stable alias)",
        "context_length": 200000,
        "max_output_tokens": 4096,
        "supports_vision": True,
    },
}

DEFAULT_MODEL = "claude-sonnet-4-5"
ANTHROPIC_API_VERSION = "2023-06-01"

# ---------------------------------------------------------------------------
# WebUI variable registration
# ---------------------------------------------------------------------------
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "ANTHROPIC_API_KEY",
        label="Anthropic API Key",
        default="",
        value_type=str,
        ui_type="password",
        description="API key for Anthropic Claude (https://console.anthropic.com/keys).",
        scope="llm",
        component="anthropic",
        tags=["cortex_engine", "sensitive"],
        needs_component_reload=True,
    )
    register_exposed_var(
        "ANTHROPIC_BASE_URL",
        label="Anthropic Base URL",
        default="https://api.anthropic.com",
        value_type=str,
        ui_type="string",
        description="Base URL for the Anthropic API (change only for proxies).",
        scope="llm",
        component="anthropic",
        tags=["cortex_engine"],
        advanced=True,
        needs_component_reload=True,
    )
    register_exposed_var(
        "ANTHROPIC_DEFAULT_MODEL",
        label="Default Model",
        default=DEFAULT_MODEL,
        value_type=str,
        ui_type="combobox",
        options=list(MODEL_CONFIGS.keys()),
        description="Default Claude model to use when no scope/action override matches.",
        scope="llm",
        component="anthropic",
        tags=["cortex_engine"],
        needs_component_reload=False,
    )
    register_exposed_var(
        "ANTHROPIC_MAX_TOKENS",
        label="Max Output Tokens",
        default="8192",
        value_type=int,
        ui_type="number",
        description="Maximum tokens to generate per response.",
        scope="llm",
        component="anthropic",
        tags=["cortex_engine"],
        advanced=True,
    )
except Exception:
    pass

# ---------------------------------------------------------------------------
# Config variables
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = config_registry.get_var(
    "ANTHROPIC_API_KEY",
    "",
    label="Anthropic API Key",
    description="API key for Anthropic Claude.",
    group="llm",
    component="anthropic",
    sensitive=True,
)

ANTHROPIC_BASE_URL = config_registry.get_var(
    "ANTHROPIC_BASE_URL",
    "https://api.anthropic.com",
    label="Anthropic Base URL",
    description="Base URL for the Anthropic API.",
    group="llm",
    component="anthropic",
    advanced=True,
)

ANTHROPIC_DEFAULT_MODEL = config_registry.get_var(
    "ANTHROPIC_DEFAULT_MODEL",
    DEFAULT_MODEL,
    label="Default Model",
    description="Default Claude model.",
    group="llm",
    component="anthropic",
)

ANTHROPIC_MAX_TOKENS = config_registry.get_var(
    "ANTHROPIC_MAX_TOKENS",
    8192,
    label="Max Output Tokens",
    description="Maximum tokens to generate per response.",
    group="llm",
    component="anthropic",
    value_type=int,
    advanced=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key.strip(),
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }


def _extract_image_parts(prompt: Any) -> list[dict[str, Any]]:
    """Return Anthropic-format inline image content blocks extracted from *prompt*."""
    parts: list[dict[str, Any]] = []
    if not isinstance(prompt, dict):
        return parts

    attachments: list[Any] = []
    for key in ("attachments", "images", "media"):
        val = prompt.get(key)
        if isinstance(val, list):
            attachments.extend(val)

    for att in attachments:
        if not isinstance(att, dict):
            continue
        mime = att.get("mime_type", "") or att.get("type", "")
        if mime not in _VISION_MIME_TYPES:
            continue
        raw: bytes | str | None = att.get("data") or att.get("base64")
        if raw is None:
            path_str = att.get("path") or att.get("file_path")
            if path_str and Path(path_str).exists():
                raw = Path(path_str).read_bytes()
        if raw is None:
            continue
        if isinstance(raw, bytes):
            raw = base64.b64encode(raw).decode()
        parts.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": raw,
                },
            }
        )
    return parts


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class AnthropicPlugin(AIPluginBase):
    """Anthropic Claude LLM Engine using the Messages REST API."""

    display_name = "Anthropic Claude"
    supports_prompt_request = True

    def __init__(self, notify_fn: Any = None) -> None:
        from core.notifier import set_notifier

        if notify_fn:
            set_notifier(notify_fn)
            self._notify_fn = notify_fn
        else:
            self._notify_fn = lambda chat_id, message: log_info(
                f"[anthropic] NOTIFY fallback: {message}"
            )
            set_notifier(self._notify_fn)

        self._current_model: str = str(ANTHROPIC_DEFAULT_MODEL) or DEFAULT_MODEL
        if self._current_model not in MODEL_CONFIGS:
            self._current_model = DEFAULT_MODEL

        self._current_request_meta: dict[str, Any] | None = None

        # Model limits map for plugin_instance.py compatibility
        cfg = MODEL_CONFIGS.get(self._current_model, {})
        self.model_limits_map: dict[str, int] = {
            "default": cfg.get("context_length", 200000) * 3
        }

        log_info(f"[anthropic] Initialized with model: {self._current_model}")

    # ------------------------------------------------------------------
    # AIPluginBase interface
    # ------------------------------------------------------------------

    def get_health_status(self) -> tuple[bool, str]:
        if not ANTHROPIC_API_KEY or not str(ANTHROPIC_API_KEY).strip():
            return False, "ANTHROPIC_API_KEY not configured"
        return True, ""

    def get_supported_models(self) -> list[str]:
        return list(MODEL_CONFIGS.keys())

    def get_current_model(self) -> str:
        return self._current_model

    def set_current_model(self, name: str) -> None:
        if name not in MODEL_CONFIGS:
            log_warning(f"[anthropic] Unknown model '{name}', keeping current")
            return
        self._current_model = name
        cfg = MODEL_CONFIGS[name]
        self.model_limits_map["default"] = cfg.get("context_length", 200000) * 3
        log_info(f"[anthropic] Active model updated: {name}")

    def get_rate_limit(self) -> tuple[int, int, float]:
        # 60 requests / 60 s window / 0.5 s min delay
        return (60, 60, 0.5)

    def get_interface_limits(self) -> dict[str, Any]:
        cfg = MODEL_CONFIGS.get(self._current_model, {})
        return {
            "max_prompt_chars": cfg.get("context_length", 200000) * 3,
            "max_response_chars": cfg.get("max_output_tokens", 8192),
            "supports_images": cfg.get("supports_vision", True),
            "supports_functions": False,
            "supports_voice_interaction": False,
            "model_name": self._current_model,
        }

    def supports_agent(self) -> bool:
        return True

    def agent_execute(self, action_dict: dict, context: dict | None = None) -> dict:
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            agent_plugin = PLUGIN_REGISTRY.get("agent") if PLUGIN_REGISTRY else None
            if agent_plugin and hasattr(agent_plugin, "execute_action"):
                return agent_plugin.execute_action(action_dict, context=context)
        except Exception as exc:
            log_warning(f"[anthropic] agent_execute delegation failed: {exc}")
        return {"success": False, "error": "agent plugin unavailable"}

    async def handle_incoming_message(self, bot: Any, message: Any, prompt: Any) -> str:
        """Process a message using a pre-built prompt and return the response."""
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
                f"[anthropic] Processing message from chat_id="
                f"{getattr(message, 'chat_id', 'unknown')}"
            )

            response = await self.generate_response(prompt)

            if response:
                preview = response[:200] + ("..." if len(response) > 200 else "")
                log_info(f"[anthropic] Generated response: {preview}")

            return response

        except Exception as exc:
            log_error(f"[anthropic] Error in handle_incoming_message: {exc!r}")
            notify_trainer(f"Anthropic error:\n{exc}")
            return f"Error during response generation: {exc}"
        finally:
            self._current_request_meta = None

    async def generate_response(self, prompt: Any) -> str:  # type: ignore[override]
        """Send prompt to Anthropic Claude and return the response text."""
        api_key = str(ANTHROPIC_API_KEY).strip() if ANTHROPIC_API_KEY else ""
        if not api_key:
            log_warning("[anthropic] ANTHROPIC_API_KEY not configured")
            return "Anthropic API Key not configured. Please set ANTHROPIC_API_KEY in settings."

        try:
            # Dispatch correction prompts through the dedicated handler
            if isinstance(prompt, dict) and "system_message" in prompt:
                sm = prompt.get("system_message", {})
                sm_type = sm.get("type", "") if isinstance(sm, dict) else ""
                if sm_type in (
                    "error",
                    "correction",
                    "invalid_json",
                    "validation_error",
                ):
                    return await self._handle_correction_prompt(prompt, api_key)

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
                            return await self._handle_correction_prompt(parsed, api_key)
                except (json.JSONDecodeError, ValueError):
                    pass

            # === Phase 5: PromptRequest native-format path (with prompt caching) ===
            _pr: PromptRequest | None = None
            if isinstance(prompt, PromptRequest):
                _pr = prompt
            elif isinstance(prompt, dict):
                candidate = prompt.get("__prompt_request")
                if isinstance(candidate, PromptRequest):
                    _pr = candidate
            if _pr is not None:
                from core.prompt_renderers import AnthropicRenderer

                enable_caching = bool(
                    config_registry.get_value("ENABLE_PROMPT_CACHING", True)
                )
                renderer = AnthropicRenderer(_pr, enable_caching=enable_caching)
                img_parts = _extract_image_parts(prompt)
                rendered = (
                    renderer.render_with_image_parts(img_parts)
                    if img_parts
                    else renderer.render()
                )
                return await self._call_api_with_messages(
                    rendered_system=rendered.get("system", []),
                    messages=rendered.get("messages", []),
                    tools=rendered.get("tools") or [],
                    api_key=api_key,
                )

            # Fallback path: still support non-PromptRequest callers without
            # rebuilding a legacy JSON blob prompt pipeline.
            if isinstance(prompt, dict):
                prompt_text = json.dumps(
                    {k: v for k, v in prompt.items() if k != "__prompt_request"},
                    ensure_ascii=False,
                )
            elif isinstance(prompt, str):
                prompt_text = prompt
            else:
                prompt_text = str(prompt)

            image_parts = _extract_image_parts(prompt)

            if isinstance(prompt, dict):
                content: list[dict[str, Any]] = []
                if image_parts:
                    content.extend(image_parts)
                content.append({"type": "text", "text": prompt_text})
                return await self._call_api_with_messages(
                    rendered_system=[],
                    messages=[{"role": "user", "content": content}],
                    tools=[],
                    api_key=api_key,
                )

            return await self._call_api(
                prompt_text=prompt_text,
                image_parts=image_parts,
                api_key=api_key,
            )

        except Exception as exc:
            log_error(f"[anthropic] generate_response failed: {exc!r}")
            return f"Error generating response: {exc}"

    async def _call_api(
        self,
        prompt_text: str,
        api_key: str,
        image_parts: list[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Perform the actual Anthropic Messages API call (runs in executor)."""
        base_url = (
            str(ANTHROPIC_BASE_URL).rstrip("/")
            if ANTHROPIC_BASE_URL
            else "https://api.anthropic.com"
        )
        model = self._current_model
        max_out = max_tokens or int(ANTHROPIC_MAX_TOKENS or 8192)

        # Build user content
        content: list[dict[str, Any]] = []
        if image_parts:
            content.extend(image_parts)
        content.append({"type": "text", "text": prompt_text})

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_out,
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            payload["system"] = system

        headers = _build_headers(api_key)

        url = f"{base_url}/v1/messages"
        log_cortex_request(
            engine="anthropic",
            model=model,
            url=url,
            payload=payload,
        )

        start = time.monotonic()
        loop = asyncio.get_event_loop()
        resp_data = await loop.run_in_executor(
            None, lambda: self._post_sync(base_url, headers, payload)
        )
        elapsed = time.monotonic() - start

        if isinstance(resp_data, str):
            # Error string returned from _post_sync
            log_cortex_response(
                engine="anthropic",
                model=model,
                error=resp_data,
                elapsed_ms=elapsed * 1000,
            )
            return resp_data

        text = self._extract_text(resp_data)
        usage = resp_data.get("usage") if isinstance(resp_data, dict) else None
        log_cortex_response(
            engine="anthropic",
            model=model,
            body=resp_data,
            usage=usage,
            elapsed_ms=elapsed * 1000,
        )
        return text

    async def _call_api_with_messages(
        self,
        rendered_system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        api_key: str,
        max_tokens: int | None = None,
    ) -> str:
        """Phase-5 Anthropic call: pre-rendered system blocks + messages list.

        Supports native prompt caching (cache_control blocks are embedded by
        AnthropicRenderer) and converts ``tool_use`` blocks to SyntH's
        ``{\"actions\": [...]}`` format via ``AnthropicRenderer``.
        """
        from core.prompt_renderers import AnthropicRenderer

        base_url = (
            str(ANTHROPIC_BASE_URL).rstrip("/")
            if ANTHROPIC_BASE_URL
            else "https://api.anthropic.com"
        )
        model = self._current_model
        max_out = max_tokens or int(ANTHROPIC_MAX_TOKENS or 8192)

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_out,
            "system": rendered_system,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        headers = _build_headers(api_key)
        url = f"{base_url}/v1/messages"
        log_cortex_request(
            engine="anthropic",
            model=model,
            url=url,
            payload=payload,
        )

        start = time.monotonic()
        loop = asyncio.get_event_loop()
        resp_data = await loop.run_in_executor(
            None, lambda: self._post_sync(base_url, headers, payload)
        )
        elapsed = time.monotonic() - start

        if isinstance(resp_data, str):
            log_cortex_response(
                engine="anthropic",
                model=model,
                error=resp_data,
                elapsed_ms=elapsed * 1000,
            )
            return resp_data

        # Phase 5: tool_use blocks → SyntH actions format
        content_blocks = (
            resp_data.get("content", []) if isinstance(resp_data, dict) else []
        )
        has_tool_use = any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content_blocks
        )
        if has_tool_use:
            result_text = AnthropicRenderer.parse_tool_use_response(resp_data)
        else:
            result_text = self._extract_text(resp_data)

        usage = resp_data.get("usage") if isinstance(resp_data, dict) else None
        log_cortex_response(
            engine="anthropic",
            model=model,
            body=resp_data,
            usage=usage,
            elapsed_ms=elapsed * 1000,
        )
        return result_text

    def _post_sync(
        self, base_url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any] | str:
        """Blocking HTTP POST to the Anthropic Messages endpoint."""
        url = f"{base_url}/v1/messages"
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            body = ""
            try:
                body = exc.response.text[:500]
            except Exception:
                pass
            log_error(f"[anthropic] HTTP {exc.response.status_code}: {body}")
            return f"Anthropic API error ({exc.response.status_code}): {body}"
        except Exception as exc:
            log_error(f"[anthropic] Request failed: {exc!r}")
            return f"Anthropic request failed: {exc}"

    def _extract_text(self, data: dict[str, Any]) -> str:
        """Extract response text from an Anthropic API response dict."""
        try:
            for block in data.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    return str(block.get("text", "")).strip()
        except Exception as exc:
            log_warning(f"[anthropic] Failed to extract text from response: {exc}")
        return str(data)

    async def _handle_correction_prompt(
        self, prompt: dict[str, Any], api_key: str
    ) -> str:
        """Handle SyntH correction / invalid-JSON retry prompts."""
        sm = prompt.get("system_message", {}) or {}
        if not isinstance(sm, dict):
            sm = {}

        instructions: str = (
            sm.get("instructions", "")
            or sm.get("content", "")
            or json.dumps(sm, ensure_ascii=False)
        )
        interface = sm.get("interface", "")
        message_action = sm.get("message_action", "send_message")

        correction_prompt = {
            "correction_task": instructions,
            "original_prompt": copy.deepcopy(prompt),
        }
        correction_text = json.dumps(correction_prompt, indent=2, ensure_ascii=False)

        system = (
            "You are a JSON correction assistant. "
            "Output ONLY valid JSON following the exact structure shown. "
            f"CURRENT INTERFACE: {interface}. "
            f"TO SEND A MESSAGE TO THE USER: Use action type '{message_action}'. "
            "NO explanations. NO markdown. ONLY valid JSON starting with {{ and ending with }}."
        )

        return await self._call_api(
            prompt_text=correction_text,
            api_key=api_key,
            system=system,
        )

    def get_supported_actions(self) -> dict[str, Any]:
        return {}


# ---------------------------------------------------------------------------
# Registry exports
# ---------------------------------------------------------------------------

# Capability declaration for external_engines_base multi-registry discovery.
ENGINE_CAPABILITIES: dict[str, bool] = {"llm": True, "stt": False, "tts": False}

PLUGIN_CLASS = AnthropicPlugin
