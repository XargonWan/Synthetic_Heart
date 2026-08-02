"""Shared helpers for validating and classifying outbound file attachments.

Interfaces (Telegram, Discord, Matrix, ...) use these helpers when Synth sends a
file to a user or channel. Files must live inside the agent filesystem sandbox
(the same roots used by ``plugins/agent_plugin.py``) so that a compromised or
hallucinated action cannot exfiltrate arbitrary host files.

The sandbox roots come from, in order of precedence:

* ``AGENT_FS_ROOTS`` — a colon-separated list of absolute roots, or
* ``[AGENT_FS_ROOT | "/app", SYNTH_LOG_DIR | "/app/logs"]`` as the default.

This mirrors :meth:`plugins.agent_plugin.AgentPlugin._allowed_roots` /
``_resolve_safe_path`` but is a standalone module so every interface can share a
single validation path without importing the plugin.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

# Media kinds returned by :func:`classify_media`.
MEDIA_IMAGE = "image"
MEDIA_VIDEO = "video"
MEDIA_AUDIO = "audio"
MEDIA_DOCUMENT = "document"

# Extension fallbacks for cases where ``mimetypes`` cannot guess a type.
_AUDIO_EXTS = {
    ".mp3",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".flac",
    ".m4a",
    ".aac",
    ".wma",
    ".weba",
}
_VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".wmv",
}
_IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
    ".svg",
    ".heic",
    ".heif",
}


def allowed_file_roots() -> list[Path]:
    """Return the resolved filesystem roots outbound files must live inside."""
    roots_raw = os.getenv("AGENT_FS_ROOTS")
    if roots_raw:
        roots = [p.strip() for p in roots_raw.split(":") if p.strip()]
    else:
        roots = [
            os.getenv("AGENT_FS_ROOT", "/app"),
            os.getenv("SYNTH_LOG_DIR", "/app/logs"),
        ]

    out: list[Path] = []
    for root in roots:
        try:
            out.append(Path(root).resolve())
        except Exception:
            continue
    return out


def resolve_safe_outbound_path(raw_path: str) -> tuple[Path | None, str | None]:
    """Resolve ``raw_path`` and ensure it stays inside an allowed root.

    Returns ``(resolved_path, None)`` on success or ``(None, error_message)`` if
    the path is missing, invalid, or escapes the sandbox. Relative paths are
    resolved against the first allowed root. The returned path is guaranteed to
    exist and to be a regular file.
    """
    if not raw_path or not str(raw_path).strip():
        return None, "Missing path"

    p = Path(str(raw_path).strip())
    if not p.is_absolute():
        roots = allowed_file_roots()
        if not roots:
            return None, "No allowed roots configured"
        p = roots[0] / p

    try:
        resolved = p.resolve()
    except Exception as exc:
        return None, f"Invalid path: {exc}"

    inside_root = False
    for root in allowed_file_roots():
        try:
            resolved.relative_to(root)
            inside_root = True
            break
        except ValueError:
            continue

    if not inside_root:
        return None, "Path is outside allowed roots"

    if not resolved.exists():
        return None, "File does not exist"
    if not resolved.is_file():
        return None, "Path is not a regular file"

    return resolved, None


def guess_mime_type(path: Path | str) -> str:
    """Return a best-effort MIME type for ``path``.

    Falls back to ``application/octet-stream`` when the type cannot be guessed.
    """
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def classify_media(path: Path | str) -> str:
    """Classify ``path`` into one of image/video/audio/document.

    Uses the MIME type first, then an extension fallback. Anything that is not a
    recognised image/video/audio type is treated as a generic ``document``.
    """
    mime = guess_mime_type(path)
    if mime.startswith("image/"):
        return MEDIA_IMAGE
    if mime.startswith("video/"):
        return MEDIA_VIDEO
    if mime.startswith("audio/"):
        return MEDIA_AUDIO

    ext = Path(path).suffix.lower()
    if ext in _AUDIO_EXTS:
        return MEDIA_AUDIO
    if ext in _VIDEO_EXTS:
        return MEDIA_VIDEO
    if ext in _IMAGE_EXTS:
        return MEDIA_IMAGE

    return MEDIA_DOCUMENT
