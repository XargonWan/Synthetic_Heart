# core/external_endpoints/adapters/openai_compat.py
"""Adapter for OpenAI-compatible endpoints.

Works with any service exposing the OpenAI Chat Completions API:
Ollama, LM Studio, OpenRouter, vLLM, Groq, Together AI, Grok/xAI,
the external Selenium LLM Engine, and OpenAI itself.

Uses the ``openai`` SDK (``base_url`` override) so auth headers,
retry logic, and streaming are all handled by the SDK.
"""

from __future__ import annotations

from typing import Any, AsyncIterator
from urllib.parse import urlparse, urlunparse

from core.logging_utils import log_debug, log_warning

from core.external_endpoints.adapters.base import (
    BaseProtocolAdapter,
    ChatResponse,
    ModelInfo,
)

# Endpoints that are known to support audio/speech (best-effort heuristic)
_KNOWN_TTS_PATHS = ["/audio/speech", "/v1/audio/speech"]
_KNOWN_STT_PATHS = ["/audio/transcriptions", "/v1/audio/transcriptions"]


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

                self._client = AsyncOpenAI(
                    base_url=self._base_url,
                    api_key=self._api_key,
                    timeout=self._timeout,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "[openai_compat] The 'openai' package is required."
                ) from exc
        return self._client

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

        try:
            response = await client.chat.completions.create(
                model=request_model,
                messages=messages,
                stream=False,
                **{
                    k: v
                    for k, v in kwargs.items()
                    if k not in ("model", "messages", "stream")
                },
            )
            choice = response.choices[0]
            usage = {}
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens or 0,
                    "completion_tokens": response.usage.completion_tokens or 0,
                    "total_tokens": response.usage.total_tokens or 0,
                }
            return ChatResponse(
                content=choice.message.content or "",
                model=response.model or request_model,
                finish_reason=choice.finish_reason or "stop",
                usage=usage,
            )
        except Exception:
            raise

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        client = self._get_client()
        request_model = model or "default"

        try:
            stream = await client.chat.completions.create(
                model=request_model,
                messages=messages,
                stream=True,
                **{
                    k: v
                    for k, v in kwargs.items()
                    if k not in ("model", "messages", "stream")
                },
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield delta.content
        except Exception:
            raise

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    def _parse_model_entry(self, entry: Any) -> ModelInfo:
        # Some OpenAI-compatible endpoints return dict-like entries, others
        # return SDK model objects. Support both.
        if isinstance(entry, dict):
            entry_id = str(entry.get("id", ""))
            return ModelInfo(
                id=entry_id,
                name=str(entry.get("name", entry_id)),
                owned_by=str(entry.get("owned_by", "")),
            )
        entry_id = getattr(entry, "id", "") or ""
        return ModelInfo(
            id=str(entry_id),
            name=str(getattr(entry, "name", entry_id) or entry_id),
            owned_by=str(getattr(entry, "owned_by", "") or ""),
        )

    def _resolve_http_url(self, path: str) -> str:
        parsed = urlparse(self._base_url)
        base_path = parsed.path.rstrip("/")
        joined_path = f"{base_path}/{path.lstrip('/')}" if base_path else f"/{path.lstrip('/')}"
        return urlunparse(parsed._replace(path=joined_path))

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
                        timeout=aiohttp.ClientTimeout(total=40),
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

        url = f"{self._base_url}/audio/transcriptions"
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
        data.add_field("model", kwargs.get("model", "whisper-1"))

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
                            return result.get("text", "")
                        log_debug(
                            f"[openai_compat] STT {url} returned {resp.status} – not supported"
                        )
            except Exception as exc:
                log_debug(f"[openai_compat] STT request failed ({url}): {exc}")
        return None

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
        timeout: float = 30.0,
    ) -> tuple[bool, str]:
        """Send a minimal chat 'ping' to verify cortex connectivity.

        Posts a single ``ping`` user message directly via aiohttp (no SDK
        auth flow, no SyntH prompt).  Returns ``(True, reply_text)`` on
        success, ``(False, error_str)`` on failure.
        """
        import aiohttp

        chat_url = f"{self._base_url}/v1/chat/completions"
        payload: dict[str, Any] = {
            "model": model or "default",
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        log_debug(
            f"[openai_compat] ping_test → POST {chat_url} model={payload['model']}"
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    chat_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        err = f"HTTP {resp.status}: {body[:200]}"
                        log_warning(f"[openai_compat] ping_test failed: {err}")
                        return False, err
                    data = await resp.json()
                    reply = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    log_debug(f"[openai_compat] ping_test OK — reply: {reply!r}")
                    return True, reply
        except Exception as exc:
            err = repr(exc)
            log_warning(f"[openai_compat] ping_test exception (url={chat_url}): {err}")
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

        # --- Vision: check model names from /models list ---
        try:
            models = await self.list_models()
            for m in models:
                if any(
                    kw in m.id.lower()
                    for kw in ("vision", "vl", "llava", "visual", "gpt-4o", "gemma3")
                ):
                    capabilities["vision"] = True
                    break
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
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/audio/transcriptions",
                    data=data,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    # Any non-404 status means the path exists (empty file may return 400)
                    capabilities["auris"] = resp.status != 404
        except Exception:
            pass

        return capabilities
