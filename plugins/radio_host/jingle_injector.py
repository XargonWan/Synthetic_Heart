from __future__ import annotations

import os
import tempfile
from typing import Any

from core.logging_utils import log_error, log_info, log_warning

from .azuracast_client import AzuraCastClient


class JingleInjector:
    def __init__(self, client: AzuraCastClient, station_id: str):
        self._client = client
        self._station_id = station_id
        self._temp_dir = tempfile.mkdtemp(prefix="radio_host_")
        self._injected_files: list[str] = []

    def update_station(self, station_id: str) -> None:
        self._station_id = station_id

    async def inject_banter(
        self, text: str, style: str = "transition"
    ) -> dict[str, Any]:
        if not text or not text.strip():
            return {"status": "skipped", "reason": "empty_text"}

        audio_path = await self._generate_tts(text)
        if not audio_path:
            log_warning("[radio_host] TTS generation failed for banter")
            return {"status": "error", "reason": "tts_failed"}

        upload_result = await self._client.upload_file(self._station_id, audio_path)
        if upload_result is None:
            return {"status": "error", "reason": "upload_failed"}

        self._injected_files.append(audio_path)

        log_info(f"[radio_host] Banter injected ({style}): {text[:80]}...")
        return {
            "status": "success",
            "audio_path": audio_path,
            "text": text,
            "style": style,
        }

    async def _generate_tts(self, text: str) -> str | None:
        try:
            from core.core_initializer import PLUGIN_REGISTRY
            from core.config_manager import config_registry

            vox = PLUGIN_REGISTRY.get("vox")
            if vox is None:
                log_warning("[radio_host] Vox plugin not available")
                return None

            engine_override = (
                str(
                    config_registry.get_value(
                        "RADIO_HOST_VOX_ENGINE",
                        "",
                        value_type=str,
                        group="plugins",
                        component="radio_host",
                    )
                )
                or None
            )

            out_dir = os.path.join(self._temp_dir, "tts")
            os.makedirs(out_dir, exist_ok=True)

            result = await vox.speak(
                text=text,
                engine_name=engine_override or None,
            )

            audio_path = result.get("audio_path") if isinstance(result, dict) else None
            if audio_path and os.path.isfile(audio_path):
                return audio_path

            log_warning(f"[radio_host] speak() returned no audio path: {result}")
            return None
        except Exception as e:
            log_error(f"[radio_host] TTS error: {e}")
            return None

    async def cleanup(self) -> None:
        for path in self._injected_files:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        self._injected_files.clear()
        try:
            os.rmdir(self._temp_dir)
        except OSError:
            pass
