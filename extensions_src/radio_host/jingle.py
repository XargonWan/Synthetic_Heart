"""Making the banter audio and putting it on air.

The voice itself is the Core's (``ctx.voice.synthesize``): this module keeps what is
the radio's own: where the clips are kept, the replay copy shown in the Radio window,
and the WebDJ broadcast to AzuraCast.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from azuracast_client import AzuraCastClient
from radiolog import log_error, log_info, log_warning
from synth_sdk.runtime import Context

#: How many replay clips are kept for the Radio window.
KEEP_REPLAYS = 30


class JingleInjector:
    def __init__(
        self,
        ctx: Context,
        client: AzuraCastClient,
        station_shortcode: str,
        audio_dir: Path,
        streamer_username: str = "SyntH",
        streamer_password: str = "synthradio",
        gain_db: float = 4.0,
        voice_engine: str = "",
    ):
        self._ctx = ctx
        self._client = client
        self._station_shortcode = station_shortcode
        self._audio_dir = audio_dir
        self._streamer_username = streamer_username
        self._streamer_password = streamer_password
        self._gain_db = gain_db
        self._voice_engine = voice_engine

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

    def update_voice_engine(self, engine: str) -> None:
        self._voice_engine = engine

    async def generate_tts(self, text: str) -> str | None:
        """Speak ``text`` and return a WAV file of ours, or ``None``."""
        try:
            produced = await self._ctx.voice.synthesize(text, self._voice_engine)
        except Exception as e:  # noqa: BLE001 - a missing voice must not stop the radio
            log_error(f"[radio_host] TTS error: {e}")
            return None
        if produced is None:
            log_warning("[radio_host] TTS generation failed for banter")
            return None
        # The Core's copy is removed within the hour; ours stays until we are done.
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        kept = self._audio_dir / f"tts_{time.time_ns()}{produced.suffix or '.wav'}"
        try:
            shutil.move(str(produced), kept)
        except OSError as e:
            log_warning(f"[radio_host] could not keep the synthesised clip: {e}")
            return str(produced)
        return str(kept)

    def save_replay(self, webm: bytes | None) -> str | None:
        """The small copy the Radio window plays back."""
        if not webm:
            return None
        try:
            self._audio_dir.mkdir(parents=True, exist_ok=True)
            path = self._audio_dir / f"banter_{time.time_ns()}.webm"
            path.write_bytes(webm)
            return str(path)
        except OSError as e:
            log_warning(f"[radio_host] Could not save persistent audio: {e}")
            return None

    async def replay_copy(self, audio_path: str) -> str | None:
        """Convert a clip to the replay format (with the on-air gain)."""
        return self.save_replay(
            await self._client.convert_audio_to_webm(audio_path, gain_db=self._gain_db)
        )

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

        speaker_name = synth_name or self._streamer_username or "SyntH"
        webm = await self._client.convert_audio_to_webm(
            audio_path, gain_db=self._gain_db
        )
        if webm is None:
            return {"status": "error", "reason": "conversion_failed"}
        replay_path = self.save_replay(webm)
        result = await self._client.broadcast_webm(
            webm,
            station_shortcode=self._station_shortcode,
            username=self._streamer_username,
            password=self._streamer_password,
            title=f"{speaker_name} is speaking",
            artist="",
        )
        log_info(
            f"[radio_host] Banter broadcast result: {result.get('status')} "
            f"('{text[:50]}...')"
        )
        return {
            "status": result.get("status", "error"),
            "audio_path": audio_path,
            "replay_path": replay_path,
            "text": text,
            "style": style,
        }

    def forget(self, *paths: str | None) -> None:
        """Delete clips that are no longer needed."""
        for path in paths:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def trim_replays(self, keep: int = KEEP_REPLAYS) -> list[str]:
        """Delete all but the newest ``keep`` replay clips; return what was removed."""
        try:
            clips = sorted(
                self._audio_dir.glob("banter_*.webm"), key=lambda p: p.stat().st_mtime
            )
        except OSError:
            return []
        removed: list[str] = []
        for clip in clips[:-keep] if keep > 0 else clips:
            try:
                clip.unlink()
                removed.append(clip.name)
            except OSError:
                continue
        return removed
