# core/external_endpoints/bridges/auris_bridge.py
"""Auris (STT) bridge for external endpoints."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING

from plugins.auris_base import AurisEngineBase, AurisTranscriptResult

if TYPE_CHECKING:
    from core.external_endpoints.adapters.base import BaseProtocolAdapter
    from core.external_endpoints.models import ExternalEndpoint


def _looks_like_wav(audio_bytes: bytes) -> bool:
    """Return True if *audio_bytes* already starts with a RIFF/WAVE header."""
    return (
        len(audio_bytes) >= 12
        and audio_bytes[0:4] == b"RIFF"
        and audio_bytes[8:12] == b"WAVE"
    )


def _transcode_to_wav(audio_bytes: bytes) -> bytes | None:
    """Transcode arbitrary audio (webm/opus, mp3, ogg, ...) to 16 kHz mono WAV.

    Many STT backends (e.g. faster-whisper on Harmony) can only decode a small
    set of container formats and reject browser-recorded ``webm/opus`` with a
    "Format not recognised" error.  Browsers' ``MediaRecorder`` almost always
    produces ``webm/opus``, so we normalise everything to WAV via ``ffmpeg``
    before forwarding.  Returns the WAV bytes, or ``None`` if transcoding fails
    (caller then falls back to the original bytes).
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    in_path: str | None = None
    out_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".in", delete=False) as in_fh:
            in_fh.write(audio_bytes)
            in_path = in_fh.name
        out_fd, out_path = tempfile.mkstemp(suffix=".wav")
        os.close(out_fd)

        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                ffmpeg,
                "-y",
                "-i",
                in_path,
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                out_path,
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            from core.logging_utils import log_warning

            log_warning(
                "[auris_bridge] ffmpeg transcode to WAV failed "
                f"(rc={result.returncode}): "
                f"{result.stderr.decode('utf-8', 'replace')[:300]}"
            )
            return None

        with open(out_path, "rb") as out_fh:
            wav_bytes = out_fh.read()
        return wav_bytes or None
    except Exception as exc:
        from core.logging_utils import log_warning

        log_warning(f"[auris_bridge] ffmpeg transcode raised: {exc!r}")
        return None
    finally:
        for path in (in_path, out_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


class ExternalAurisEngine(AurisEngineBase):
    """AurisEngineBase implementation backed by an external endpoint adapter."""

    def __init__(
        self,
        endpoint: "ExternalEndpoint",
        adapter: "BaseProtocolAdapter",
    ) -> None:
        self._endpoint = endpoint
        self._adapter = adapter
        self._adapter._engine_label = endpoint.name or "auris_bridge"
        self.display_name = f"{endpoint.display_label or endpoint.name} (STT)"

    def transcribe(
        self,
        file_path: str,
        mime_type: str | None = None,
    ) -> AurisTranscriptResult | None:
        """Read *file_path* and forward audio bytes to the external adapter."""
        import asyncio

        if not os.path.exists(file_path):
            return None

        with open(file_path, "rb") as fh:
            audio_bytes = fh.read()

        if not audio_bytes:
            return None

        # Normalise the audio to WAV before forwarding.  Browser MediaRecorder
        # produces ``webm/opus``, which some STT backends (e.g. faster-whisper
        # on Harmony) reject with "Format not recognised".  Transcoding to a
        # plain 16 kHz mono WAV makes the STT path format-agnostic.  If the
        # input is already WAV, or ffmpeg is unavailable / fails, we keep the
        # original bytes.
        if not _looks_like_wav(audio_bytes):
            wav_bytes = _transcode_to_wav(audio_bytes)
            if wav_bytes:
                audio_bytes = wav_bytes
                mime_type = "audio/wav"

        # Resolve the STT model for this endpoint.  Multi-modal endpoints (e.g.
        # Harmony) use a dedicated ``stt_model`` in ``extra_config`` because the
        # endpoint's ``default_model`` is reserved for the cortex/text engine and
        # is not a valid speech-to-text model.
        extra = self._endpoint.extra_config or {}
        stt_model = extra.get("stt_model") or self._endpoint.default_model

        transcribe_kwargs: dict[str, str | None] = {"mime_type": mime_type}
        if stt_model:
            transcribe_kwargs["model"] = stt_model

        coro = self._adapter.transcribe_audio(audio_bytes, **transcribe_kwargs)

        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is not None:
                # Called from within a running event loop: schedule the coroutine
                # on that loop and block on a Future.
                import concurrent.futures

                future: concurrent.futures.Future[str | None] = (
                    concurrent.futures.Future()
                )

                async def _run() -> None:
                    try:
                        result = await coro
                        future.set_result(result)
                    except Exception as exc:
                        future.set_exception(exc)

                asyncio.ensure_future(_run())
                text = future.result(timeout=120)
            else:
                # No running loop in this thread (e.g. called via
                # ``asyncio.to_thread``): run the coroutine to completion here.
                text = asyncio.run(coro)
        except Exception as exc:
            from core.logging_utils import log_error

            log_error(
                f"[auris_bridge] transcribe failed "
                f"(model={stt_model}, mime={mime_type}): {exc!r}"
            )
            return None

        if text is None:
            return None

        return AurisTranscriptResult(text=text, language=None)


# Required by AurisRegistry::load_engine() when loading via module path
ENGINE_CLASS = ExternalAurisEngine
