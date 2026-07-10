# core/external_endpoints/adapters/fish_audio_adapter.py
"""Adapter for the Fish Audio cloud TTS API (https://api.fish.audio/v1/tts).

Fish Audio is a TTS-only provider: the adapter implements ``generate_tts``
and reports a fixed ``vox`` capability.  Requests use the Fish payload schema
``{"text", "reference_id", "format"}`` with the API key sent as an
``Authorization: Bearer`` header and the model tier (e.g. ``s2.1-pro-free``)
as a ``model`` header.

Relevant ``extra_config`` keys (set by the provider preset / add-endpoint
wizard):

* ``tts_model``        — model tier header (default ``s2.1-pro-free``)
* ``tts_output_format``— ``wav`` / ``mp3`` / ``pcm`` payload format
* ``tts_reference_id`` — cloned/library voice id used as ``reference_id``
* ``tts_extra_payload``— optional dict merged into the request payload
  (e.g. ``{"temperature": 0.7}`` prosody controls)
"""

from __future__ import annotations

import time as _time
from typing import Any

import aiohttp

from core.cortex_api_logger import log_cortex_request, log_cortex_response
from core.external_endpoints.adapters.base import (
    BaseProtocolAdapter,
    ChatResponse,
    ModelInfo,
)
from core.logging_utils import log_warning

DEFAULT_BASE_URL = "https://api.fish.audio/v1/tts"
DEFAULT_MODEL = "s2.1-pro-free"

# Fish Audio model tiers — the API has no public model-listing endpoint, so
# these are advertised statically to populate the WebUI model dropdown.
_MODEL_TIERS: tuple[tuple[str, str], ...] = (
    ("s2.1-pro-free", "Speech 2.1 Pro (free tier)"),
    ("s2.1-pro", "Speech 2.1 Pro"),
    ("s1", "Speech 1"),
)

_SUPPORTED_FORMATS = frozenset({"wav", "mp3", "pcm"})


class FishAudioAdapter(BaseProtocolAdapter):
    """Adapter for the Fish Audio ``/v1/tts`` endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._api_key = api_key
        self._extra_config = extra_config or {}

    # ------------------------------------------------------------------
    # Chat / LLM — not supported (TTS-only provider)
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatResponse:
        raise NotImplementedError("Fish Audio is a TTS-only endpoint")

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=tier_id,
                name=tier_label,
                owned_by="fish.audio",
                capabilities={"vox": True},
                model_type="tts",
                input_modalities=["text"],
                output_modalities=["audio"],
            )
            for tier_id, tier_label in _MODEL_TIERS
        ]

    # ------------------------------------------------------------------
    # TTS
    # ------------------------------------------------------------------

    def _resolve_format(self, requested: Any) -> str:
        fmt = str(
            requested
            or self._extra_config.get("tts_output_format")
            or self._extra_config.get("output_format")
            or "wav"
        ).lower()
        return fmt if fmt in _SUPPORTED_FORMATS else "wav"

    async def generate_tts(
        self,
        text: str,
        voice: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        engine_tag = f"fish_audio:{self._engine_label or 'default'}"

        reference_id = str(
            voice
            or self._extra_config.get("tts_reference_id")
            or self._extra_config.get("reference_id")
            or ""
        ).strip()
        fmt = self._resolve_format(kwargs.get("format"))
        model = str(
            kwargs.get("model") or self._extra_config.get("tts_model") or DEFAULT_MODEL
        )

        payload: dict[str, Any] = {"text": text, "format": fmt}
        if reference_id:
            payload["reference_id"] = reference_id
        extra_payload = self._extra_config.get("tts_extra_payload")
        if isinstance(extra_payload, dict):
            for key, value in extra_payload.items():
                payload.setdefault(str(key), value)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "model": model,
        }

        log_cortex_request(
            engine_tag,
            model=model,
            url=self._base_url,
            payload={
                "task": "generate_tts",
                "text_length": len(text),
                "reference_id": reference_id or None,
                "format": fmt,
            },
        )
        _req_start = _time.monotonic()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._base_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:500]
                        _elapsed = (_time.monotonic() - _req_start) * 1000
                        log_cortex_response(
                            engine_tag,
                            model=model,
                            status=resp.status,
                            error=f"HTTP {resp.status}: {body}",
                            elapsed_ms=_elapsed,
                        )
                        log_warning(
                            f"[fish_audio] {self._base_url} returned "
                            f"HTTP {resp.status}: {body}"
                        )
                        return None
                    audio_data = await resp.read()
                    _elapsed = (_time.monotonic() - _req_start) * 1000
                    log_cortex_response(
                        engine_tag,
                        model=model,
                        status=200,
                        body=f"<audio: {len(audio_data)} bytes>",
                        elapsed_ms=_elapsed,
                    )
                    return audio_data
        except Exception as exc:
            _elapsed = (_time.monotonic() - _req_start) * 1000
            log_cortex_response(
                engine_tag,
                model=model,
                error=str(exc),
                elapsed_ms=_elapsed,
            )
            log_warning(f"[fish_audio] request failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Probe / health
    # ------------------------------------------------------------------

    async def probe_capabilities(self) -> dict[str, bool]:
        # TTS-only provider; synthesis is billed, so no live probe request.
        return {"cortex": False, "vox": True, "auris": False, "live": False}

    async def health_check(self) -> bool:
        # No free health endpoint — reachable iff configured with a key.
        return bool(self._api_key)

    async def ping_test(
        self,
        model: str | None = None,
        timeout: float = 15.0,
    ) -> tuple[bool, str]:
        return False, "Fish Audio is a TTS-only endpoint (no chat ping)"
