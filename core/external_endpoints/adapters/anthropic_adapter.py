# core/external_endpoints/adapters/anthropic_adapter.py
"""Adapter for Anthropic Claude endpoints (Messages API)."""

from __future__ import annotations

import time as _time
from typing import Any

import aiohttp

from core.cortex_api_logger import (
    log_cortex_request,
    log_cortex_response,
    sanitize_for_log,
)
from core.logging_utils import log_debug

from core.external_endpoints.adapters.base import (
    BaseProtocolAdapter,
    ChatResponse,
    ModelInfo,
)

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"

# Well-known Claude models (fallback when /v1/models is not available)
_KNOWN_MODELS = [
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
]


def _openai_to_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert OpenAI-style messages to Anthropic Messages API format."""
    system_parts: list[str] = []
    anthropic_messages: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            continue
        anthropic_role = "assistant" if role == "assistant" else "user"
        if isinstance(content, str):
            anthropic_messages.append({"role": anthropic_role, "content": content})
        elif isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            anthropic_messages.append(
                {"role": anthropic_role, "content": "\n".join(text_parts)}
            )
    return "\n".join(system_parts), anthropic_messages


class AnthropicAdapter(BaseProtocolAdapter):
    """Adapter for the Anthropic Claude Messages API."""

    DEFAULT_MODEL = "claude-haiku-4-5"

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatResponse:
        request_model = model or self.DEFAULT_MODEL
        system_instruction, anthropic_messages = _openai_to_anthropic(messages)
        engine_tag = f"anthropic:{self._engine_label or 'default'}"

        payload: dict[str, Any] = {
            "model": request_model,
            "messages": anthropic_messages,
            "max_tokens": kwargs.get("max_tokens", 4096),
        }
        if system_instruction:
            payload["system"] = system_instruction

        log_cortex_request(
            engine_tag,
            model=request_model,
            url=f"{self._base_url}/v1/messages",
            payload=sanitize_for_log(payload),
        )
        _req_start = _time.monotonic()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/v1/messages",
                    json=payload,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        error_msg = data.get("error", {}).get("message", str(data))
                        _elapsed = (_time.monotonic() - _req_start) * 1000
                        log_cortex_response(
                            engine_tag,
                            model=request_model,
                            status=resp.status,
                            error=error_msg,
                            elapsed_ms=_elapsed,
                        )
                        raise RuntimeError(
                            f"[anthropic_adapter] API error {resp.status}: {error_msg}"
                        )

                    content_blocks = data.get("content", [])
                    content_text = "".join(
                        block.get("text", "")
                        for block in content_blocks
                        if block.get("type") == "text"
                    )
                    usage_raw = data.get("usage", {})
                    usage = {
                        "prompt_tokens": usage_raw.get("input_tokens", 0),
                        "completion_tokens": usage_raw.get("output_tokens", 0),
                        "total_tokens": (
                            usage_raw.get("input_tokens", 0)
                            + usage_raw.get("output_tokens", 0)
                        ),
                    }
                    _elapsed = (_time.monotonic() - _req_start) * 1000
                    log_cortex_response(
                        engine_tag,
                        model=data.get("model", request_model),
                        status=200,
                        body=content_text,
                        usage=usage,
                        elapsed_ms=_elapsed,
                    )
                    return ChatResponse(
                        content=content_text,
                        model=data.get("model", request_model),
                        finish_reason=data.get("stop_reason", "stop"),
                        usage=usage,
                    )
        except Exception as exc:
            if not isinstance(exc, RuntimeError):
                _elapsed = (_time.monotonic() - _req_start) * 1000
                log_cortex_response(
                    engine_tag,
                    model=request_model,
                    error=str(exc),
                    elapsed_ms=_elapsed,
                )
            raise

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    async def list_models(self) -> list[ModelInfo]:
        """Fetch models from /v1/models (newer API) or return known list."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/v1/models",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        entries = data.get("data", [])
                        if entries:
                            return [
                                ModelInfo(
                                    id=m.get("id", ""),
                                    name=m.get("display_name", m.get("id", "")),
                                    owned_by="anthropic",
                                )
                                for m in entries
                                if m.get("id")
                            ]
        except Exception as exc:
            log_debug(f"[anthropic_adapter] list_models HTTP failed: {exc}")

        # Fallback – static list
        return [ModelInfo(id=m, name=m, owned_by="anthropic") for m in _KNOWN_MODELS]

    # ------------------------------------------------------------------
    # Probe / health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Verify the API key is valid by calling /v1/models."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/v1/models",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status in (200, 404)  # 404 = path missing but key OK
        except Exception:
            return False

    async def probe_capabilities(self) -> dict[str, bool]:
        alive = await self.health_check()
        return {
            "cortex": alive,
            "vox": False,
            "auris": False,
            "live": False,
            "vision": alive,  # Claude 3 family supports vision
        }

    # ------------------------------------------------------------------
    # TTS / STT – not supported
    # ------------------------------------------------------------------

    async def generate_tts(
        self, text: str, voice: str | None = None, **kwargs: Any
    ) -> bytes | None:
        return None

    async def transcribe_audio(
        self, audio_bytes: bytes, mime_type: str | None = None, **kwargs: Any
    ) -> str | None:
        return None

    # ------------------------------------------------------------------
    # Vision (Iris) – Anthropic Messages API image format
    # ------------------------------------------------------------------

    async def describe_image(
        self,
        image_bytes: bytes,
        mime_type: str | None = None,
        prompt: str | None = None,
        **kwargs: Any,
    ) -> str | None:
        """Describe *image_bytes* using the Anthropic Messages API image format.

        Sends a message with an ``image`` content block containing the image as
        base64.  Supported MIME types: ``image/jpeg``, ``image/png``,
        ``image/gif``, ``image/webp``.

        Returns ``None`` if the request fails or the MIME type is unsupported.
        """
        import base64

        _SUPPORTED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}
        effective_mime = mime_type or "image/jpeg"
        if effective_mime not in _SUPPORTED_MIME:
            return None

        effective_prompt = prompt or "Describe this image in detail."
        b64 = base64.b64encode(image_bytes).decode("ascii")

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": effective_mime,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": effective_prompt},
                ],
            }
        ]
        request_model = kwargs.get("model", self.DEFAULT_MODEL)
        payload: dict[str, Any] = {
            "model": request_model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", 1024),
        }
        engine_tag = f"anthropic:{self._engine_label or 'default'}"

        log_cortex_request(
            engine_tag,
            model=request_model,
            url=f"{self._base_url}/v1/messages",
            payload={
                "task": "describe_image",
                "mime_type": effective_mime,
                "image_size": f"{len(image_bytes)} bytes",
                "prompt": effective_prompt,
            },
        )
        _req_start = _time.monotonic()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/v1/messages",
                    json=payload,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        _elapsed = (_time.monotonic() - _req_start) * 1000
                        log_cortex_response(
                            engine_tag,
                            model=request_model,
                            status=resp.status,
                            error=f"HTTP {resp.status}",
                            elapsed_ms=_elapsed,
                        )
                        return None
                    content_blocks = data.get("content", [])
                    description = (
                        "".join(
                            block.get("text", "")
                            for block in content_blocks
                            if block.get("type") == "text"
                        )
                        or None
                    )
                    _elapsed = (_time.monotonic() - _req_start) * 1000
                    log_cortex_response(
                        engine_tag,
                        model=request_model,
                        status=200,
                        body=description,
                        elapsed_ms=_elapsed,
                    )
                    return description
        except Exception as exc:
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model=request_model,
                error=str(exc),
                elapsed_ms=_elapsed,
            )
            log_debug(f"[anthropic_adapter] describe_image failed: {exc}")
            return None
