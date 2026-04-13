# core/external_endpoints/bridges/cortex_bridge.py
"""Cortex (LLM) bridge for external endpoints.

Wraps a :class:`BaseProtocolAdapter` in the ``AIPluginBase`` interface so
that any external endpoint registered as a Cortex engine behaves identically
to a built-in engine from the perspective of the SyntH core.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import TYPE_CHECKING, Any

from core.ai_plugin_base import AIPluginBase
from core.logging_utils import log_debug, log_warning

if TYPE_CHECKING:
    from core.external_endpoints.adapters.base import BaseProtocolAdapter
    from core.external_endpoints.models import ExternalEndpoint


# ---------------------------------------------------------------------------
# Multimodal extraction helper
# ---------------------------------------------------------------------------

# Keys whose values can contain lists of multimodal attachment dicts.
_MM_KEYS: frozenset[str] = frozenset(
    {"attachments", "images", "audio", "documents", "videos"}
)
# Subtree-root keys that describe action *schemas*, not actual media data.
_SCHEMA_KEYS: frozenset[str] = frozenset({"actions", "available_actions", "schema"})


def _extract_attachments_and_redact(
    prompt: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Extract multimodal attachments from *prompt* and return a redacted copy.

    Recursively walks the prompt dict looking for attachment items that contain
    base64-encoded data (``data`` or ``base64`` field) alongside a ``mime_type``.
    Those are collected as ``{"mime_type": …, "data": …}`` dicts and their
    base64 payload is replaced with a short placeholder in the returned copy so
    the JSON text sent to the model stays compact.

    Returns:
        A ``(redacted_prompt_copy, multimodal_parts)`` tuple.
    """
    redacted = copy.deepcopy(prompt)
    parts: list[dict[str, str]] = []

    def _try_extract(item: Any) -> None:
        """If *item* looks like an attachment with base64, collect + redact it."""
        if not isinstance(item, dict):
            return
        mime = item.get("mime_type") or item.get("mimeType")
        if not mime:
            return
        for field in ("data", "base64"):
            b64 = item.get(field)
            if b64 and isinstance(b64, str) and len(b64) > 256:
                parts.append({"mime_type": str(mime), "data": b64})
                item[field] = f"<binary: {len(b64)} chars>"
                return  # one data field per attachment

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in _MM_KEYS:
                items = node.get(key)
                if isinstance(items, list):
                    for item in items:
                        _try_extract(item)
                elif isinstance(items, dict):
                    _try_extract(items)
            for key, val in node.items():
                if key not in _SCHEMA_KEYS and key not in _MM_KEYS:
                    _walk(val)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(redacted)
    return redacted, parts


class ExternalCortexEngine(AIPluginBase):
    """AIPluginBase implementation backed by an external endpoint adapter."""

    def __init__(
        self,
        endpoint: "ExternalEndpoint",
        adapter: "BaseProtocolAdapter",
        notify_fn: Any = None,
    ) -> None:
        self._endpoint = endpoint
        self._adapter = adapter
        self._adapter._engine_label = endpoint.name or "cortex_bridge"
        self.notify_fn = notify_fn
        self.display_name = endpoint.display_label or endpoint.name

    # ------------------------------------------------------------------
    # Core LLM interface
    # ------------------------------------------------------------------

    def _extra_api_kwargs(self) -> dict[str, Any]:
        """Build extra API kwargs derived from ``endpoint.extra_config``.

        Supported keys (set inside the endpoint's *Extra Config* JSON field):

        * ``disable_thinking`` (bool) — pass ``enable_thinking=False`` to the
          API.  Supported by Qwen3 / LM Studio: prevents the model from spending
          the entire context window on chain-of-thought tokens before generating
          a response.  Drastically reduces latency on models that default to
          extended thinking mode.
        """
        extra = self._endpoint.extra_config or {}
        kwargs: dict[str, Any] = {}
        if extra.get("disable_thinking"):
            kwargs["enable_thinking"] = False
        return kwargs

    def _get_retry_settings(self) -> tuple[int, float]:
        extra = self._endpoint.extra_config or {}
        max_retries = int(extra.get("retry_attempts", 3))
        backoff = float(extra.get("retry_backoff", 0.5))
        return max_retries, backoff

    @staticmethod
    def _is_retryable_exception(exc: Exception) -> bool:
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return True
        msg = str(exc).lower()
        return any(
            token in msg
            for token in (
                "connection",
                "timeout",
                "refused",
                "reset",
                "temporarily unavailable",
                "dns",
                "unreachable",
            )
        )

    async def generate_response(self, messages: list[dict[str, Any]] | Any) -> str:
        """Forward ``messages`` to the external endpoint and return the response text.

        Accepts either a list of OpenAI-style message dicts (e.g. from recon) or a
        SyntH JSON-prompt dict/str — same flexible contract as the built-in engines.
        """
        # Normalise to a messages list, same way openrouter/gemini handle the prompt
        if isinstance(messages, list):
            msg_list = messages
        elif isinstance(messages, dict):
            from core.json_utils import sanitize_for_json

            stripped = {k: v for k, v in messages.items() if k != "__prompt_request"}
            redacted, mm_parts = _extract_attachments_and_redact(stripped)
            safe = sanitize_for_json(redacted)
            prompt_text = json.dumps(safe, ensure_ascii=False)
            if mm_parts:
                content_parts: list[dict[str, Any]] = [
                    {"type": "text", "text": prompt_text}
                ]
                for p in mm_parts:
                    content_parts.append(
                        {
                            "type": "inline_data",
                            "inline_data": {
                                "mime_type": p["mime_type"],
                                "data": p["data"],
                            },
                        }
                    )
                msg_list = [{"role": "user", "content": content_parts}]
                log_debug(
                    f"[cortex_bridge] Extracted {len(mm_parts)} multimodal "
                    f"part(s) from prompt dict"
                )
            else:
                msg_list = [{"role": "user", "content": prompt_text}]
        else:
            msg_list = [{"role": "user", "content": str(messages)}]

        model = self._endpoint.default_model or None
        max_retries, backoff = self._get_retry_settings()
        attempt = 0
        while True:
            attempt += 1
            try:
                chat_resp = await self._adapter.chat_completion(
                    msg_list, model=model, **self._extra_api_kwargs()
                )
                return chat_resp.content
            except Exception as exc:
                should_retry = attempt < max_retries and self._is_retryable_exception(
                    exc
                )
                if should_retry:
                    delay = backoff * (2 ** (attempt - 1))
                    log_warning(
                        f"[cortex_bridge:{self._endpoint.name}] generate_response failed "
                        f"(attempt {attempt}/{max_retries}): {exc}; retrying in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                log_warning(
                    f"[cortex_bridge:{self._endpoint.name}] generate_response failed: {exc}"
                )
                raise

    def _build_messages(self, prompt: Any) -> list[dict[str, Any]]:
        """Convert a SyntH prompt into an OpenAI-style messages list.

        Extracts ``instructions_verbose`` (or ``instructions``) from a SyntH prompt
        dict and places it as a ``system`` role message so the LLM receives explicit
        instructions rather than a single raw JSON blob in the user turn.
        """
        if not isinstance(prompt, dict):
            content: str = prompt if isinstance(prompt, str) else str(prompt)
            return [{"role": "user", "content": content}]

        instructions: str = (
            prompt.get("instructions_verbose") or prompt.get("instructions") or ""
        )
        # Strip keys elevated to system; sanitize the rest (handles non-serializable
        # objects like the PromptRequest dataclass via __dict__ conversion).
        from core.json_utils import sanitize_for_json

        _skip = {"instructions", "instructions_verbose", "__prompt_request"}
        user_dict = {k: v for k, v in prompt.items() if k not in _skip}

        # Extract multimodal attachments before serialising to text so that
        # base64 blobs don't waste context tokens on the text side.
        redacted, mm_parts = _extract_attachments_and_redact(user_dict)
        redacted = sanitize_for_json(redacted)
        user_content = json.dumps(redacted, ensure_ascii=False)

        # Build user message — multipart if we have attachments
        if mm_parts:
            content_parts: list[dict[str, Any]] = [
                {"type": "text", "text": user_content}
            ]
            for p in mm_parts:
                content_parts.append(
                    {
                        "type": "inline_data",
                        "inline_data": {
                            "mime_type": p["mime_type"],
                            "data": p["data"],
                        },
                    }
                )
            log_debug(
                f"[cortex_bridge] _build_messages: extracted {len(mm_parts)} "
                f"multimodal part(s)"
            )
            user_msg_content: Any = content_parts
        else:
            user_msg_content = user_content

        if instructions:
            return [
                {"role": "system", "content": str(instructions)},
                {"role": "user", "content": user_msg_content},
            ]
        return [{"role": "user", "content": user_msg_content}]

    async def handle_incoming_message(
        self, bot: Any, message: Any, prompt: Any
    ) -> str | None:
        """Process a pre-built SyntH JSON prompt and return the LLM response text.

        The message_chain (plugin_instance) drives the full pipeline; this method
        only handles the LLM call — same contract as openrouter/gemini engine.
        Correction prompts are forwarded to the engine like any other prompt;
        the corrector loop is managed entirely by the message chain.
        """
        messages = self._build_messages(prompt)
        return await self.generate_response(messages)

    # NOTE: generate_response already uses _extra_api_kwargs(), so all call
    # paths (Recon via generate_response, main LLM via handle_incoming_message)
    # benefit from extra_config settings such as ``disable_thinking``.

    # ------------------------------------------------------------------
    # Model / capability info
    # ------------------------------------------------------------------

    @property
    def model_limits_map(self) -> dict[str, int]:
        """Return max_chars budget per model — read from extra_config or use a safe default."""
        extra = self._endpoint.extra_config or {}
        max_chars = int(extra.get("max_chars", 100_000))
        return {"default": max_chars}

    def get_supported_models(self) -> list[str]:
        if self._endpoint.available_models:
            return list(self._endpoint.available_models)
        if self._endpoint.default_model:
            return [self._endpoint.default_model]
        return []

    def get_supported_action_types(self) -> list[str]:
        return []

    @staticmethod
    def get_supported_actions() -> dict[str, Any]:
        return {}

    def get_rate_limit(self) -> tuple[int, int, float]:
        # Conservative default; users can adjust via extra_config
        extra = self._endpoint.extra_config or {}
        return (
            int(extra.get("rate_limit_requests", 60)),
            int(extra.get("rate_limit_window", 60)),
            float(extra.get("rate_limit_min_interval", 0.0)),
        )

    # ------------------------------------------------------------------
    # Health check (optional, used by core_initializer)
    # ------------------------------------------------------------------

    def get_health_status(self) -> tuple[bool, str]:
        """Sync wrapper — always returns (True, '') to avoid blocking startup."""
        return True, ""

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def set_current_model(self, model: str) -> None:
        """Set the active model used for completions (called by the WebUI Components tab)."""
        if not model:
            raise ValueError("model name cannot be empty")
        models = self.get_supported_models()
        if models and model not in models:
            raise ValueError(f"Model '{model}' is not in the available list: {models}")
        self._endpoint.default_model = model

    def get_current_model(self) -> str | None:
        """Return the currently active model name."""
        return self._endpoint.default_model

    async def stream_response(self, messages: list[dict[str, Any]]):
        """Yield text chunks from a streaming completion."""
        model = self._endpoint.default_model or None
        try:
            async for chunk in self._adapter.stream_chat_completion(
                messages, model=model
            ):
                yield chunk
        except Exception as exc:
            log_warning(
                f"[cortex_bridge:{self._endpoint.name}] stream_response failed: {exc}"
            )
            raise


# Required by CortexRegistry::load_engine() when loading via module path
PLUGIN_CLASS = ExternalCortexEngine
