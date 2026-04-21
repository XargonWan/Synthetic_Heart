# core/external_endpoints/bridges/cortex_bridge.py
"""Cortex (LLM) bridge for external endpoints.

Wraps a :class:`BaseProtocolAdapter` in the ``AIPluginBase`` interface so
that any external endpoint registered as a Cortex engine behaves identically
to a built-in engine from the perspective of the SyntH core.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.ai_plugin_base import AIPluginBase
from core.logging_utils import log_warning
from core.external_endpoints.models import EndpointProtocol

if TYPE_CHECKING:
    from core.external_endpoints.adapters.base import BaseProtocolAdapter
    from core.external_endpoints.models import ExternalEndpoint


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
        else:
            msg_list = self._build_messages(messages)

        model = self._endpoint.default_model
        if not model and self._endpoint.available_models:
            model = self._endpoint.available_models[0]
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
        user_dict = {
            k: v
            for k, v in prompt.items()
            if k not in ("instructions", "instructions_verbose")
        }
        redacted_user_dict = self._copy_and_redact_data(user_dict)
        user_content: str | list[dict[str, Any]] = json.dumps(
            redacted_user_dict, ensure_ascii=False
        )

        if self._endpoint.protocol == EndpointProtocol.OPENAI:
            multimodal_parts = self._extract_multimodal_parts(user_dict)
            if multimodal_parts:
                user_content = [
                    {
                        "type": "text",
                        "text": json.dumps(redacted_user_dict, ensure_ascii=False),
                    },
                    *multimodal_parts,
                ]

        if instructions:
            return [
                {"role": "system", "content": str(instructions)},
                {"role": "user", "content": user_content},
            ]
        return [{"role": "user", "content": user_content}]

    def _extract_multimodal_parts(self, prompt: Any) -> list[dict[str, Any]]:
        """Extract OpenAI-style multimodal content parts from a SyntH prompt."""
        parts: list[dict[str, Any]] = []

        if isinstance(prompt, str):
            try:
                prompt = json.loads(prompt)
            except (json.JSONDecodeError, ValueError):
                return parts

        if not isinstance(prompt, dict):
            return parts

        multimodal_keys = {"attachments", "images", "videos"}
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

            mime_type = str(att.get("mime_type") or att.get("mimeType") or "")
            file_path = str(att.get("path") or att.get("file_path") or "")

            if file_path and not mime_type:
                import mimetypes as mt

                mime_type, _ = mt.guess_type(file_path)
                mime_type = mime_type or "application/octet-stream"

            if not mime_type.startswith("image/"):
                continue

            b64_data = att.get("data") or att.get("base64", "")
            if not b64_data and file_path:
                try:
                    path = Path(file_path)
                    if path.exists() and path.is_file():
                        b64_data = base64.b64encode(path.read_bytes()).decode("utf-8")
                except Exception:
                    continue

            if not b64_data:
                continue

            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{b64_data}"},
                }
            )

        return parts

    def _copy_and_redact_data(self, prompt: dict[str, Any]) -> dict[str, Any]:
        """Deep copy a prompt and remove heavy base64 attachment payloads."""
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
            for field in data_fields:
                if field in item:
                    item[field] = f"<redacted: {len(str(item[field]))} chars>"

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
