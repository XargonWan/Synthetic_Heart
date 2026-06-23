from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from typing import Any

from core.logging_utils import log_error, log_info, log_warning

from .azuracast_client import AzuraCastClient

_NON_LATIN_RE = re.compile(
    r"[^\x20-\x7E\xA0-\xFF\u0100-\u017F\u2010-\u2018\u201C-\u201D]+"
)


class JingleInjector:
    def __init__(
        self,
        client: AzuraCastClient,
        station_shortcode: str,
        audio_storage_dir: str | None = None,
        streamer_username: str = "SyntH",
        streamer_password: str = "synthradio",
        gain_db: float = 4.0,
    ):
        self._client = client
        self._station_shortcode = station_shortcode
        self._temp_dir = tempfile.mkdtemp(prefix="radio_host_")
        self._audio_storage_dir = audio_storage_dir
        self._streamer_username = streamer_username
        self._streamer_password = streamer_password
        self._gain_db = gain_db
        self._injected_files: list[str] = []

    def update_station_shortcode(self, shortcode: str) -> None:
        self._station_shortcode = shortcode

    def update_streamer_credentials(
        self, username: str | None = None, password: str | None = None
    ) -> None:
        if username is not None:
            self._streamer_username = username
        if password is not None:
            self._streamer_password = password

    def update_gain(self, gain_db: float) -> None:
        self._gain_db = gain_db

    async def inject_banter(
        self,
        text: str,
        style: str = "transition",
        pre_generated_audio_path: str | None = None,
        synth_name: str | None = None,
    ) -> dict[str, Any]:
        if not text or not text.strip():
            return {"status": "skipped", "reason": "empty_text"}

        audio_path = pre_generated_audio_path
        if not audio_path:
            audio_path = await self.generate_tts(text)
        if not audio_path:
            log_warning("[radio_host] TTS generation failed for banter")
            return {"status": "error", "reason": "tts_failed"}

        ts = int(time.time())

        persistent_path: str | None = None
        if self._audio_storage_dir:
            try:
                os.makedirs(self._audio_storage_dir, exist_ok=True)
                dest_name = f"banter_{ts}.wav"
                dest_path = os.path.join(self._audio_storage_dir, dest_name)
                shutil.copy2(audio_path, dest_path)
                persistent_path = dest_path
            except Exception as copy_err:
                log_warning(f"[radio_host] Could not save persistent audio: {copy_err}")

        speaker_name = synth_name or self._streamer_username or "SyntH"
        result = await self._client.broadcast_banter(
            station_shortcode=self._station_shortcode,
            audio_path=audio_path,
            username=self._streamer_username,
            password=self._streamer_password,
            title=f"{speaker_name} is speaking",
            artist="",
            gain_db=self._gain_db,
        )

        log_info(
            f"[radio_host] Banter broadcast result: {result.get('status')} "
            f"('{text[:50]}...')"
        )

        # Only track the temp audio for cleanup on success
        if result.get("status") == "success":
            self._injected_files.append(audio_path)

        return {
            "status": result.get("status", "error"),
            "audio_path": persistent_path or audio_path,
            "text": text,
            "style": style,
        }

    async def generate_tts(self, text: str) -> str | None:
        try:
            from core.config_manager import config_registry
            from core.core_initializer import PLUGIN_REGISTRY

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

            engine_name = engine_override or getattr(vox, "_active_engine_name", "")
            tts_text = (
                _NON_LATIN_RE.sub("", text).strip()
                if "kitten" in engine_name.lower()
                else text.strip()
            )
            if not tts_text:
                log_warning("[radio_host] TTS text empty after sanitization")
                tts_text = "A track transition."

            out_dir = os.path.join(self._temp_dir, "tts")
            os.makedirs(out_dir, exist_ok=True)

            result = await vox.speak(
                text=tts_text,
                engine_name=engine_override or None,
                generate_only=True,
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
