# plugins/vox_engines/http.py
"""Vox TTS engine: HTTP endpoint.

Calls one or more external HTTP TTS servers.  Supports two payload styles:

* **Legacy** (the original ``tts_lipsync``-style backend):
  ``{"text", "voice_wav", "use_emo_text"}`` — used when no
  ``HTTP_TTS_REFERENCE_ID`` is configured.  ``voice_wav`` is only included
  when ``HTTP_TTS_VOICE_WAV`` is set.
* **Reference-id** (Fish Audio ``/v1/tts`` and compatible servers):
  ``{"text", "reference_id", "format"}`` — used when
  ``HTTP_TTS_REFERENCE_ID`` is set.  The API key is sent as a
  ``Authorization: Bearer`` header and the model tier (e.g.
  ``s2.1-pro-free``) as a ``model`` header.

All settings are registered under the ``http`` component so they surface in
the WebUI Engines tab inside the Vox → http box.  Failover across a
comma-separated endpoint list is supported in both styles.

Registration is performed at import time.
"""

from __future__ import annotations

import json
from typing import Any

import requests

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.vox_registry import register_vox_engine
from plugins.vox_base import VoxEngineBase

_COMPONENT = "http"


def _cfg(key: str, default: Any, value_type: type = str) -> Any:
    """Read one of this engine's config keys (registered below)."""
    return config_registry.get_value(
        key,
        default,
        value_type=value_type,
        group="plugins",
        component=_COMPONENT,
    )


def _register_config() -> None:
    """Register the engine's config keys so the WebUI renders them.

    The Engines tab shows, inside each Vox engine box, every config item
    whose ``component`` matches the engine's registry name — so these must
    all use ``component="http"``.
    """
    config_registry.get_value(
        "HTTP_TTS_ENDPOINTS",
        "",
        label="Endpoint URL(s)",
        description=(
            "Comma-separated TTS endpoint URLs, tried in order until one "
            "succeeds. Fish Audio: https://api.fish.audio/v1/tts. Falls back "
            "to the legacy TTS_ENDPOINTS key when empty."
        ),
        value_type=str,
        group="plugins",
        component=_COMPONENT,
    )
    config_registry.get_value(
        "HTTP_TTS_API_KEY",
        "",
        label="API key",
        description=(
            "Sent as 'Authorization: Bearer <key>'. Required by Fish Audio; "
            "leave empty for servers without authentication."
        ),
        value_type=str,
        group="plugins",
        component=_COMPONENT,
        sensitive=True,
    )
    config_registry.get_value(
        "HTTP_TTS_MODEL",
        "",
        label="Model",
        description=(
            "Sent as a 'model' HTTP header when set. Fish Audio model tiers: "
            "s2.1-pro-free, s2.1-pro, s1."
        ),
        value_type=str,
        group="plugins",
        component=_COMPONENT,
    )
    config_registry.get_value(
        "HTTP_TTS_REFERENCE_ID",
        "",
        label="Reference voice ID",
        description=(
            "Voice reference_id (Fish Audio cloned/library voice). Setting "
            "this switches the request payload to the Fish-style "
            "{text, reference_id, format} schema; leave empty for legacy "
            "servers that expect {text, voice_wav}."
        ),
        value_type=str,
        group="plugins",
        component=_COMPONENT,
    )
    config_registry.get_value(
        "HTTP_TTS_FORMAT",
        "pcm",
        label="Audio format",
        description=(
            "Format returned by the server. 'wav' is recommended for Fish "
            "Audio (sent as the payload 'format' field); 'pcm' matches "
            "legacy raw-PCM servers. Note: Fish Audio's pcm output is "
            "44100 Hz — set the sample rate accordingly."
        ),
        value_type=str,
        group="plugins",
        component=_COMPONENT,
        constraints={"choices": ["pcm", "wav"]},
    )
    config_registry.get_value(
        "HTTP_TTS_SAMPLE_RATE",
        22050,
        label="PCM sample rate",
        description=(
            "Sample rate used to wrap raw PCM responses into WAV. Ignored "
            "when the format is 'wav'. Legacy servers: 22050; Fish Audio "
            "pcm: 44100."
        ),
        value_type=int,
        group="plugins",
        component=_COMPONENT,
        advanced=True,
    )
    config_registry.get_value(
        "HTTP_TTS_VOICE_WAV",
        "",
        label="Reference voice WAV path (legacy)",
        description=(
            "Server-side path to the reference voice WAV, sent as "
            "'voice_wav' in legacy payload mode. Omitted from the payload "
            "when empty."
        ),
        value_type=str,
        group="plugins",
        component=_COMPONENT,
        advanced=True,
    )
    config_registry.get_value(
        "HTTP_TTS_EXTRA_HEADERS",
        "{}",
        label="Extra HTTP headers (JSON)",
        description=(
            'JSON object merged into the request headers, e.g. {"X-Custom": "value"}.'
        ),
        value_type=str,
        group="plugins",
        component=_COMPONENT,
        advanced=True,
    )
    config_registry.get_value(
        "HTTP_TTS_EXTRA_PARAMS",
        "{}",
        label="Extra payload parameters (JSON)",
        description=(
            "JSON object merged into the request payload, e.g. "
            '{"temperature": 0.7, "top_p": 0.7} for Fish Audio prosody '
            "controls."
        ),
        value_type=str,
        group="plugins",
        component=_COMPONENT,
        advanced=True,
    )
    config_registry.get_value(
        "HTTP_TTS_TIMEOUT_SECONDS",
        0,
        label="Request timeout (seconds)",
        description=(
            "Per-request timeout. 0 falls back to the legacy "
            "TTS_TIMEOUT_SECONDS key (default 300)."
        ),
        value_type=int,
        group="plugins",
        component=_COMPONENT,
        advanced=True,
    )

    # Password-style rendering for the API key in the WebUI.
    try:
        from core.variables_engine import exposed_vars, register_exposed_var

        if exposed_vars.get_definition("HTTP_TTS_API_KEY") is None:
            register_exposed_var(
                "HTTP_TTS_API_KEY",
                label="API key",
                default="",
                value_type=str,
                ui_type="password",
                description="Bearer token for the HTTP TTS endpoint.",
                scope="plugins",
                component=_COMPONENT,
            )
    except Exception:  # pragma: no cover - cosmetic only
        pass


_register_config()


def _parse_json_config(key: str) -> dict[str, Any]:
    """Parse a JSON-object config value, returning {} on any problem."""
    raw = str(_cfg(key, "{}") or "{}").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        log_warning(f"[vox/http] {key} is not valid JSON ({exc}); ignoring.")
        return {}
    if not isinstance(parsed, dict):
        log_warning(f"[vox/http] {key} must be a JSON object; ignoring.")
        return {}
    return parsed


class HttpVoxEngine(VoxEngineBase):
    """Vox TTS engine that posts to an external HTTP TTS server."""

    display_name = "HTTP TTS endpoint"

    @property
    def output_format(self) -> str:
        # "wav" when the remote server returns RIFF data (e.g. Fish Audio
        # with format=wav); raw PCM otherwise (legacy servers).
        return "wav" if str(_cfg("HTTP_TTS_FORMAT", "pcm")) == "wav" else "pcm"

    @property
    def sample_rate(self) -> int:
        return int(_cfg("HTTP_TTS_SAMPLE_RATE", 22050, int))

    @property
    def channels(self) -> int:
        return 1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_endpoints(self) -> list[str]:
        raw = str(_cfg("HTTP_TTS_ENDPOINTS", "") or "")
        if not raw.strip():
            # Legacy key kept so existing .env / DB configs keep working.
            raw = str(
                config_registry.get_value(
                    "TTS_ENDPOINTS",
                    "",
                    value_type=str,
                    group="plugins",
                    component="tts_lipsync",
                )
                or ""
            )
        return [e.strip() for e in raw.split(",") if e.strip()]

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        api_key = str(_cfg("HTTP_TTS_API_KEY", "") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        model = str(_cfg("HTTP_TTS_MODEL", "") or "").strip()
        if model:
            headers["model"] = model
        for k, v in _parse_json_config("HTTP_TTS_EXTRA_HEADERS").items():
            headers[str(k)] = str(v)
        return headers

    def _build_payload(self, text: str, **kwargs: Any) -> dict[str, Any]:
        reference_id = str(_cfg("HTTP_TTS_REFERENCE_ID", "") or "").strip()
        if reference_id:
            # Fish Audio / reference-id style servers.
            payload: dict[str, Any] = {
                "text": text,
                "reference_id": reference_id,
                "format": str(_cfg("HTTP_TTS_FORMAT", "pcm")),
            }
        else:
            # Legacy voice_wav-style servers.
            payload = {
                "text": text,
                "use_emo_text": False,
            }
            voice_wav = str(_cfg("HTTP_TTS_VOICE_WAV", "") or "").strip()
            if voice_wav:
                payload["voice_wav"] = voice_wav
            # forward optional language hint to remote server
            if kwargs.get("language"):
                payload["language"] = kwargs["language"]
        payload.update(_parse_json_config("HTTP_TTS_EXTRA_PARAMS"))
        return payload

    def _timeout_for(self) -> int:
        configured = int(_cfg("HTTP_TTS_TIMEOUT_SECONDS", 0, int))
        if configured > 0:
            return configured
        return int(
            config_registry.get_value(
                "TTS_TIMEOUT_SECONDS",
                300,
                value_type=int,
                group="plugins",
                component="tts_lipsync",
            )
        )

    def _post_tts(
        self,
        endpoint: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout_s: int,
    ) -> bytes | None:
        try:
            resp = requests.post(
                endpoint, json=payload, headers=headers or None, timeout=timeout_s
            )
            if resp.status_code != 200:
                log_warning(
                    f"[vox/http] {endpoint} → HTTP {resp.status_code}: {resp.text[:200]}"
                )
                return None
            # Success responses are a raw audio byte stream — never JSON.
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
            log_warning(
                "[vox/http] No endpoints configured — set HTTP_TTS_ENDPOINTS "
                "in Engines → Vox → http."
            )
            return None

        headers = self._build_headers()
        payload = self._build_payload(text, **kwargs)
        timeout = self._timeout_for()

        for endpoint in endpoints:
            log_debug(f"[vox/http] POST {endpoint} (timeout={timeout}s)")
            audio = self._post_tts(endpoint, payload, headers, timeout)
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
    label=(
        "External HTTP TTS server (Fish Audio /v1/tts or any custom server). "
        "Configure endpoint, API key, model and voice below."
    ),
)
