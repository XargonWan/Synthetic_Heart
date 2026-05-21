from __future__ import annotations

import asyncio
import os
import tempfile
import time
from typing import Any

from core.logging_utils import log_error, log_info, log_warning

from .azuracast_client import AzuraCastClient


class JingleInjector:
    def __init__(
        self,
        client: AzuraCastClient,
        station_id: str,
        audio_storage_dir: str | None = None,
    ):
        self._client = client
        self._station_id = station_id
        self._temp_dir = tempfile.mkdtemp(prefix="radio_host_")
        self._audio_storage_dir = audio_storage_dir
        self._injected_files: list[str] = []

    def update_station(self, station_id: str) -> None:
        self._station_id = station_id

    def update_audio_storage_dir(self, audio_storage_dir: str | None) -> None:
        self._audio_storage_dir = audio_storage_dir

    def _extract_media_data(
        self, upload_result: dict[str, Any]
    ) -> tuple[int | None, str | None]:
        data = upload_result
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            data = data["data"]
        media_id = data.get("id")
        unique_id = data.get("unique_id")
        return media_id, unique_id

    async def inject_banter(
        self, text: str, style: str = "transition"
    ) -> dict[str, Any]:
        if not text or not text.strip():
            return {"status": "skipped", "reason": "empty_text"}

        audio_path = await self._generate_tts(text)
        if not audio_path:
            log_warning("[radio_host] TTS generation failed for banter")
            return {"status": "error", "reason": "tts_failed"}

        ts = int(time.time())

        # --- Persist a local copy before uploading (keep for replay) --------
        persistent_path: str | None = None
        if self._audio_storage_dir:
            import shutil

            try:
                os.makedirs(self._audio_storage_dir, exist_ok=True)
                dest_name = f"banter_{ts}.wav"
                dest_path = os.path.join(self._audio_storage_dir, dest_name)
                shutil.copy2(audio_path, dest_path)
                persistent_path = dest_path
            except Exception as copy_err:
                log_warning(f"[radio_host] Could not save persistent audio: {copy_err}")
        # -----------------------------------------------------------------------

        dest = f"_banter/synth_{ts}.wav"
        upload_result = await self._client.upload_file(
            self._station_id, audio_path, destination=dest
        )
        if upload_result is None:
            return {"status": "error", "reason": "upload_failed"}

        media_id, unique_id = self._extract_media_data(upload_result)

        if unique_id:
            queued = await self._client.queue_media(self._station_id, unique_id)
            if queued:
                log_info(
                    f"[radio_host] Banter queued for immediate playback "
                    f"(media {unique_id})"
                )

        if media_id is not None:
            asyncio.create_task(self._delayed_cleanup(media_id))

        # Only track the temp file (not persistent copy) for cleanup()
        self._injected_files.append(audio_path)

        log_info(f"[radio_host] Banter injected ({style}): {text[:80]}...")
        return {
            "status": "success",
            "audio_path": persistent_path or audio_path,
            "text": text,
            "style": style,
            "media_id": media_id,
            "media_unique_id": unique_id,
        }

    async def _delayed_cleanup(self, media_id: int | str) -> None:
        await asyncio.sleep(120)
        ok = await self._client.delete_media(self._station_id, media_id)
        if ok:
            log_info(f"[radio_host] Cleaned up banter media {media_id}")

    async def _generate_tts(self, text: str) -> str | None:
        try:
            from core.core_initializer import PLUGIN_REGISTRY
            from core.config_manager import config_registry

            # VoxPlugin registers itself as "vox_plugin" (see plugins/vox_plugin.py).
            # Fall back to "vox" for forward compatibility.
            vox = PLUGIN_REGISTRY.get("vox_plugin") or PLUGIN_REGISTRY.get("vox")
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
