# engines/external_engines/xai_grok.py
"""xAI Grok LLM Engine for Synthetic Heart.

Uses the xAI Chat Completions REST API (no SDK dependency) to communicate
with Grok models. Grok's Chat Completions endpoint is OpenAI-compatible.

Key features:
- Pure REST via ``requests`` — no SDK required
- Configurable model, API key, and base URL
- Handles SyntH correction prompts (invalid-JSON retry loop)
- Multimodal vision support (inline base64 images)
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
from core.beat_utils import is_outbound_beat
from core.config_manager import config_registry
from core.cortex_api_logger import log_cortex_request, log_cortex_response
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.prompt_request import PromptRequest

ENGINE_LABEL = "xAI Grok — Grok-family models via Chat Completions API"

# Supported image MIME types for vision-capable Grok models
_VISION_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# ---------------------------------------------------------------------------
# Model catalogue
# ---------------------------------------------------------------------------

MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "grok-4-1-fast-reasoning": {
        "description": "Grok 4.1 Fast Reasoning — Optimized for agentic tool calling with reasoning",
        "context_length": 2000000,
        "max_output_tokens": 4096,
        "supports_vision": False,
    },
    "grok-4-1-fast-non-reasoning": {
        "description": "Grok 4.1 Fast Non-Reasoning — Fast variant without extended reasoning",
        "context_length": 2000000,
        "max_output_tokens": 4096,
        "supports_vision": False,
    },
    "grok-code-fast-1": {
        "description": "Grok Code Fast 1 — Lightweight agentic model optimized for coding tasks",
        "context_length": 256000,
        "max_output_tokens": 4096,
        "supports_vision": False,
    },
    "grok-4-fast-reasoning": {
        "description": "Grok 4 Fast Reasoning — Fast variant with reasoning capabilities enabled",
        "context_length": 2000000,
        "max_output_tokens": 4096,
        "supports_vision": False,
    },
    "grok-4-fast-non-reasoning": {
        "description": "Grok 4 Fast Non-Reasoning — Fast variant without extended reasoning",
        "context_length": 2000000,
        "max_output_tokens": 4096,
        "supports_vision": False,
    },
    "grok-4-0709": {
        "description": "Grok 4 (July 2024 Release) — Flagship reasoning model with vision",
        "context_length": 256000,
        "max_output_tokens": 4096,
        "supports_vision": True,
    },
    "grok-3-mini": {
        "description": "Grok 3 Mini — Smaller, faster variant of Grok 3 for simpler tasks",
        "context_length": 131072,
        "max_output_tokens": 4096,
        "supports_vision": False,
    },
    "grok-3": {
        "description": "Grok 3 — Previous generation flagship model",
        "context_length": 131072,
        "max_output_tokens": 4096,
        "supports_vision": False,
    },
    "grok-2-vision-1212": {
        "description": "Grok 2 Vision — Vision-capable model for image understanding tasks",
        "context_length": 32768,
        "max_output_tokens": 4096,
        "supports_vision": True,
    },
}

DEFAULT_MODEL = "grok-4-1-fast-reasoning"

# ---------------------------------------------------------------------------
# WebUI variable registration
# ---------------------------------------------------------------------------
try:
    from core.variables_engine import register_exposed_var

    # API key, base URL and default model live in the Engines tab
    # (external_endpoints), not in Settings.
    register_exposed_var(
        "XAI_MAX_TOKENS",
        label="Max Output Tokens",
        default="4096",
        value_type=int,
        ui_type="number",
        description="Maximum tokens to generate per response.",
        scope="llm",
        component="xai_grok",
        tags=["cortex_engine"],
        advanced=True,
    )
except Exception:
    pass

# ---------------------------------------------------------------------------
# Config variables
# ---------------------------------------------------------------------------
XAI_API_KEY = config_registry.get_var(
    "XAI_API_KEY",
    "",
    label="XAI API Key",
    description="API key for xAI Grok.",
    group="llm",
    component="xai_grok",
    sensitive=True,
    hidden=True,
)

XAI_BASE_URL = config_registry.get_var(
    "XAI_BASE_URL",
    "https://api.x.ai",
    label="XAI Base URL",
    description="Base URL for the xAI API.",
    group="llm",
    component="xai_grok",
    advanced=True,
    hidden=True,
)

XAI_DEFAULT_MODEL = config_registry.get_var(
    "XAI_DEFAULT_MODEL",
    DEFAULT_MODEL,
    label="Default Model",
    description="Default Grok model.",
    group="llm",
    component="xai_grok",
    hidden=True,
)

XAI_MAX_TOKENS = config_registry.get_var(
    "XAI_MAX_TOKENS",
    4096,
    label="Max Output Tokens",
    description="Maximum tokens to generate per response.",
    group="llm",
    component="xai_grok",
    value_type=int,
    advanced=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class XaiGrokPlugin(AIPluginBase):
    """xAI Grok LLM Engine using the Chat Completions REST API."""

    display_name = "xAI Grok"
    supports_prompt_request = True

    def __init__(self, notify_fn: Any = None) -> None:
        from core.notifier import set_notifier

        if notify_fn:
            set_notifier(notify_fn)
            self._notify_fn = notify_fn
        else:
            self._notify_fn = lambda chat_id, message: log_info(
                f"[xai_grok] NOTIFY fallback: {message}"
            )
            set_notifier(self._notify_fn)

        self._current_model: str = str(XAI_DEFAULT_MODEL) or DEFAULT_MODEL
        if self._current_model not in MODEL_CONFIGS:
            self._current_model = DEFAULT_MODEL

        self._current_request_meta: dict[str, Any] | None = None

        cfg = MODEL_CONFIGS.get(self._current_model, {})
        self.model_limits_map: dict[str, int] = {
            "default": cfg.get("context_length", 256000) * 3
        }

        log_info(f"[xai_grok] Initialized with model: {self._current_model}")

    # ------------------------------------------------------------------
    # AIPluginBase interface
    # ------------------------------------------------------------------

    def get_health_status(self) -> tuple[bool, str]:
        if not XAI_API_KEY or not str(XAI_API_KEY).strip():
            return False, "XAI_API_KEY not configured"
        return True, ""

    def get_supported_models(self) -> list[str]:
        return list(MODEL_CONFIGS.keys())

    def get_current_model(self) -> str:
        return self._current_model

    def set_current_model(self, name: str) -> None:
        if name not in MODEL_CONFIGS:
            log_warning(f"[xai_grok] Unknown model '{name}', keeping current")
            return
        self._current_model = name
        cfg = MODEL_CONFIGS[name]
        self.model_limits_map["default"] = cfg.get("context_length", 256000) * 3
        log_info(f"[xai_grok] Active model updated: {name}")

    def get_rate_limit(self) -> tuple[int, int, float]:
        # 60 requests / 60 s window / 0.5 s min delay
        return (60, 60, 0.5)

    def get_interface_limits(self) -> dict[str, Any]:
        cfg = MODEL_CONFIGS.get(self._current_model, {})
        return {
            "max_prompt_chars": cfg.get("context_length", 256000) * 3,
            "max_response_chars": cfg.get("max_output_tokens", 4096),
            "supports_images": cfg.get("supports_vision", False),
            "supports_functions": True,
            "supports_voice_interaction": False,
            "model_name": self._current_model,
        }

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
                f"[xai_grok] Processing message from chat_id="
                f"{getattr(message, 'chat_id', 'unknown')}"
            )

            response = await self.generate_response(prompt)

            if response:
                preview = response[:200] + ("..." if len(response) > 200 else "")
                log_info(f"[xai_grok] Generated response: {preview}")

            return response

        except Exception as exc:
            log_error(f"[xai_grok] Error in handle_incoming_message: {exc!r}")
            notify_trainer(f"xAI Grok error:\n{exc}")
            return f"Error during response generation: {exc}"
        finally:
            self._current_request_meta = None

    async def generate_response(self, messages: Any) -> str:
        """Send prompt to xAI Grok and return the response text."""
        api_key = str(XAI_API_KEY).strip() if XAI_API_KEY else ""
        if not api_key:
            log_warning("[xai_grok] XAI_API_KEY not configured")
            return "xAI API Key not configured. Please set XAI_API_KEY in settings."

        try:
            # Dispatch correction prompts through the dedicated handler
            if isinstance(messages, dict) and "system_message" in messages:
                sm = messages.get("system_message", {})
                sm_type = sm.get("type", "") if isinstance(sm, dict) else ""
                if sm_type in (
                    "error",
                    "correction",
                    "invalid_json",
                    "validation_error",
                ):
                    return await self._handle_correction_prompt(messages, api_key)

            elif isinstance(messages, str):
                try:
                    parsed = json.loads(messages)
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

            # === PromptRequest native-format path ===
            _pr: PromptRequest | None = None
            if isinstance(messages, PromptRequest):
                _pr = messages
            elif isinstance(messages, dict):
                candidate = messages.get("__prompt_request")
                if isinstance(candidate, PromptRequest):
                    _pr = candidate

            if _pr is not None:
                from core.prompt_renderers import OpenAIRenderer

                _pr.supports_tool_calling = True
                renderer = OpenAIRenderer(_pr)

                cfg = MODEL_CONFIGS.get(self._current_model, {})
                supports_vision = bool(cfg.get("supports_vision", False))

                img_parts = self._extract_image_parts(messages)
                rendered_messages = (
                    renderer.render_with_multimodal(img_parts, supports_vision)
                    if img_parts
                    else renderer.render()
                )
                tools = renderer.tool_schemas()

                return await self._call_api_with_messages(
                    messages=rendered_messages,
                    tools=tools,
                    api_key=api_key,
                )

            # Fallback path for non-PromptRequest callers
            if isinstance(messages, dict):
                prompt_text = json.dumps(
                    {k: v for k, v in messages.items() if k != "__prompt_request"},
                    ensure_ascii=False,
                )
            elif isinstance(messages, str):
                prompt_text = messages
            else:
                prompt_text = str(messages)

            image_parts = self._extract_image_parts(messages)
            system_instruction = self._build_system_instruction(messages)

            return await self._call_api(
                prompt_text=prompt_text,
                image_parts=image_parts if image_parts else None,
                system=system_instruction,
                api_key=api_key,
            )

        except Exception as exc:
            log_error(f"[xai_grok] generate_response failed: {exc!r}")
            return f"Error generating response: {exc}"

    async def _call_api(
        self,
        prompt_text: str,
        api_key: str,
        image_parts: list[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Perform the Chat Completions API call with a constructed user message."""
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})

        content: list[dict[str, Any]] | str
        if image_parts:
            content = []
            cfg = MODEL_CONFIGS.get(self._current_model, {})
            supports_vision = bool(cfg.get("supports_vision", False))
            if supports_vision:
                content.extend(image_parts)
            else:
                log_warning(
                    f"[xai_grok] Vision disabled for model {self._current_model}; skipping image parts"
                )
            content.append({"type": "text", "text": prompt_text})
        else:
            content = prompt_text

        messages.append({"role": "user", "content": content})

        return await self._call_api_with_messages(
            messages=messages,
            tools=[],
            api_key=api_key,
            max_tokens=max_tokens,
        )

    async def _call_api_with_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        api_key: str,
        max_tokens: int | None = None,
    ) -> str:
        """Perform the actual Chat Completions API call."""
        from core.prompt_renderers import OpenAIRenderer

        base_url = str(XAI_BASE_URL).rstrip("/") if XAI_BASE_URL else "https://api.x.ai"
        url = f"{base_url}/v1/chat/completions"
        model = self._current_model
        max_out = max_tokens or int(XAI_MAX_TOKENS or 4096)

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_out,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = _build_headers(api_key)
        log_cortex_request(
            engine="xai_grok",
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
                engine="xai_grok",
                model=model,
                error=resp_data,
                elapsed_ms=elapsed * 1000,
            )
            return resp_data

        choices = resp_data.get("choices", [])
        if not choices:
            err = "Response missing choices"
            log_cortex_response(
                engine="xai_grok",
                model=model,
                error=err,
                elapsed_ms=elapsed * 1000,
            )
            return f"xAI Grok error: {err}"

        choice_msg = choices[0].get("message", {})
        tool_calls = choice_msg.get("tool_calls")

        if tool_calls:
            result_text = OpenAIRenderer.parse_tool_call_response(resp_data)
        else:
            result_text = choice_msg.get("content") or ""

        usage = resp_data.get("usage")
        log_cortex_response(
            engine="xai_grok",
            model=model,
            body=result_text.strip(),
            usage=usage,
            elapsed_ms=elapsed * 1000,
        )
        return result_text.strip()

    def _post_sync(
        self, base_url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any] | str:
        """Blocking HTTP POST to the xAI Chat Completions endpoint."""
        url = f"{base_url}/v1/chat/completions"
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
            log_error(f"[xai_grok] HTTP {exc.response.status_code}: {body}")
            return f"xAI Grok API error ({exc.response.status_code}): {body}"
        except Exception as exc:
            log_error(f"[xai_grok] Request failed: {exc!r}")
            return f"xAI Grok request failed: {exc}"

    def _extract_image_parts(self, prompt: Any) -> list[dict[str, Any]]:
        """Extract multimodal image attachments from prompt."""
        parts: list[dict[str, Any]] = []

        if isinstance(prompt, str):
            try:
                prompt = json.loads(prompt)
            except (json.JSONDecodeError, ValueError):
                return parts

        if not isinstance(prompt, dict):
            return parts

        multimodal_keys = {"attachments", "images", "documents", "videos"}
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

            if mime_type not in _VISION_MIME_TYPES:
                continue

            b64_data = att.get("data") or att.get("base64", "")
            if not b64_data and file_path:
                try:
                    p = Path(file_path)
                    if p.exists():
                        b64_data = base64.b64encode(p.read_bytes()).decode("utf-8")
                except Exception as exc:
                    log_warning(f"[xai_grok] Failed to read file {file_path}: {exc}")
                    continue

            if not b64_data:
                continue

            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{b64_data}"},
                }
            )
            log_debug(f"[xai_grok] Added image part: {mime_type}")

        return parts

    def _copy_and_redact_data(self, prompt: dict[str, Any]) -> dict[str, Any]:
        """Deep copy prompt and redact heavy base64 data."""
        try:
            redacted = copy.deepcopy(prompt)
            multimodal_keys = {"attachments", "images", "documents", "videos"}
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
            log_warning(f"[xai_grok] Failed to redact prompt data: {exc}")
            return prompt

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
            "original_prompt": self._copy_and_redact_data(prompt),
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
            max_tokens=8192,
        )

    def get_supported_actions(self) -> dict[str, Any]:
        return {}


# Capability declaration for external_engines_base discovery.
ENGINE_CAPABILITIES: dict[str, bool] = {"llm": True, "stt": False, "tts": False}

PLUGIN_CLASS = XaiGrokPlugin
