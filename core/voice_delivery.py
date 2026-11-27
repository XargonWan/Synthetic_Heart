"""Delivering a spoken reply to a chat.

A reply that is spoken while a live call is open is played into the call. When it goes to the
chat instead (no call, or the call ended), it is a message, and the chat only takes some audio
formats as a *voice message*: Telegram wants Ogg/Opus, a bare WAV arrives as a file named
``vox_1795769074.wav``. Each interface declares the format its chats accept
(``InterfaceTraits.voice_format``); this module converts the clip to it with ``ffmpeg`` and sends
it with ``send_as_voice`` set, so an interface that tells a voice note from a file sends a voice
note.

Nothing is lost when the conversion cannot be made (no ``ffmpeg``, a clip it cannot read): the
original clip is sent as it is, as it was before.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

from core.interface_traits import traits_for
from core.logging_utils import log_debug, log_warning

#: format -> (file extension, ffmpeg arguments)
FORMATS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ogg_opus": (
        ".ogg",
        ("-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1"),
    ),
    "mp3": (".mp3", ("-c:a", "libmp3lame", "-q:a", "4", "-ac", "1")),
    "m4a": (".m4a", ("-c:a", "aac", "-b:a", "64k", "-ac", "1")),
}
CONVERT_TIMEOUT_SEC = 60


def _convert_blocking(source: Path, voice_format: str) -> Path | None:
    spec = FORMATS.get(voice_format)
    ffmpeg = shutil.which("ffmpeg")
    if spec is None or ffmpeg is None:
        if spec is not None:
            log_warning(
                "[voice_delivery] ffmpeg is not installed: sending the clip as it is"
            )
        return None
    extension, arguments = spec
    target = source.with_suffix(extension)
    if target == source:
        return None  # already in that container
    try:
        done = subprocess.run(
            [ffmpeg, "-y", "-i", str(source), "-vn", *arguments, str(target)],
            capture_output=True,
            timeout=CONVERT_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log_warning(f"[voice_delivery] conversion to {voice_format} failed: {exc!r}")
        target.unlink(missing_ok=True)
        return None
    if done.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        log_warning(
            f"[voice_delivery] ffmpeg could not make {voice_format}: "
            f"{done.stderr.decode(errors='replace')[-200:]}"
        )
        target.unlink(missing_ok=True)
        return None
    return target


async def convert_for(interface_name: str, audio_path: Path) -> Path | None:
    """The clip in the format ``interface_name``'s chats accept, or ``None`` when it needs no
    conversion or none could be made."""
    voice_format = traits_for(interface_name).voice_format
    if not voice_format:
        return None
    return await asyncio.to_thread(_convert_blocking, audio_path, voice_format)


async def send_speech(
    target: Any,
    interface_name: str,
    interface_path: str,
    audio_path: Path | str,
    caption: str | None,
) -> None:
    """Send ``audio_path`` to the chat at ``interface_path`` as a spoken message.

    The payload is the single positional argument, the one convention every interface shares
    (see ``VoxPlugin._dispatch``).
    """
    source = Path(audio_path)
    converted = await convert_for(interface_name, source)
    payload: dict[str, Any] = {
        "interface_path": interface_path,
        "media": [str(converted or source)],
        "text": caption,
        "send_as_voice": True,
    }
    try:
        await target.send_message(payload)
    finally:
        if converted is not None:
            log_debug(f"[voice_delivery] sent {converted.name} to {interface_path}")
            converted.unlink(missing_ok=True)
