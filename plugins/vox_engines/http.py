# plugins/vox_engines/http.py
"""Vox TTS engine: HTTP endpoint.

Calls one or more external HTTP TTS servers (the original ``tts_lipsync``
backend).  Supports failover across a comma-separated ``TTS_ENDPOINTS`` list.

Registration is performed at import time.
"""

from __future__ import annotations

from typing import Any

import requests

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.vox_registry import register_vox_engine
from plugins.vox_base import VoxEngineBase


class HttpVoxEngine(VoxEngineBase):
    """Vox TTS engine that posts to an external HTTP TTS server."""

    display_name = "HTTP TTS endpoint"

    # Output is raw PCM from legacy servers; the Vox plugin will wrap it.
    # Override to "wav" if the server already returns RIFF data.
    @property
    def output_format(self) -> str:
        return "pcm"  # Vox plugin auto-wraps; individual servers may return WAV too

    @property
    def sample_rate(self) -> int:
        return 22050

    @property
    def channels(self) -> int:
        return 1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_endpoints(self) -> list[str]:
        raw = config_registry.get_value(
            "TTS_ENDPOINTS",
            "",
            value_type=str,
            group="plugins",
            component="tts_lipsync",
        )
        if not raw:
            return []
        return [e.strip() for e in str(raw).split(",") if e.strip()]

    def _get_voice_ref(self, endpoint: str) -> str:
        """Return the reference-voice path appropriate for a given endpoint.

        Kept for backward-compatibility with existing HTTP TTS servers.
        Operators may override via ``VOX_ENGINE_SETTINGS``.
        """
        if "192.168.1.69" in endpoint:
            return r"F:\0synth\0synth\reference\2b_ref.wav"
        return r"C:\Users\EVO\Documents\ai2\index-tts\index-tts-training_v2\audio\reference\2b_ref.wav"

    def _post_tts(
        self, endpoint: str, payload: dict[str, Any], timeout_s: int
    ) -> bytes | None:
        try:
            resp = requests.post(endpoint, json=payload, timeout=timeout_s)
            if resp.status_code != 200:
                log_warning(
                    f"[vox/http] {endpoint} → HTTP {resp.status_code}: {resp.text[:200]}"
                )
                return None
            return resp.content or None
        except Exception as exc:
            log_warning(f"[vox/http] Connection error for {endpoint}: {exc}")
            return None

    # ------------------------------------------------------------------
    # VoxEngineBase implementation
    # ------------------------------------------------------------------

    def generate_tts(
        self,
        text: str,
        emotion: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        endpoints = self._load_endpoints()
        if not endpoints:
            log_warning("[vox/http] No TTS_ENDPOINTS configured.")
            return None

        global_timeout: int = int(
            config_registry.get_value(
                "TTS_TIMEOUT_SECONDS",
                300,
                value_type=int,
                group="plugins",
                component="tts_lipsync",
            )
        )

        for endpoint in endpoints:
            voice_wav = self._get_voice_ref(endpoint)
            payload: dict[str, Any] = {
                "text": text,
                "voice_wav": voice_wav,
                "use_emo_text": False,
            }
            # forward optional language hint to remote server
            if "language" in kwargs and kwargs["language"]:
                payload["language"] = kwargs["language"]

            # Fast timeout for the primary server so failover is prompt
            timeout = 2 if "192.168.1.6:" in endpoint else global_timeout

            log_debug(f"[vox/http] POST {endpoint} (timeout={timeout}s)")
            audio = self._post_tts(endpoint, payload, timeout)
            if audio:
                log_info(f"[vox/http] Audio received from {endpoint}")
                return audio

        log_error("[vox/http] All TTS endpoints failed.")
        return None


# ---------------------------------------------------------------------------
# Export + auto-registration
# ---------------------------------------------------------------------------

ENGINE_CLASS = HttpVoxEngine

register_vox_engine(
    name="http",
    module_path=__name__,
    capabilities={
        "voice_cloning": True,
        "emotions": False,
        "streaming": False,
        "local": False,
    },
    label="HTTP TTS endpoint (legacy index-tts / custom server). Configure TTS_ENDPOINTS.",
)
