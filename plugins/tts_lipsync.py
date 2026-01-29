import asyncio
import os
import re
import time
from pathlib import Path
from typing import List

import requests

from core.ai_plugin_base import AIPluginBase
from core.core_initializer import register_plugin, INTERFACE_REGISTRY
from core.config_manager import config_registry
from core.variables_engine import register_exposed_var
from core.logging_utils import log_info, log_warning, log_error, log_debug


register_exposed_var(
    "TTS_ENDPOINTS",
    label="TTS Endpoints",
    default="",
    value_type=str,
    ui_type="string",
    description="Comma-separated list of TTS HTTP endpoints.",
    scope="plugins",
    component="tts_lipsync",
    advanced=True,
)

register_exposed_var(
    "TTS_TIMEOUT_SECONDS",
    label="TTS Timeout (s)",
    default=30,
    value_type=int,
    ui_type="number",
    description="Timeout for TTS HTTP requests in seconds.",
    scope="plugins",
    component="tts_lipsync",
    advanced=True,
)

register_exposed_var(
    "TTS_OUTPUT_DIR",
    label="TTS Output Directory",
    default="res/synth_webui/static/audio/tts",
    value_type=str,
    ui_type="string",
    description="Directory for generated TTS audio files.",
    scope="plugins",
    component="tts_lipsync",
    advanced=True,
)


class TTSLipSyncPlugin(AIPluginBase):
    display_name = "TTS Lip Sync"

    def __init__(self):
        super().__init__()
        register_plugin("tts_lipsync", self)
        log_info("[tts_lipsync] Plugin initialized")

        self.endpoints = self._load_endpoints()
        self.timeout_s = config_registry.get_value(
            "TTS_TIMEOUT_SECONDS",
            30,
            value_type=int,
            group="plugins",
            component="tts_lipsync",
        )
        self.output_dir = Path(
            config_registry.get_value(
                "TTS_OUTPUT_DIR",
                "res/synth_webui/static/audio/tts",
                value_type=str,
                group="plugins",
                component="tts_lipsync",
            )
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _load_endpoints(self) -> List[str]:
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

    def get_supported_action_types(self) -> list[str]:
        return ["tts_speak"]

    def get_supported_actions(self) -> dict:
        return {
            "tts_speak": {
                "description": "Generate speech from text and play it in the WebUI.",
                "required_fields": ["text"],
                "optional_fields": ["emotion"],
            }
        }

    async def handle_custom_action(self, action_type: str, payload: dict):
        if action_type != "tts_speak":
            return {"error": "Unknown action"}

        text = (payload or {}).get("text")
        emotion = (payload or {}).get("emotion")
        if not text or not isinstance(text, str):
            return {"status": "error", "message": "text is required"}

        filename = await self._generate_audio(text, emotion)
        if not filename:
            return {"status": "failed", "message": "tts generation failed"}

        audio_url = f"/static/audio/tts/{filename}"

        webui = INTERFACE_REGISTRY.get("synth_webui") or INTERFACE_REGISTRY.get("webui")
        if webui and hasattr(webui, "broadcast_event"):
            await webui.broadcast_event(
                "synth:tts-play",
                {"audio_url": audio_url, "text": text, "emotion": emotion},
            )
            log_info(f"[tts_lipsync] Broadcasted synth:tts-play for {audio_url}")
        else:
            log_warning("[tts_lipsync] WebUI interface not available; audio generated but not broadcast")

        return {"status": "success", "audio_url": audio_url}

    async def _generate_audio(self, text: str, emotion: str | None = None) -> str | None:
        endpoints = self._load_endpoints()
        if not endpoints:
            log_warning("[tts_lipsync] No TTS endpoints configured")
            return None

        clean_text = re.sub(r"\s+", " ", text).strip()
        if not clean_text:
            return None

        payload = {"text": clean_text, "emotion": emotion}
        audio_bytes = None

        for endpoint in endpoints:
            try:
                log_debug(f"[tts_lipsync] Requesting TTS from {endpoint}")
                audio_bytes = await asyncio.to_thread(
                    _post_tts,
                    endpoint,
                    payload,
                    self.timeout_s,
                )
                if audio_bytes:
                    break
            except Exception as e:
                log_warning(f"[tts_lipsync] TTS request failed for {endpoint}: {e}")

        if not audio_bytes:
            log_error("[tts_lipsync] All TTS endpoints failed")
            return None

        filename = f"tts_{int(time.time())}.wav"
        out_path = self.output_dir / filename
        try:
            with open(out_path, "wb") as f:
                f.write(audio_bytes)
        except Exception as e:
            log_error(f"[tts_lipsync] Failed to write audio file: {e}")
            return None

        return filename


def _post_tts(endpoint: str, payload: dict, timeout_s: int) -> bytes | None:
    resp = requests.post(endpoint, json=payload, timeout=timeout_s)
    if resp.status_code != 200:
        return None
    return resp.content or None


PLUGIN_CLASS = TTSLipSyncPlugin