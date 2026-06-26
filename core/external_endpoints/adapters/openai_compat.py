# core/external_endpoints/adapters/openai_compat.py
"""Adapter for OpenAI-compatible endpoints.

Works with any service exposing the OpenAI Chat Completions API:
Ollama, LM Studio, OpenRouter, vLLM, Groq, Together AI, Grok/xAI,
the external Selenium LLM Engine, and OpenAI itself.

Uses the ``openai`` SDK (``base_url`` override) so auth headers,
retry logic, and streaming are all handled by the SDK.
"""

from __future__ import annotations

import re
import time as _time
from typing import Any, AsyncIterator
from urllib.parse import urlparse, urlunparse

import httpx
from core.cortex_api_logger import (
    log_cortex_request,
    log_cortex_response,
)
from core.logging_utils import log_debug, log_warning

from core.external_endpoints.adapters.base import (
    BaseProtocolAdapter,
    ChatResponse,
    ModelInfo,
)

# Endpoints that are known to support audio/speech (best-effort heuristic)
_KNOWN_TTS_PATHS = ["/audio/speech", "/v1/audio/speech"]
_KNOWN_STT_PATHS = ["/audio/transcriptions", "/v1/audio/transcriptions"]

# Matches <think>…</think>, <thinking>…</thinking>, and <thought>…</thought>
# blocks produced by reasoning models (Qwen3.5, DeepSeek-R1, etc.) when thinking
# leaks into content despite enable_thinking=False.
_THINKING_RE = re.compile(
    r"<(?:think(?:ing)?|thought)>.*?</(?:think(?:ing)?|thought)>",
    re.DOTALL | re.IGNORECASE,
)
# Some models drop the opening tag and emit a reasoning preamble terminated by a
# lone closing tag (e.g. "reasoning… </thought>{json}"). Strip everything up to
# and including the first such closing tag.
_THINKING_LEADING_CLOSE_RE = re.compile(
    r"^.*?</(?:think(?:ing)?|thought)>\s*", re.DOTALL | re.IGNORECASE
)


def _strip_thinking(text: str) -> str:
    cleaned = _THINKING_RE.sub("", text)
    cleaned = _THINKING_LEADING_CLOSE_RE.sub("", cleaned, count=1)
    return cleaned.strip()


class OpenAICompatAdapter(BaseProtocolAdapter):
    """Adapter for any OpenAI-compatible REST endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or "sk-nokey"
        self._timeout = timeout
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI

                http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(self._timeout or 300.0),
                )
                self._client = AsyncOpenAI(
                    base_url=self._sdk_base_url(),
                    api_key=self._api_key,
                    timeout=self._timeout,
                    http_client=http_client,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "[openai_compat] The 'openai' package is required."
                ) from exc
        return self._client

    def _sdk_base_url(self) -> str:
        """Return base_url normalized to end with /v1 for the OpenAI SDK."""
        url = self._base_url.rstrip("/")
        if not url.endswith("/v1"):
            url = f"{url}/v1"
        return url

    def _http_chat_urls(self) -> list[str]:
        """Return ordered candidate chat URLs for direct HTTP calls."""
        parsed = urlparse(self._base_url)
        base_path = parsed.path.rstrip("/")

        if base_path.endswith("/v1"):
            candidates = [
                f"{base_path}/chat/completions",
                f"{base_path}/chat",
                "/api/v1/chat/completions",
                "/api/v1/chat",
            ]
        else:
            candidates = [
                f"{base_path}/v1/chat/completions",
                f"{base_path}/v1/chat",
                f"{base_path}/chat/completions",
                f"{base_path}/chat",
                f"{base_path}/api/v1/chat/completions",
                f"{base_path}/api/v1/chat",
            ]

        urls: list[str] = []
        seen: set[str] = set()
        for path in candidates:
            url = urlunparse(parsed._replace(path=path))
            if url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    def _http_chat_url(self) -> str:
        """Return the first candidate chat URL for direct HTTP calls."""
        return self._http_chat_urls()[0]

    @staticmethod
    def _extract_tool_call_actions(message: Any) -> str:
        """Normalize OpenAI tool_calls payloads into SyntH action JSON."""
        from core.prompt_renderers import OpenAIRenderer

        content = ""
        tool_calls_raw: Any = None

        if isinstance(message, dict):
            content = _strip_thinking(str(message.get("content") or ""))
            tool_calls_raw = message.get("tool_calls")
        else:
            content = _strip_thinking(str(getattr(message, "content", "") or ""))
            tool_calls_raw = getattr(message, "tool_calls", None)

        tool_calls: list[dict[str, Any]] = []
        for tool_call in tool_calls_raw or []:
            if isinstance(tool_call, dict):
                function = tool_call.get("function") or {}
                tool_calls.append(
                    {
                        "id": tool_call.get("id"),
                        "type": tool_call.get("type") or "function",
                        "function": {
                            "name": function.get("name"),
                            "arguments": function.get("arguments") or "{}",
                        },
                    }
                )
                continue

            function_obj = getattr(tool_call, "function", None)
            if function_obj is None:
                continue
            tool_calls.append(
                {
                    "id": getattr(tool_call, "id", None),
                    "type": getattr(tool_call, "type", "function"),
                    "function": {
                        "name": getattr(function_obj, "name", None),
                        "arguments": getattr(function_obj, "arguments", "{}") or "{}",
                    },
                }
            )

        parsed = OpenAIRenderer.parse_tool_call_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": content,
                            "tool_calls": tool_calls,
                        }
                    }
                ]
            }
        )
        return str(parsed or "").strip()

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
        client = self._get_client()
        request_model = model or "default"
        engine_tag = f"openai_compat:{self._engine_label or 'default'}"

        # Pull out vendor-extension keys that need to travel via ``extra_body``
        # (the OpenAI SDK rejects unknown root-level kwargs, but accepts extra_body).
        # ``enable_thinking`` — Qwen3.5 / LM Studio: disables chain-of-thought so
        # reasoning tokens don't fill the context window before the response, and
        # don't bleed into message content where they corrupt action parsing.
        # Defaulting to False here matches the behaviour of the vision path and of
        # Gemini/OpenRouter which never expose thinking in the response content.
        extra_body: dict[str, Any] = kwargs.pop("extra_body", {}) or {}
        if "enable_thinking" in kwargs:
            extra_body["enable_thinking"] = kwargs.pop("enable_thinking")

        filtered = {
            k: v for k, v in kwargs.items() if k not in ("model", "messages", "stream")
        }
        logged_payload: dict[str, Any] = {"messages": messages}
        logged_payload.update(filtered)
        if extra_body:
            logged_payload["extra_body"] = extra_body

        log_cortex_request(
            engine_tag,
            model=request_model,
            url=self._sdk_base_url(),
            payload=logged_payload,
        )
        _req_start = _time.monotonic()

        try:
            response = await client.chat.completions.create(
                model=request_model,
                messages=messages,
                stream=False,
                extra_body=extra_body or None,
                **filtered,
            )
            choice = response.choices[0]
            usage = {}
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens or 0,
                    "completion_tokens": response.usage.completion_tokens or 0,
                    "total_tokens": response.usage.total_tokens or 0,
                }
            # Prefer native tool_calls over message content — when a reasoning
            # model emits chain-of-thought before calling a function, its thinking
            # text ends up in `content` while the actual call is in `tool_calls`.
            # Checking tool_calls first avoids returning thinking text as the
            # response and matches the behaviour of Gemini / OpenRouter engines.
            _raw_tool_calls = (
                choice.message.get("tool_calls")
                if isinstance(choice.message, dict)
                else getattr(choice.message, "tool_calls", None)
            )
            if _raw_tool_calls:
                content = self._extract_tool_call_actions(choice.message)
            else:
                content = _strip_thinking(self._extract_message_content(choice.message))
            finish_reason = choice.finish_reason or "stop"
            if finish_reason == "tool_calls":
                finish_reason = "tool_call"

            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model=response.model or request_model,
                status=200,
                body=content,
                usage=usage or None,
                elapsed_ms=_elapsed,
            )
            return ChatResponse(
                content=content,
                model=response.model or request_model,
                finish_reason=finish_reason,
                usage=usage,
            )
        except Exception as exc:
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model=request_model,
                error=str(exc),
                elapsed_ms=_elapsed,
            )
            raise

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        client = self._get_client()
        request_model = model or "default"
        engine_tag = f"openai_compat:{self._engine_label or 'default'}"
        filtered = {
            k: v for k, v in kwargs.items() if k not in ("model", "messages", "stream")
        }
        logged_payload: dict[str, Any] = {"messages": messages, "stream": True}
        logged_payload.update(filtered)

        log_cortex_request(
            engine_tag,
            model=request_model,
            url=self._sdk_base_url(),
            payload=logged_payload,
        )
        _req_start = _time.monotonic()
        _accumulated: list[str] = []

        try:
            stream = await client.chat.completions.create(
                model=request_model,
                messages=messages,
                stream=True,
                **filtered,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue
                content = self._extract_message_content(delta)
                if not content:
                    continue
                _accumulated.append(content)
                yield content
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model=request_model,
                body="".join(_accumulated),
                elapsed_ms=_elapsed,
            )
        except Exception as exc:
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

    def _normalize_capabilities(self, capabilities: Any) -> dict[str, bool]:
        if isinstance(capabilities, dict):
            return {
                str(key).lower(): bool(value)
                for key, value in capabilities.items()
                if isinstance(key, (str, int, float))
            }
        if isinstance(capabilities, (list, tuple, set)):
            return {
                str(item).lower(): True
                for item in capabilities
                if isinstance(item, (str, int, float))
            }
        if isinstance(capabilities, (str, int, float)):
            return {str(capabilities).lower(): True}
        return {}

    def _parse_model_entry(self, entry: Any) -> ModelInfo:
        # Some OpenAI-compatible endpoints return dict-like entries, others
        # return SDK model objects. Support both.
        if isinstance(entry, dict):
            entry_id = str(entry.get("id", ""))
            capabilities = self._normalize_capabilities(entry.get("capabilities", {}))
            return ModelInfo(
                id=entry_id,
                name=str(entry.get("name", entry_id)),
                owned_by=str(entry.get("owned_by", "")),
                capabilities=capabilities,
            )
        entry_id = getattr(entry, "id", "") or ""
        capabilities = self._normalize_capabilities(getattr(entry, "capabilities", {}))
        return ModelInfo(
            id=str(entry_id),
            name=str(getattr(entry, "name", entry_id) or entry_id),
            owned_by=str(getattr(entry, "owned_by", "") or ""),
            capabilities=capabilities,
        )

    def _supports_vision_capability(self, model: ModelInfo) -> bool:
        if not model.capabilities:
            return False
        keys = {key.lower() for key in model.capabilities.keys()}
        if any(
            keyword in keys
            for keyword in ("vision", "image", "images", "multimodal", "visual")
        ):
            return True
        if model.capabilities.get("vision") or model.capabilities.get("image"):
            return True
        return False

    async def _resolve_probe_model(
        self,
        *,
        models: list[ModelInfo] | None = None,
        prefer_vision: bool = False,
    ) -> str | None:
        model_infos = models
        if model_infos is None:
            try:
                model_infos = await self.list_models()
            except Exception:
                return None

        if prefer_vision:
            for model in model_infos:
                if model.id and self._supports_vision_capability(model):
                    return model.id

        for model in model_infos:
            if model.id:
                return model.id

        return None

    def _resolve_http_url(self, path: str) -> str:
        parsed = urlparse(self._base_url)
        base_path = parsed.path.rstrip("/")
        joined_path = (
            f"{base_path}/{path.lstrip('/')}" if base_path else f"/{path.lstrip('/')}"
        )
        return urlunparse(parsed._replace(path=joined_path))

    @staticmethod
    def _extract_message_content(message: Any) -> str:
        if isinstance(message, dict):
            content = message.get("content", "") or ""
            if content:
                return str(content)
            return str(message.get("reasoning_content", "") or "")

        content = getattr(message, "content", None)
        if content:
            return str(content)
        return str(getattr(message, "reasoning_content", "") or "")

    def _http_model_paths(self) -> list[str]:
        parsed = urlparse(self._base_url)
        if parsed.path.rstrip("/").endswith("/v1"):
            return ["/models"]
        return ["/v1/models", "/models"]

    def _http_tts_paths(self) -> list[str]:
        parsed = urlparse(self._base_url)
        if parsed.path.rstrip("/").endswith("/v1"):
            return ["/audio/speech"]
        return ["/v1/audio/speech", "/audio/speech"]

    def _http_stt_paths(self) -> list[str]:
        parsed = urlparse(self._base_url)
        if parsed.path.rstrip("/").endswith("/v1"):
            return ["/audio/transcriptions"]
        return ["/v1/audio/transcriptions", "/audio/transcriptions"]

    async def _list_models_via_http(self) -> list[ModelInfo]:
        import aiohttp

        effective_timeout = float(self._timeout or 300.0)

        def _parse_list(data: Any) -> list[ModelInfo]:
            if data is None:
                return []
            parsed = []
            for m in data:
                try:
                    parsed_model = self._parse_model_entry(m)
                    if parsed_model.id:
                        parsed.append(parsed_model)
                except Exception as exc:
                    log_warning(f"[openai_compat] skipping invalid model entry: {exc}")
            return parsed

        for path in self._http_model_paths():
            url = self._resolve_http_url(path)
            log_debug(f"[openai_compat] trying HTTP GET {url}")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        timeout=aiohttp.ClientTimeout(
                            total=effective_timeout,
                            connect=effective_timeout,
                            sock_connect=effective_timeout,
                            sock_read=effective_timeout,
                        ),
                    ) as resp:
                        if resp.status != 200:
                            log_warning(
                                f"[openai_compat] GET {url} returned HTTP {resp.status}"
                            )
                            continue
                        response_data = await resp.json()
                        if isinstance(response_data, dict):
                            response_data = response_data.get("data", [])
                        if isinstance(response_data, list):
                            return _parse_list(response_data)
                        return []
            except Exception as exc:
                log_warning(
                    f"[openai_compat] list_models HTTP fallback failed (url={url}): {repr(exc)}"
                )
        return []

    async def list_models(self) -> list[ModelInfo]:
        return await self._list_models_via_http()

    # ------------------------------------------------------------------
    # TTS
    # ------------------------------------------------------------------

    async def generate_tts(
        self,
        text: str,
        voice: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        """Try to generate TTS via the /audio/speech endpoint.

        Returns ``None`` if the endpoint does not support it.
        """
        import aiohttp

        payload = {
            "model": kwargs.get("model", "tts-1"),
            "input": text,
            "voice": voice or "alloy",
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        for path in self._http_tts_paths():
            url = self._resolve_http_url(path)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status == 200:
                            return await resp.read()
                        log_debug(
                            f"[openai_compat] TTS {url} returned {resp.status} – not supported"
                        )
            except Exception as exc:
                log_debug(f"[openai_compat] TTS request failed ({url}): {exc}")
        return None

    # ------------------------------------------------------------------
    # STT
    # ------------------------------------------------------------------

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        mime_type: str | None = None,
        **kwargs: Any,
    ) -> str | None:
        """Try transcription via the /audio/transcriptions endpoint.

        Returns ``None`` if the endpoint does not support it.
        """
        import aiohttp

        engine_tag = f"openai_compat:{self._engine_label or 'default'}"
        stt_model = kwargs.get("model", "whisper-1")
        headers = {"Authorization": f"Bearer {self._api_key}"}
        ext = "wav"
        if mime_type:
            ext = mime_type.split("/")[-1].split(";")[0].strip() or "wav"

        data = aiohttp.FormData()
        data.add_field(
            "file",
            audio_bytes,
            filename=f"audio.{ext}",
            content_type=mime_type or "audio/wav",
        )
        data.add_field("model", stt_model)

        log_cortex_request(
            engine_tag,
            model=stt_model,
            payload={
                "task": "transcribe_audio",
                "mime_type": mime_type or "audio/wav",
                "audio_size": f"{len(audio_bytes)} bytes",
            },
        )
        _req_start = _time.monotonic()

        for path in self._http_stt_paths():
            url = self._resolve_http_url(path)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        data=data,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            text = result.get("text", "")
                            _elapsed = (_time.monotonic() - _req_start) * 1000
                            log_cortex_response(
                                engine_tag,
                                model=stt_model,
                                status=200,
                                body=text,
                                elapsed_ms=_elapsed,
                            )
                            return text
                        log_debug(
                            f"[openai_compat] STT {url} returned {resp.status} – not supported"
                        )
            except Exception as exc:
                log_debug(f"[openai_compat] STT request failed ({url}): {exc}")
        _elapsed = (_time.monotonic() - _req_start) * 1000
        log_cortex_response(
            engine_tag,
            model=stt_model,
            error="No STT endpoint responded successfully",
            elapsed_ms=_elapsed,
        )
        return None

    # ------------------------------------------------------------------
    # Vision (Iris) – OpenAI vision message format
    # ------------------------------------------------------------------

    async def describe_image(
        self,
        image_bytes: bytes,
        mime_type: str | None = None,
        prompt: str | None = None,
        **kwargs: Any,
    ) -> str | None:
        """Describe *image_bytes* using the OpenAI vision message format.

        Sends a chat completion request with the image embedded as a base64
        ``image_url`` content part, compatible with GPT-4o, LLaVA, Qwen-VL
        and other vision-capable OpenAI-compatible endpoints.

        Returns ``None`` if the request fails or the endpoint does not support
        vision.
        """
        import base64

        import aiohttp

        engine_tag = f"openai_compat:{self._engine_label or 'default'}"
        effective_mime = mime_type or "image/jpeg"
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{effective_mime};base64,{b64}"
        effective_prompt = prompt or "Describe this image in detail."
        vision_model = kwargs.get("model", "default")
        vision_timeout = float(kwargs.pop("vision_timeout", self._timeout or 600.0))

        # Disable chain-of-thought thinking for vision calls unless explicitly
        # requested. Some OpenAI-compatible providers spend the whole budget on
        # reasoning tokens for image tasks, which hurts latency and reliability.
        if "enable_thinking" not in kwargs:
            kwargs["enable_thinking"] = False

        # Gemma 4 (and most vision-capable models) attend better when the image
        # comes before the text prompt rather than after it.
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                    {"type": "text", "text": effective_prompt},
                ],
            }
        ]

        payload: dict[str, Any] = {
            "model": vision_model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", 1024),
            "stream": False,
        }
        if "enable_thinking" in kwargs:
            payload["enable_thinking"] = kwargs["enable_thinking"]

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        # Vision calls use only /chat/completions endpoints — generic /chat
        # fallbacks (e.g. /v1/chat) are non-standard and can return empty content.
        vision_urls = [u for u in self._http_chat_urls() if u.endswith("completions")]
        if not vision_urls:
            vision_urls = self._http_chat_urls()

        log_cortex_request(
            engine_tag,
            model=vision_model,
            url=vision_urls[0] if vision_urls else "",
            payload={
                "task": "describe_image",
                "mime_type": effective_mime,
                "image_size": f"{len(image_bytes)} bytes",
                "prompt": effective_prompt,
            },
        )
        _req_start = _time.monotonic()
        last_error = "No vision endpoint responded successfully"

        try:
            async with aiohttp.ClientSession() as session:
                for chat_url in vision_urls:
                    try:
                        async with session.post(
                            chat_url,
                            json=payload,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=vision_timeout),
                        ) as resp:
                            if resp.status != 200:
                                body = await resp.text()
                                last_error = f"HTTP {resp.status}: {body[:200]}"
                                continue

                            result = await resp.json()
                            message = result.get("choices", [{}])[0].get("message", {})
                            description = self._extract_message_content(message) or None
                            _elapsed = (_time.monotonic() - _req_start) * 1000
                            log_cortex_response(
                                engine_tag,
                                model=vision_model,
                                status=200,
                                body=description,
                                elapsed_ms=_elapsed,
                            )
                            return description
                    except Exception as exc:
                        last_error = str(exc)
                        continue
        except Exception as exc:
            last_error = str(exc)

        _elapsed = (_time.monotonic() - _req_start) * 1000
        log_cortex_response(
            engine_tag,
            model=vision_model,
            error=last_error,
            elapsed_ms=_elapsed,
        )
        if last_error and last_error != "No vision endpoint responded successfully":
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_warning(f"[openai_compat] describe_image failed: {last_error}")
        return None

    async def _probe_vision_support(
        self,
        *,
        model: str | None = None,
        models: list[ModelInfo] | None = None,
    ) -> bool:
        import base64

        import aiohttp

        # A lightweight standard OpenAI-style image probe. If the endpoint supports
        # vision in the chat completion path, this request should succeed.
        tiny_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
            b"\x00\x0cIDAT\x08\xdbc\xf8\x0f\x00\x01\x05\x01\x02\x9a\x9b"
            b"\x0c\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        data_url = f"data:image/png;base64,{base64.b64encode(tiny_png).decode('ascii')}"
        prompt = "Describe this image in detail."
        payload = {
            "model": model
            or await self._resolve_probe_model(models=models, prefer_vision=True)
            or "default",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": 10,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession() as session:
                for chat_url in self._http_chat_urls():
                    try:
                        async with session.post(
                            chat_url,
                            json=payload,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            if resp.status == 200:
                                return True
                    except Exception:
                        continue
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Probe / health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        import aiohttp

        for path in self._http_model_paths():
            url = self._resolve_http_url(path)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status < 500:
                            return True
            except Exception:
                continue
        return False

    async def ping_test(
        self,
        model: str | None = None,
        timeout: float | None = None,
    ) -> tuple[bool, str]:
        """Send a minimal chat 'ping' to verify cortex connectivity.

        Posts a single ``ping`` user message directly via aiohttp (no SDK
        auth flow, no SyntH prompt).  Returns ``(True, reply_text)`` on
        success, ``(False, error_str)`` on failure.

        Two-phase timeout strategy:
                - TCP connect uses the effective probe timeout (explicit ``timeout``
                    when provided, otherwise the adapter's configured timeout).
                - Body read uses the same effective timeout (soft): if the server already
          returned HTTP 200 but the model is still generating (e.g. thinking/
          reasoning models like Qwen3.5), a body-read timeout is treated as a
          *reachability success* — ``(True, '')`` — rather than a failure.
          This prevents slow models from being silently excluded from the
          cortex registry after a probe.
        """
        import aiohttp

        effective_timeout = float(
            timeout if timeout is not None else (self._timeout or 30.0)
        )
        request_model = model or await self._resolve_probe_model() or "default"
        payload: dict[str, Any] = {
            "model": request_model,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "max_tokens": 16,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last_err = ""
        try:
            async with aiohttp.ClientSession() as session:
                for chat_url in self._http_chat_urls():
                    log_debug(
                        f"[openai_compat] ping_test → POST {chat_url} model={payload['model']}"
                    )
                    try:
                        async with session.post(
                            chat_url,
                            json=payload,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(
                                total=effective_timeout,
                                connect=effective_timeout,
                                sock_connect=effective_timeout,
                                sock_read=effective_timeout,
                            ),
                        ) as resp:
                            if resp.status >= 400:
                                body = await resp.text()
                                last_err = f"HTTP {resp.status}: {body[:200]}"
                                log_debug(
                                    f"[openai_compat] ping_test {chat_url} failed: {last_err}"
                                )
                                # A structured 503 (JSON body) means the endpoint is
                                # reachable and correctly parsed the request — the model
                                # is just temporarily at capacity.  Treat as success.
                                if resp.status == 503 and body.lstrip().startswith("{"):
                                    log_debug(
                                        f"[openai_compat] ping_test {chat_url}: "
                                        f"structured 503 — endpoint reachable, model at capacity"
                                    )
                                    return True, ""
                                continue
                            try:
                                data = await resp.json()
                                message = data.get("choices", [{}])[0].get(
                                    "message", {}
                                )
                                reply = self._extract_message_content(message)
                                log_debug(
                                    f"[openai_compat] ping_test OK — reply: {reply!r}"
                                )
                                return True, reply
                            except Exception as body_exc:
                                log_warning(
                                    f"[openai_compat] ping_test: HTTP 200 but body read timed out "
                                    f"(slow/thinking model?) — treating as reachable. "
                                    f"detail: {body_exc}"
                                )
                                return True, ""
                    except Exception as exc:
                        last_err = repr(exc)
                        continue
        except Exception as exc:
            last_err = repr(exc)

        err = last_err or "No reachable chat endpoint"
        log_warning(f"[openai_compat] ping_test failed: {err}")
        return False, err

    async def probe_capabilities(self) -> dict[str, bool]:
        """Detect Vox / Auris / vision support.

        Note: ``cortex`` is intentionally left ``False`` here.  The caller
        (``probe_endpoint``) sets it based on the result of
        ``adapter.ping_test()``, which provides a more reliable signal than
        the ``/models`` listing.
        """
        capabilities: dict[str, bool] = {
            "cortex": False,
            "vox": False,
            "auris": False,
            "live": False,
            "vision": False,
        }

        import aiohttp

        # --- Vision: read declared model capability metadata first ---
        models: list[ModelInfo] = []
        try:
            models = await self.list_models()
        except Exception:
            models = []

        for m in models:
            if self._supports_vision_capability(m):
                capabilities["vision"] = True
                break

        if not capabilities["vision"]:
            probed_model_ids: set[str] = set()
            _vision_probe_limit = 10
            for m in models:
                if len(probed_model_ids) >= _vision_probe_limit:
                    break
                if not m.id or m.id in probed_model_ids:
                    continue
                probed_model_ids.add(m.id)
                try:
                    if await self._probe_vision_support(model=m.id):
                        capabilities["vision"] = True
                        break
                except Exception:
                    continue

        if not capabilities["vision"] and not models:
            try:
                capabilities["vision"] = await self._probe_vision_support()
            except Exception:
                pass

        # --- Vox: probe /audio/speech with a tiny payload ---
        for path in self._http_tts_paths():
            url = self._resolve_http_url(path)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        json={"model": "tts-1", "input": "test", "voice": "alloy"},
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        capabilities["vox"] = resp.status == 200
                        if resp.status == 200:
                            break
            except Exception:
                continue

        # --- Auris: probe /audio/transcriptions with empty form ---
        try:
            import io

            data = aiohttp.FormData()
            data.add_field(
                "file",
                io.BytesIO(b""),
                filename="probe.wav",
                content_type="audio/wav",
            )
            data.add_field("model", "whisper-1")
            for path in self._http_stt_paths():
                url = self._resolve_http_url(path)
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            url,
                            data=data,
                            headers={"Authorization": f"Bearer {self._api_key}"},
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            # Any non-404 status means the path exists (empty file may return 400)
                            capabilities["auris"] = resp.status != 404
                            if capabilities["auris"]:
                                break
                except Exception:
                    continue
        except Exception:
            pass

        return capabilities
