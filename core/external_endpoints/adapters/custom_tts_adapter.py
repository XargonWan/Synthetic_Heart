# core/external_endpoints/adapters/custom_tts_adapter.py
"""Adapter for legacy/custom HTTP TTS endpoints.

This adapter supports external endpoints that expose a simple HTTP TTS API
similar to the legacy ``tts_lipsync``/``index-tts`` backend.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from core.external_endpoints.adapters.base import BaseProtocolAdapter, ChatResponse, ModelInfo
from core.logging_utils import log_debug, log_warning


class LegacyHttpTTSAdapter(BaseProtocolAdapter):
    """Adapter for legacy HTTP TTS endpoints."""

    def __init__(self, base_url: str, extra_config: dict[str, Any] | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._extra_config = extra_config or {}

    # ------------------------------------------------------------------
    # Chat / LLM
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatResponse:
        raise NotImplementedError("Legacy HTTP TTS adapter does not support chat completion")

    async def list_models(self) -> list[ModelInfo]:
        return []

    # ------------------------------------------------------------------
    # TTS
    # ------------------------------------------------------------------

    async def generate_tts(
        self,
        text: str,
        voice: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        payload: dict[str, Any] = {
            "text": text,
            "use_emo_text": False,
        }

        voice_wav = self._extra_config.get("tts_voice_wav") or self._extra_config.get(
            "voice_wav"
        )
        if voice:
            payload["voice_wav"] = voice
        elif voice_wav:
            payload["voice_wav"] = voice_wav

        if "language" in kwargs and kwargs["language"]:
            payload["language"] = kwargs["language"]

        url = self._base_url
        if self._extra_config.get("tts_endpoint_path"):
            url = f"{self._base_url.rstrip('/')}/{self._extra_config['tts_endpoint_path'].lstrip('/')}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        log_warning(
                            f"[legacy_http_tts] {url} returned HTTP {resp.status}"
                        )
                        return None
                    return await resp.read()
        except Exception as exc:
            log_warning(f"[legacy_http_tts] request failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Probe / health
    # ------------------------------------------------------------------

    async def probe_capabilities(self) -> dict[str, bool]:
        return {"vox": True}

    async def health_check(self) -> bool:
        payload = {"text": "ping", "use_emo_text": False}
        if self._extra_config.get("tts_voice_wav"):
            payload["voice_wav"] = self._extra_config["tts_voice_wav"]

        url = self._base_url
        if self._extra_config.get("tts_endpoint_path"):
            url = f"{self._base_url.rstrip('/')}/{self._extra_config['tts_endpoint_path'].lstrip('/')}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def ping_test(
        self,
        model: str | None = None,
        timeout: float = 15.0,
    ) -> tuple[bool, str]:
        return False, "ping_test not supported for legacy HTTP TTS"
