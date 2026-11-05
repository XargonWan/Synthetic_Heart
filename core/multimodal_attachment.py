# core/multimodal_attachment.py
"""
Multimodal Attachment Handler for Synthetic Heart.

Provides utilities for extracting and processing multimodal content
(images, audio, documents) from various interface messages (Telegram, Discord).
This module bridges interface-specific attachment formats to the unified
format expected by LLM engines like gemini_api.

Usage:
    from core.multimodal_attachment import extract_multimodal_from_telegram

    attachments = await extract_multimodal_from_telegram(bot, message)
    # Pass to LLM via context: context["attachments"] = attachments
"""

import base64
import mimetypes
from pathlib import Path
from typing import Any

from core.logging_utils import log_debug, log_warning, log_error


# Supported MIME types (matching gemini_api.py).
# Note: Anthropic only accepts jpeg/png/gif/webp (its real API limit); Gemini and
# OpenAI-compatible vision endpoints additionally accept HEIC/HEIF.
SUPPORTED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/heic",
    "image/heif",
}
SUPPORTED_AUDIO_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/ogg",
    "audio/flac",
    "audio/aac",
    "audio/mp4",
    "audio/x-m4a",
    "audio/x-wav",
}
SUPPORTED_VIDEO_TYPES = {
    "video/mp4",
    "video/mpeg",
    "video/mov",
    "video/quicktime",
    "video/avi",
    "video/x-msvideo",
    "video/x-flv",
    "video/mpg",
    "video/webm",
    "video/wmv",
    "video/x-ms-wmv",
    "video/3gpp",
}
SUPPORTED_DOCUMENT_TYPES = {
    "application/pdf",
    "text/plain",
    "text/html",
    "text/css",
    "text/javascript",
    "application/javascript",
    "text/x-python",
    "text/markdown",
    "application/json",
    "application/xml",
    "text/xml",
    "text/csv",
}


def get_mime_type(file_path: str | Path | None, file_name: str | None = None) -> str:
    """Determine MIME type from file path or name."""
    # Try from file_path first
    if file_path:
        path = Path(file_path) if isinstance(file_path, str) else file_path
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type:
            return mime_type

    # Try from file_name
    if file_name:
        mime_type, _ = mimetypes.guess_type(file_name)
        if mime_type:
            return mime_type

    # Fallback mapping for common extensions
    ext_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
        ".oga": "audio/ogg",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".html": "text/html",
        ".htm": "text/html",
        ".css": "text/css",
        ".js": "text/javascript",
        ".py": "text/x-python",
        ".md": "text/markdown",
        ".json": "application/json",
        ".xml": "application/xml",
        ".csv": "text/csv",
        # Video formats
        ".mp4": "video/mp4",
        ".mpeg": "video/mpeg",
        ".mpg": "video/mpeg",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".flv": "video/x-flv",
        ".webm": "video/webm",
        ".wmv": "video/x-ms-wmv",
        ".3gp": "video/3gpp",
        ".3gpp": "video/3gpp",
    }

    # Get suffix from either path
    suffix = None
    if file_path:
        suffix = (
            Path(file_path).suffix.lower()
            if isinstance(file_path, str)
            else file_path.suffix.lower()
        )
    elif file_name:
        suffix = Path(file_name).suffix.lower()

    if suffix:
        return ext_map.get(suffix, "application/octet-stream")

    return "application/octet-stream"


def is_supported_type(mime_type: str) -> bool:
    """Check if a MIME type is supported for multimodal input."""
    return (
        mime_type in SUPPORTED_IMAGE_TYPES
        or mime_type in SUPPORTED_AUDIO_TYPES
        or mime_type in SUPPORTED_VIDEO_TYPES
        or mime_type in SUPPORTED_DOCUMENT_TYPES
    )


def encode_bytes_to_base64(data: bytes) -> str:
    """Encode bytes to base64 string."""
    return base64.b64encode(data).decode("utf-8")


async def _extract_audio_from_video(
    video_bytes: bytes, source_label: str
) -> bytes | None:
    """Extract the audio track from a video file using ffmpeg.

    Gemini tends to focus on the visual track when a video is sent as a single
    inline_data blob, so we extract the audio separately and send it alongside
    the video to ensure the model attends to both modalities.

    Returns raw OGG Opus bytes, or None if extraction fails or there is no
    audio track.
    """
    import asyncio
    import subprocess
    import tempfile
    import os

    tmp_in = None
    tmp_out = None
    try:
        # Write video bytes to a temp file (ffmpeg needs seekable input)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(video_bytes)
            tmp_in = f.name

        tmp_out = tmp_in + ".audio.ogg"

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            tmp_in,
            "-vn",  # discard video
            "-c:a",
            "libopus",  # encode to Opus
            "-b:a",
            "64k",  # moderate bitrate — keeps size small
            "-ac",
            "1",  # mono
            "-ar",
            "16000",  # 16 kHz
            tmp_out,
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            stderr_text = stderr.decode(errors="replace") if stderr else ""
            # "does not contain any stream" means no audio track — not an error
            if "does not contain any stream" in stderr_text:
                log_debug(f"[multimodal] No audio track in video ({source_label})")
            else:
                log_debug(
                    f"[multimodal] ffmpeg audio extraction failed for {source_label}: {stderr_text[:300]}"
                )
            return None

        if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
            log_debug(
                f"[multimodal] Audio extraction produced empty output for {source_label}"
            )
            return None

        with open(tmp_out, "rb") as f:
            audio_bytes = f.read()

        log_debug(
            f"[multimodal] Extracted audio track from {source_label}: {len(audio_bytes)} bytes"
        )
        return audio_bytes

    except FileNotFoundError:
        log_debug("[multimodal] ffmpeg not available — skipping video audio extraction")
        return None
    except Exception as e:
        log_warning(f"[multimodal] Audio extraction error for {source_label}: {e}")
        return None
    finally:
        # Clean up temp files
        for p in (tmp_in, tmp_out):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


def _split_mjpeg_stream(data: bytes) -> list[bytes]:
    """Split a concatenated MJPEG stream into individual JPEG frames.

    ffmpeg ``image2pipe`` outputs each frame as a complete JPEG back-to-back.
    We split on the SOI marker (``FF D8``) that begins each frame.
    """
    SOI = b"\xff\xd8"
    frames: list[bytes] = []
    start = data.find(SOI)
    while start != -1:
        next_start = data.find(SOI, start + 2)
        if next_start == -1:
            frames.append(data[start:])
        else:
            frames.append(data[start:next_start])
        start = next_start
    return frames


def _make_wav_header(
    data_size: int,
    sample_rate: int = 16000,
    channels: int = 1,
    bits_per_sample: int = 16,
) -> bytes:
    """Build a 44-byte RIFF/WAV header for raw PCM data."""
    import struct

    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,  # sub-chunk size
        1,  # PCM format
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )


async def _probe_video_info(tmp_path: str) -> dict[str, float] | None:
    """Use *ffprobe* to extract duration, resolution and native FPS.

    Returns ``{"duration": float, "width": int, "height": int, "native_fps": float}``
    or ``None`` on any failure.
    """
    import asyncio
    import json as _json
    import subprocess

    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-select_streams",
            "v:0",
            tmp_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0 or not stdout:
            return None

        info = _json.loads(stdout.decode(errors="replace"))

        # Duration — prefer format-level, fall back to stream-level
        duration = 0.0
        fmt = info.get("format", {})
        if fmt.get("duration"):
            duration = float(fmt["duration"])

        width = 0
        height = 0
        native_fps = 0.0
        streams = info.get("streams", [])
        for s in streams:
            if s.get("codec_type") == "video":
                width = int(s.get("width", 0))
                height = int(s.get("height", 0))
                # r_frame_rate is a fraction like "30/1"
                rfr = s.get("r_frame_rate", "0/1")
                parts = rfr.split("/")
                if len(parts) == 2 and int(parts[1]) != 0:
                    native_fps = float(int(parts[0]) / int(parts[1]))
                if not duration and s.get("duration"):
                    duration = float(s["duration"])
                break

        if duration <= 0:
            return None

        return {
            "duration": duration,
            "width": width,
            "height": height,
            "native_fps": native_fps,
        }
    except (FileNotFoundError, OSError):
        log_debug("[multimodal] ffprobe not available — skipping video probe")
        return None
    except Exception as e:
        log_debug(f"[multimodal] ffprobe failed: {e}")
        return None


# ── Token-budget constants for dynamic FPS ──────────────────────────────────
_TOKENS_PER_FRAME: int = 1120  # Gemini 3 default media_resolution
_TOKENS_PER_AUDIO_SEC: int = 25  # empirical from API logs
_TOKENS_PER_MARKER: int = 20  # approximate per-section text marker
_MIN_FPS: float = 0.5  # below this, temporal coherence breaks
_MAX_FPS: float = 5.0  # diminishing returns above this
_MAX_FRAME_WIDTH: int = 1024  # limits JPEG byte size; Gemini 3 tokens unaffected


def compute_optimal_video_params(
    duration_s: float,
    token_budget: int,
    tokens_per_frame: int = _TOKENS_PER_FRAME,
    tokens_per_audio_sec: int = _TOKENS_PER_AUDIO_SEC,
    tokens_per_marker: int = _TOKENS_PER_MARKER,
) -> tuple[float, int]:
    """Compute the best extraction FPS to fill *token_budget* for a video.

    Returns ``(fps, max_frames)`` — both clamped to sane ranges.
    """
    import math

    if duration_s <= 0:
        return (2.0, 4)  # safe fallback

    # Audio tokens scale linearly with duration
    audio_tokens = math.ceil(duration_s) * tokens_per_audio_sec

    # Estimate marker overhead: 1 marker per second of video + 1 preamble + 1 end marker
    n_seconds = math.ceil(duration_s)
    marker_tokens = (n_seconds + 2) * tokens_per_marker

    available = token_budget - audio_tokens - marker_tokens
    if available <= 0:
        return (_MIN_FPS, max(int(duration_s * _MIN_FPS), 1))

    max_frames = max(available // tokens_per_frame, 1)
    raw_fps = max_frames / duration_s
    clamped_fps = max(_MIN_FPS, min(raw_fps, _MAX_FPS))

    # Re-derive frame count from clamped fps
    final_frames = max(int(clamped_fps * duration_s), 1)

    return (round(clamped_fps, 2), final_frames)


async def decompose_video_to_frames_and_audio(
    video_bytes: bytes,
    source_label: str,
    fps: float = 2,
    *,
    token_budget: int | None = None,
    tokens_per_frame: int = _TOKENS_PER_FRAME,
) -> list[dict] | None:
    """Decompose a video into temporally-aligned frame + audio chunk pairs.

    When *token_budget* is provided the function probes the video with
    ``ffprobe`` and dynamically computes the best extraction FPS so that the
    total multimodal token cost stays just within the budget.

    Extracts visual frames at *fps* frames-per-second and splits the audio
    track into 1-second WAV chunks so that downstream engines can interleave
    them.  This lets the model "see and hear" each second of the video in
    parallel with explicit temporal grounding.

    All heavy output is piped through memory — only a single temporary file
    is written for the input video (MP4 demuxing requires seeking).

    Returns a list of per-second dicts::

        [
            {
                "ts": 0.0,
                "frames_b64": ["<b64>", "<b64>"],   # *fps* frames
                "audio_b64": "<b64>" | None,         # 1-second WAV chunk
            },
            ...
        ]

    Returns ``None`` on any failure (caller should fall back to blob
    behaviour).
    """
    import asyncio
    import subprocess
    import tempfile
    import os

    # MP4 demuxing requires a seekable input — one temp file is unavoidable.
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".mp4", prefix="synth_vdec_")
        os.write(fd, video_bytes)
        os.close(fd)

        # --- Dynamic FPS: probe video and compute optimal params ----------------
        scale_width = 640  # default fallback
        if token_budget is not None:
            vinfo = await _probe_video_info(tmp_path)
            if vinfo and vinfo["duration"] > 0:
                computed_fps, _ = compute_optimal_video_params(
                    vinfo["duration"],
                    token_budget,
                    tokens_per_frame=tokens_per_frame,
                )
                fps = computed_fps
                # Scale up to min(native, MAX_FRAME_WIDTH) — Gemini 3 token
                # cost is fixed regardless of pixel size, so bigger = better.
                if vinfo["width"] > 0:
                    scale_width = min(vinfo["width"], _MAX_FRAME_WIDTH)
                log_debug(
                    f"[multimodal] Dynamic video params for {source_label}: "
                    f"fps={fps}, scale_width={scale_width}, "
                    f"duration={vinfo['duration']:.1f}s, "
                    f"budget={token_budget} tokens"
                )

        # --- Extract frames via image2pipe (all output to stdout) ---------------
        frame_cmd = [
            "ffmpeg",
            "-y",
            "-i",
            tmp_path,
            "-vf",
            f"fps={fps},scale={scale_width}:-1",
            "-q:v",
            "5",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        fproc = await asyncio.create_subprocess_exec(
            *frame_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        frame_stdout, frame_stderr = await fproc.communicate()

        if fproc.returncode != 0:
            log_debug(
                f"[multimodal] Frame extraction failed for {source_label}: "
                f"{(frame_stderr.decode(errors='replace'))[:300]}"
            )
            return None

        frame_bytes_list = _split_mjpeg_stream(frame_stdout)
        if not frame_bytes_list:
            log_debug(f"[multimodal] No frames extracted for {source_label}")
            return None

        # --- Extract audio as raw PCM to stdout ---------------------------------
        # 16 kHz, mono, s16le → 32 000 bytes per second
        SAMPLE_RATE = 16000
        BYTES_PER_SECOND = SAMPLE_RATE * 1 * 2  # mono, 16-bit

        audio_cmd = [
            "ffmpeg",
            "-y",
            "-i",
            tmp_path,
            "-vn",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "pipe:1",
        ]
        aproc = await asyncio.create_subprocess_exec(
            *audio_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        audio_stdout, audio_stderr = await aproc.communicate()

        # Split raw PCM into 1-second chunks and wrap in WAV headers
        audio_chunks: list[bytes] = []
        has_audio = aproc.returncode == 0 and len(audio_stdout) > 0
        if has_audio:
            offset = 0
            while offset < len(audio_stdout):
                chunk_pcm = audio_stdout[offset : offset + BYTES_PER_SECOND]
                wav_header = _make_wav_header(len(chunk_pcm), sample_rate=SAMPLE_RATE)
                audio_chunks.append(wav_header + chunk_pcm)
                offset += BYTES_PER_SECOND
        else:
            stderr_text = audio_stderr.decode(errors="replace") if audio_stderr else ""
            if "does not contain any stream" in stderr_text:
                log_debug(f"[multimodal] No audio track in {source_label}, frames-only")
            else:
                log_debug(
                    f"[multimodal] Audio extraction failed for {source_label}: "
                    f"{stderr_text[:300]}"
                )

        # --- Group frames by actual timestamp and pair with audio chunks --------
        # Each frame's real timestamp is frame_index / fps.  We bucket frames
        # into 1-second windows so they align with the 1-second audio chunks.

        total_frames = len(frame_bytes_list)

        # Compute actual timestamp for every extracted frame
        frame_ts = [i / fps for i in range(total_frames)]

        # Total seconds = max of (last frame timestamp + 1, audio chunk count)
        last_frame_sec = int(frame_ts[-1]) + 1 if frame_ts else 0
        total_seconds = max(last_frame_sec, len(audio_chunks))
        result: list[dict] = []

        for sec_idx in range(total_seconds):
            sec_start = float(sec_idx)
            sec_end = sec_start + 1.0

            # Gather frames whose timestamp falls in [sec_start, sec_end)
            sec_frames = [
                frame_bytes_list[i]
                for i, ts in enumerate(frame_ts)
                if sec_start <= ts < sec_end
            ]

            audio_chunk = audio_chunks[sec_idx] if sec_idx < len(audio_chunks) else None

            # Only emit a group if there are frames or audio for this second
            if sec_frames or audio_chunk:
                result.append(
                    {
                        "ts": sec_start,
                        "frames_b64": [encode_bytes_to_base64(fb) for fb in sec_frames],
                        "audio_b64": (
                            encode_bytes_to_base64(audio_chunk) if audio_chunk else None
                        ),
                    }
                )

        log_debug(
            f"[multimodal] Decomposed {source_label}: {total_frames} frames, "
            f"{len(audio_chunks)} audio chunks, {total_seconds} seconds "
            f"(in-memory, single temp input)"
        )
        return result

    except FileNotFoundError:
        log_debug("[multimodal] ffmpeg not available — skipping video decomposition")
        return None
    except Exception as e:
        log_warning(f"[multimodal] Video decomposition error for {source_label}: {e}")
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


async def extract_multimodal_from_telegram(
    bot: Any,
    message: Any,
    image_processor: Any | None = None,
) -> list[dict]:
    """Extract multimodal attachments from a Telegram message.

    Args:
        bot: The Telegram bot instance (for downloading files)
        message: The Telegram message object
        image_processor: Optional ImageProcessor for whitelist checking

    Returns:
        List of attachment dicts with keys: {mime_type, data, filename}
    """
    attachments = []

    try:
        chat_id = getattr(message, "chat", None)
        if chat_id:
            chat_id = getattr(chat_id, "id", chat_id)

        # Check whitelist if processor provided and this is a group
        chat_type = getattr(getattr(message, "chat", None), "type", "private")
        is_group = chat_type in ("group", "supergroup")

        if is_group and image_processor:
            if not image_processor.should_process_image(chat_id):
                log_debug(
                    f"[multimodal] Skipping attachments for non-whitelisted group {chat_id}"
                )
                return []

        photos = getattr(message, "photo", None)
        document = getattr(message, "document", None)
        audio = getattr(message, "audio", None)
        voice = getattr(message, "voice", None)
        video = getattr(message, "video", None)
        video_note = getattr(message, "video_note", None)
        sticker = getattr(message, "sticker", None)

        if not any([photos, document, sticker]):
            reply_msg = getattr(message, "reply_to_message", None)
            if reply_msg is not None:
                photos = photos or getattr(reply_msg, "photo", None)
                document = document or getattr(reply_msg, "document", None)
                sticker = sticker or getattr(reply_msg, "sticker", None)

        # Handle photos (Telegram sends multiple sizes, get largest)
        if photos:
            photo = photos[-1]  # Last is largest
            try:
                file = await bot.get_file(photo.file_id)
                file_bytes = await file.download_as_bytearray()

                attachments.append(
                    {
                        "mime_type": "image/jpeg",  # Telegram photos are always JPEG
                        "data": encode_bytes_to_base64(bytes(file_bytes)),
                        "filename": f"photo_{photo.file_unique_id}.jpg",
                    }
                )
                log_debug(
                    f"[multimodal] Extracted Telegram photo: {photo.file_unique_id}"
                )
            except Exception as e:
                log_warning(f"[multimodal] Failed to download Telegram photo: {e}")

        # Handle documents (PDFs, etc.)
        if document:
            doc = document
            mime_type = doc.mime_type or get_mime_type(None, doc.file_name)

            if is_supported_type(mime_type):
                try:
                    file = await bot.get_file(doc.file_id)
                    file_bytes = await file.download_as_bytearray()

                    attachments.append(
                        {
                            "mime_type": mime_type,
                            "data": encode_bytes_to_base64(bytes(file_bytes)),
                            "filename": doc.file_name
                            or f"document_{doc.file_unique_id}",
                        }
                    )
                    log_debug(
                        f"[multimodal] Extracted Telegram document: {doc.file_name}"
                    )
                except Exception as e:
                    log_warning(
                        f"[multimodal] Failed to download Telegram document: {e}"
                    )
            else:
                log_debug(
                    f"[multimodal] Skipping unsupported document type: {mime_type}"
                )

        # Handle audio files
        if audio:
            mime_type = audio.mime_type or get_mime_type(None, audio.file_name)

            if is_supported_type(mime_type):
                try:
                    file = await bot.get_file(audio.file_id)
                    file_bytes = await file.download_as_bytearray()

                    attachments.append(
                        {
                            "mime_type": mime_type,
                            "data": encode_bytes_to_base64(bytes(file_bytes)),
                            "filename": audio.file_name
                            or f"audio_{audio.file_unique_id}",
                        }
                    )
                    log_debug(
                        f"[multimodal] Extracted Telegram audio: {audio.file_name}"
                    )
                except Exception as e:
                    log_warning(f"[multimodal] Failed to download Telegram audio: {e}")

        # Handle voice messages
        if voice:
            mime_type = voice.mime_type or "audio/ogg"  # Telegram voice is usually OGG

            if is_supported_type(mime_type):
                try:
                    file = await bot.get_file(voice.file_id)
                    file_bytes = await file.download_as_bytearray()

                    attachments.append(
                        {
                            "mime_type": mime_type,
                            "data": encode_bytes_to_base64(bytes(file_bytes)),
                            "filename": f"voice_{voice.file_unique_id}.ogg",
                        }
                    )
                    log_debug("[multimodal] Extracted Telegram voice message")
                except Exception as e:
                    log_warning(f"[multimodal] Failed to download Telegram voice: {e}")

        # Handle video files
        if video:
            mime_type = video.mime_type or "video/mp4"  # Default to mp4

            if is_supported_type(mime_type):
                # Check file size - Telegram allows up to 20MB for bots to download
                # Gemini inline supports up to 20MB total request size
                file_size = getattr(video, "file_size", 0) or 0
                if file_size > 20 * 1024 * 1024:  # 20MB limit
                    log_warning(
                        f"[multimodal] Video too large for inline upload: {file_size} bytes"
                    )
                else:
                    try:
                        file = await bot.get_file(video.file_id)
                        file_bytes = bytes(await file.download_as_bytearray())

                        caption = getattr(message, "caption", None) or ""
                        duration = getattr(video, "duration", 0) or 0
                        width = getattr(video, "width", 0) or 0
                        height = getattr(video, "height", 0) or 0
                        attachments.append(
                            {
                                "mime_type": mime_type,
                                "data": encode_bytes_to_base64(file_bytes),
                                "filename": video.file_name
                                or f"video_{video.file_unique_id}.mp4",
                                "caption": caption,
                                "media_metadata": {
                                    "type": "video",
                                    "duration": duration,
                                    "width": width,
                                    "height": height,
                                    "file_size": file_size,
                                    "has_audio": True,
                                },
                            }
                        )
                        log_debug(
                            f"[multimodal] Extracted Telegram video: {video.file_unique_id} ({mime_type})"
                        )

                        # Extract audio track separately so the model attends to
                        # both visual and audio content (Gemini under-weights audio
                        # when it is embedded inside a video container).
                        audio_bytes = await _extract_audio_from_video(
                            file_bytes, f"video_{video.file_unique_id}"
                        )
                        if audio_bytes:
                            attachments.append(
                                {
                                    "mime_type": "audio/ogg",
                                    "data": encode_bytes_to_base64(audio_bytes),
                                    "filename": f"video_{video.file_unique_id}_audio.ogg",
                                }
                            )
                    except Exception as e:
                        log_warning(
                            f"[multimodal] Failed to download Telegram video: {e}"
                        )
            else:
                log_debug(f"[multimodal] Skipping unsupported video type: {mime_type}")

        # Handle video notes (round videos) - these are small circular videos
        if video_note:
            # Video notes are always mp4, and typically small (up to 1 minute)
            file_size = getattr(video_note, "file_size", 0) or 0
            if file_size <= 20 * 1024 * 1024:  # 20MB limit
                try:
                    file = await bot.get_file(video_note.file_id)
                    file_bytes = bytes(await file.download_as_bytearray())

                    vn_duration = getattr(video_note, "duration", 0) or 0
                    vn_length = getattr(video_note, "length", 0) or 0
                    attachments.append(
                        {
                            "mime_type": "video/mp4",
                            "data": encode_bytes_to_base64(file_bytes),
                            "filename": f"video_note_{video_note.file_unique_id}.mp4",
                            "caption": "",
                            "media_metadata": {
                                "type": "video_note",
                                "duration": vn_duration,
                                "width": vn_length,
                                "height": vn_length,
                                "file_size": file_size,
                                "has_audio": True,
                            },
                        }
                    )
                    log_debug(
                        f"[multimodal] Extracted Telegram video note: {video_note.file_unique_id}"
                    )

                    # Extract audio track separately (same reasoning as video above)
                    audio_bytes = await _extract_audio_from_video(
                        file_bytes, f"video_note_{video_note.file_unique_id}"
                    )
                    if audio_bytes:
                        attachments.append(
                            {
                                "mime_type": "audio/ogg",
                                "data": encode_bytes_to_base64(audio_bytes),
                                "filename": f"video_note_{video_note.file_unique_id}_audio.ogg",
                            }
                        )
                except Exception as e:
                    log_warning(
                        f"[multimodal] Failed to download Telegram video note: {e}"
                    )
            else:
                log_debug(f"[multimodal] Video note too large: {file_size} bytes")

        # Handle stickers.
        # Static stickers are delivered as WebP images. Animated/video stickers
        # cannot be rendered as static images directly, so we fall back to the
        # thumbnail Telegram exposes on the sticker object. When even the thumb
        # is missing, we emit a text placeholder so the LLM knows an animated
        # sticker was shared instead of silently dropping it.
        if sticker:
            is_animated = bool(getattr(sticker, "is_animated", False))
            is_video = bool(getattr(sticker, "is_video", False))
            sticker_emoji = getattr(sticker, "emoji", None)
            sticker_set_name = getattr(sticker, "set_name", None)

            if is_animated or is_video:
                thumb = getattr(sticker, "thumb", None)
                thumb_file_id = getattr(thumb, "file_id", None) if thumb else None
                if thumb_file_id:
                    try:
                        file = await bot.get_file(thumb_file_id)
                        file_bytes = await file.download_as_bytearray()
                        attachments.append(
                            {
                                "mime_type": "image/webp",
                                "data": encode_bytes_to_base64(bytes(file_bytes)),
                                "filename": f"sticker_{sticker.file_unique_id}_thumb.webp",
                                "is_sticker": True,
                                "media_metadata": {
                                    "type": "sticker",
                                    "file_unique_id": sticker.file_unique_id,
                                    "animated": is_animated,
                                    "video": is_video,
                                    "thumbnail": True,
                                    "emoji": sticker_emoji,
                                    "set_name": sticker_set_name,
                                },
                            }
                        )
                        log_debug(
                            f"[multimodal] Extracted Telegram sticker thumb: {thumb_file_id}"
                        )
                    except Exception as e:
                        log_warning(
                            f"[multimodal] Failed to download sticker thumb: {e}"
                        )
                else:
                    attachments.append(
                        {
                            "mime_type": "text/plain",
                            "data": encode_bytes_to_base64(
                                (
                                    "The user sent an "
                                    f"{'animated' if is_animated else 'video'} "
                                    "sticker that cannot be displayed as a static "
                                    "image."
                                ).encode("utf-8")
                            ),
                            "filename": "sticker.txt",
                            "is_sticker": True,
                            "media_metadata": {
                                "type": "sticker",
                                "file_unique_id": sticker.file_unique_id,
                                "animated": is_animated,
                                "video": is_video,
                                "viewable": False,
                                "emoji": sticker_emoji,
                                "set_name": sticker_set_name,
                            },
                        }
                    )
                    log_debug(
                        f"[multimodal] Animated/video sticker without thumb: "
                        f"{sticker.file_unique_id}"
                    )
            else:
                try:
                    file = await bot.get_file(sticker.file_id)
                    file_bytes = await file.download_as_bytearray()

                    attachments.append(
                        {
                            "mime_type": "image/webp",
                            "data": encode_bytes_to_base64(bytes(file_bytes)),
                            "filename": f"sticker_{sticker.file_unique_id}.webp",
                            "is_sticker": True,
                            "media_metadata": {
                                "type": "sticker",
                                "file_unique_id": sticker.file_unique_id,
                                "emoji": sticker_emoji,
                                "set_name": sticker_set_name,
                            },
                        }
                    )
                    log_debug(
                        f"[multimodal] Extracted Telegram sticker: "
                        f"{sticker.file_unique_id}"
                    )
                except Exception as e:
                    log_warning(
                        f"[multimodal] Failed to download Telegram sticker: {e}"
                    )

    except Exception as e:
        log_error(f"[multimodal] Error extracting Telegram attachments: {e}")

    return attachments


async def extract_multimodal_from_discord(
    message: Any,
    image_processor: Any | None = None,
) -> list[dict]:
    """Extract multimodal attachments from a Discord message.

    Args:
        message: The Discord message object
        image_processor: Optional ImageProcessor for whitelist checking

    Returns:
        List of attachment dicts with keys: {mime_type, data, filename}
    """
    attachments = []

    try:
        # Check whitelist if processor provided and this is a guild (server) channel
        guild = getattr(message, "guild", None)
        is_group = guild is not None

        if is_group and image_processor:
            channel_id = getattr(message.channel, "id", None)
            if channel_id and not image_processor.should_process_image(channel_id):
                log_debug(
                    f"[multimodal] Skipping attachments for non-whitelisted channel {channel_id}"
                )
                return []

        # Process Discord attachments
        for attachment in getattr(message, "attachments", []):
            content_type = getattr(attachment, "content_type", None)
            filename = getattr(attachment, "filename", "unknown")

            # Determine MIME type
            mime_type = content_type or get_mime_type(None, filename)

            if not is_supported_type(mime_type):
                log_debug(
                    f"[multimodal] Skipping unsupported Discord attachment: {mime_type}"
                )
                continue

            try:
                # Download the attachment
                file_bytes = await attachment.read()

                attachments.append(
                    {
                        "mime_type": mime_type,
                        "data": encode_bytes_to_base64(file_bytes),
                        "filename": filename,
                    }
                )
                log_debug(
                    f"[multimodal] Extracted Discord attachment: {filename} ({mime_type})"
                )
            except Exception as e:
                log_warning(
                    f"[multimodal] Failed to download Discord attachment {filename}: {e}"
                )

        # Handle Discord stickers
        for sticker in getattr(message, "stickers", []):
            sticker_format = getattr(sticker, "format", None)
            sticker_name = getattr(sticker, "name", "unknown")
            sticker_id = getattr(sticker, "id", None)

            if sticker_format is not None:
                try:
                    from discord.sticker import StickerFormatType

                    if sticker_format in (
                        StickerFormatType.lottie,
                        StickerFormatType.gif,
                        StickerFormatType.apng,
                    ):
                        log_debug(
                            f"[multimodal] Skipping unsupported Discord sticker "
                            f"format: {sticker_format} ({sticker_name})"
                        )
                        attachments.append(
                            {
                                "mime_type": "text/plain",
                                "data": encode_bytes_to_base64(
                                    f"A sticker named '{sticker_name}' was sent, "
                                    f"but its format ({sticker_format}) cannot be "
                                    f"displayed as a static image.".encode("utf-8")
                                ),
                                "filename": "sticker.txt",
                                "is_sticker": True,
                                "media_metadata": {
                                    "type": "sticker",
                                    "name": sticker_name,
                                    "id": sticker_id,
                                    "format": str(sticker_format),
                                    "viewable": False,
                                },
                            }
                        )
                        continue
                except ImportError:
                    pass

            sticker_url = getattr(sticker, "url", None)
            if not sticker_url and sticker_id:
                sticker_url = f"https://cdn.discordapp.com/stickers/{sticker_id}.png"

            if not sticker_url:
                log_debug(
                    f"[multimodal] Skipping Discord sticker without URL: {sticker_name}"
                )
                continue

            try:
                import httpx

                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(sticker_url)
                    resp.raise_for_status()
                    sticker_bytes = resp.content

                attachments.append(
                    {
                        "mime_type": "image/png",
                        "data": encode_bytes_to_base64(sticker_bytes),
                        "filename": f"sticker_{sticker_id}_{sticker_name}.png",
                        "is_sticker": True,
                        "media_metadata": {
                            "type": "sticker",
                            "name": sticker_name,
                            "id": sticker_id,
                            "format": str(sticker_format) if sticker_format else "png",
                            "viewable": True,
                        },
                    }
                )
                log_debug(
                    f"[multimodal] Extracted Discord sticker: {sticker_name} ({sticker_id})"
                )
            except Exception as e:
                log_warning(
                    f"[multimodal] Failed to download Discord sticker "
                    f"{sticker_name}: {e}"
                )
                attachments.append(
                    {
                        "mime_type": "text/plain",
                        "data": encode_bytes_to_base64(
                            f"A sticker named '{sticker_name}' was sent, "
                            f"but it could not be downloaded for analysis.".encode(
                                "utf-8"
                            )
                        ),
                        "filename": "sticker.txt",
                        "is_sticker": True,
                        "media_metadata": {
                            "type": "sticker",
                            "name": sticker_name,
                            "id": sticker_id,
                            "format": str(sticker_format)
                            if sticker_format
                            else "unknown",
                            "viewable": False,
                        },
                    }
                )

        # Handle embeds with images (optional - these are usually previews)
        for embed in getattr(message, "embeds", []):
            if embed.image:
                # Embeds usually link to external images, skip for now
                log_debug("[multimodal] Skipping embed image (external URL)")

    except Exception as e:
        log_error(f"[multimodal] Error extracting Discord attachments: {e}")

    return attachments
