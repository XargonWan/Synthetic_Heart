# core/external_endpoints/bridges/cortex_bridge.py
"""Cortex (LLM) bridge for external endpoints.

Wraps a :class:`BaseProtocolAdapter` in the ``AIPluginBase`` interface so
that any external endpoint registered as a Cortex engine behaves identically
to a built-in engine from the perspective of the SyntH core.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from core.ai_plugin_base import AIPluginBase
from core.logging_utils import log_warning

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

    async def generate_response(self, messages: list[dict[str, Any]] | Any) -> str:
        """Forward ``messages`` to the external endpoint and return the response text.

        Accepts either a list of OpenAI-style message dicts (e.g. from recon) or a
        SyntH JSON-prompt dict/str — same flexible contract as the built-in engines.
        """
        # Normalise to a messages list, same way openrouter/gemini handle the prompt
        if isinstance(messages, list):
            msg_list = messages
        elif isinstance(messages, dict):
            prompt_text = json.dumps(messages, ensure_ascii=False)
            msg_list = [{"role": "user", "content": prompt_text}]
        else:
            msg_list = [{"role": "user", "content": str(messages)}]

        model = self._endpoint.default_model or None
        try:
            chat_resp = await self._adapter.chat_completion(
                msg_list, model=model, **self._extra_api_kwargs()
            )
            return chat_resp.content
        except Exception as exc:
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
        user_content = json.dumps(user_dict, ensure_ascii=False)

        if instructions:
            return [
                {"role": "system", "content": str(instructions)},
                {"role": "user", "content": user_content},
            ]
        return [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]

    async def handle_incoming_message(
        self, bot: Any, message: Any, prompt: Any
    ) -> str | None:
        """Process a pre-built SyntH JSON prompt and return the LLM response text.

        The message_chain (plugin_instance) drives the full pipeline; this method
        only handles the LLM call — same contract as openrouter/gemini engine.
        Correction prompts are forwarded to the engine like any other prompt;
        the corrector loop is managed entirely by the message chain.
        """
        try:
            messages = self._build_messages(prompt)
            return await self.generate_response(messages)
        except Exception as exc:
            log_warning(
                f"[cortex_bridge:{self._endpoint.name}] handle_incoming_message failed: {exc}"
            )
            return None

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
