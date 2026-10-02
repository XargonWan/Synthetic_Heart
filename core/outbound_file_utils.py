"""Shared helpers for validating and classifying outbound file attachments.

Interfaces (Telegram, Discord, Matrix, ...) use these helpers when Synth sends a
file to a user or channel. Files must live inside the agent filesystem sandbox
(the same roots used by ``plugins/agent_plugin.py``) so that a compromised or
hallucinated action cannot exfiltrate arbitrary host files.

The sandbox roots come from, in order of precedence:

* ``AGENT_FS_ROOTS`` — a platform-path-separated list of absolute roots, or
* ``AGENT_FS_ROOT`` and ``SYNTH_LOG_DIR``, or
* the application root and its ``logs`` directory.

Resolution lives in :func:`core.app_paths.agent_fs_roots`, so the container's
application layout and a native install (where that path does not exist) behave
the same way.

This mirrors :meth:`plugins.agent_plugin.AgentPlugin._allowed_roots` /
``_resolve_safe_path`` but is a standalone module so every interface can share a
single validation path without importing the plugin.
"""

from __future__ import annotations

import fnmatch
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


# --- Credential material is never deliverable ------------------------------
# Files that are never attached, even from inside an allowed root. This is
# defence in depth BEHIND the roots, not a boundary of its own: it matches the
# resolved name and the directory parts, so a copy under an unrelated name
# passes. It exists because an outbound root is normally the application tree,
# and a bare checkout of that tree contains the environment file (bot tokens,
# database and service passwords) while the container image does not, because
# `.dockerignore` excludes `.env`.
_DENIED_FILE_NAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".netrc",
        "_netrc",
        ".pgpass",
        ".my.cnf",
        ".git-credentials",
        ".htpasswd",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "secrets",
    }
)
_DENIED_NAME_PATTERNS: tuple[str, ...] = (
    "*.env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.ppk",
    "id_rsa.*",
    "id_dsa.*",
    "id_ecdsa.*",
    "id_ed25519.*",
    "credentials.*",
    "secrets.*",
    "*credentials.json",
    "*service-account*.json",
)
# Directories whose whole contents are credential material.
_DENIED_DIR_PARTS: frozenset[str] = frozenset(
    {".git", ".ssh", ".aws", ".gnupg", ".docker", ".kube"}
)

# Generated media the application itself writes, which the container keeps
# OUTSIDE the application tree on purpose (Vox's output is a mounted volume at
# /config/media/tts so clips survive a rebuild). Both halves must match: the
# directory the producer is configured with AND that producer's own filename
# pattern. A misconfigured directory therefore cannot be used to read unrelated
# files out of it, and a new engine writing a different name is refused here
# (visible in the interface log as "Path is outside allowed roots") rather than
# silently widening the sandbox.
_APP_GENERATED_MEDIA: tuple[tuple[str, str], ...] = (
    ("VOX_OUTPUT_DIR", "vox_*.wav"),
    ("TTS_OUTPUT_DIR", "tts_*.wav"),
)


def _split_roots(raw: str) -> list[str]:
    """Split an ``AGENT_FS_ROOTS`` string into roots.

    The documented separator is ``:``, but a plain ``raw.split(":")`` shreds a
    Windows drive letter: ``C:/Users/x/sandbox`` becomes ``["C", "/Users/x/sandbox"]``
    and the sandbox then resolves to ``<cwd>/C`` plus a drive-relative path — so
    the real file is rejected as "outside allowed roots" (and the same call
    PASSES when cwd happens to sit on the same drive, which is how it stayed
    hidden). A one-letter segment is therefore re-joined to the segment after it.
    """
    merged: list[str] = []
    pending: str | None = None
    for part in (p.strip() for p in raw.split(":")):
        if pending is not None:
            part = f"{pending}:{part}"
            pending = None
        if not part:
            continue
        if len(part) == 1 and part.isalpha():
            pending = part
            continue
        merged.append(part)
    if pending:
        merged.append(pending)
    return merged


def _is_denied_secret_path(resolved: Path) -> bool:
    """Return True when ``resolved`` is credential material (see the lists above)."""
    name = resolved.name.lower()
    if name in _DENIED_FILE_NAMES:
        return True
    if any(fnmatch.fnmatchcase(name, pat) for pat in _DENIED_NAME_PATTERNS):
        return True
    return any(part.lower() in _DENIED_DIR_PARTS for part in resolved.parts[:-1])


def _is_app_generated_media(resolved: Path) -> bool:
    """Return True for a clip the application itself generated moments ago.

    This is the only exemption from the sandbox roots. The file must sit
    DIRECTLY in the directory its producer is configured with and carry that
    producer's filename prefix (``vox_<epoch>.wav`` and ``vox_<turn>_<i>.wav``
    for streamed replies, ``tts_<epoch>.wav`` for the tts_lipsync producer).
    """
    for env_var, pattern in _APP_GENERATED_MEDIA:
        raw = (os.getenv(env_var) or "").strip()
        if not raw:
            continue
        try:
            media_dir = Path(raw).resolve()
        except Exception:
            continue
        if resolved.parent != media_dir:
            continue
        if fnmatch.fnmatchcase(resolved.name.lower(), pattern):
            return True
    return False


def allowed_file_roots() -> list[Path]:
    """Return the resolved filesystem roots outbound files must live inside."""
    roots_raw = os.getenv("AGENT_FS_ROOTS")
    if roots_raw:
        roots = _split_roots(roots_raw)
    else:
        # Default to the APPLICATION ROOT rather than the literal "/app". In the
        # container the app is at /app so the two are the same directory, but in
        # a bare checkout (a Windows dev tree, a venv install) "/app" does not
        # exist and every outbound attachment is rejected with "Path is outside
        # allowed roots": the text still arrives, the media is silently dropped.
        # An explicit AGENT_FS_ROOT / SYNTH_LOG_DIR still takes precedence.
        app_root = Path(__file__).resolve().parent.parent
        roots = [
            os.getenv("AGENT_FS_ROOT") or str(app_root),
            os.getenv("SYNTH_LOG_DIR") or str(app_root / "logs"),
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

    if not inside_root and not _is_app_generated_media(resolved):
        return None, "Path is outside allowed roots"

    if not resolved.exists():
        return None, "File does not exist"
    if not resolved.is_file():
        return None, "Path is not a regular file"
    if _is_denied_secret_path(resolved):
        return None, "Refusing to attach a credential or key file"

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
