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


# Supported MIME types (matching gemini_api.py)
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
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

        # Handle photos (Telegram sends multiple sizes, get largest)
        if message.photo:
            photo = message.photo[-1]  # Last is largest
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
        if message.document:
            doc = message.document
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
        if message.audio:
            audio = message.audio
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
        if message.voice:
            voice = message.voice
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
        if message.video:
            video = message.video
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

                        attachments.append(
                            {
                                "mime_type": mime_type,
                                "data": encode_bytes_to_base64(file_bytes),
                                "filename": video.file_name
                                or f"video_{video.file_unique_id}.mp4",
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
        if message.video_note:
            video_note = message.video_note
            # Video notes are always mp4, and typically small (up to 1 minute)
            file_size = getattr(video_note, "file_size", 0) or 0
            if file_size <= 20 * 1024 * 1024:  # 20MB limit
                try:
                    file = await bot.get_file(video_note.file_id)
                    file_bytes = bytes(await file.download_as_bytearray())

                    attachments.append(
                        {
                            "mime_type": "video/mp4",
                            "data": encode_bytes_to_base64(file_bytes),
                            "filename": f"video_note_{video_note.file_unique_id}.mp4",
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

        # Handle stickers (as images if static)
        if (
            message.sticker
            and not message.sticker.is_animated
            and not message.sticker.is_video
        ):
            sticker = message.sticker
            try:
                file = await bot.get_file(sticker.file_id)
                file_bytes = await file.download_as_bytearray()

                # Stickers are WebP format
                attachments.append(
                    {
                        "mime_type": "image/webp",
                        "data": encode_bytes_to_base64(bytes(file_bytes)),
                        "filename": f"sticker_{sticker.file_unique_id}.webp",
                    }
                )
                log_debug(
                    f"[multimodal] Extracted Telegram sticker: {sticker.file_unique_id}"
                )
            except Exception as e:
                log_warning(f"[multimodal] Failed to download Telegram sticker: {e}")

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

        # Handle embeds with images (optional - these are usually previews)
        for embed in getattr(message, "embeds", []):
            if embed.image:
                # Embeds usually link to external images, skip for now
                log_debug("[multimodal] Skipping embed image (external URL)")

    except Exception as e:
        log_error(f"[multimodal] Error extracting Discord attachments: {e}")

    return attachments
