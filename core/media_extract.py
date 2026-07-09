# core/media_extract.py
"""Media extraction helpers for video transcription.

Provides file-based (memory-friendly) helpers used by the video-transcription
recon plugin:

* :func:`extract_audio_from_video` — extract a video's audio track to a WAV
  file via ``ffmpeg`` (streamed on disk, never fully loaded into RAM).
* :func:`fetch_youtube` — download a YouTube video's existing subtitles when
  available (fast path, no STT), otherwise download the audio track for STT,
  via ``yt-dlp``.
* :func:`is_youtube_url` — structural (not keyword-based) check for a YouTube
  watch URL.

All temporary files are created under ``tmp/recon_media/`` and the caller is
responsible for cleaning them up (see :class:`YouTubeFetchResult`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from core.logging_utils import log_debug, log_warning

# Directory for transient downloads / extractions.
_MEDIA_TMP_DIR = os.path.join("tmp", "recon_media")

# Hosts recognised as YouTube. Matched structurally against the parsed URL host,
# never against free-form message text.
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


def _ensure_tmp_dir() -> str:
    os.makedirs(_MEDIA_TMP_DIR, exist_ok=True)
    return _MEDIA_TMP_DIR


def is_youtube_url(url: str) -> bool:
    """Return True if *url* is a structurally valid YouTube watch/share URL.

    This inspects the parsed URL host and path only; it does not scan arbitrary
    text for keywords. Callers should extract candidate URLs upstream (e.g. via
    the recon LLM) and pass them here for validation.
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return False
    if host == "youtu.be":
        # Short form: https://youtu.be/<id>
        return bool(parsed.path.strip("/"))
    if parsed.path == "/watch":
        return bool(parse_qs(parsed.query).get("v"))
    # /shorts/<id>, /embed/<id>, /live/<id>
    return any(parsed.path.startswith(p) for p in ("/shorts/", "/embed/", "/live/"))


def extract_audio_from_video(video_path: str, *, timeout: int = 300) -> str | None:
    """Extract a video's audio track to a 16 kHz mono WAV file.

    Uses ``ffmpeg`` with a file-based output (never loads the media into RAM),
    which keeps memory bounded for long videos.

    Args:
        video_path: Path to the source video (any container ffmpeg understands).
        timeout:    Max seconds to allow ffmpeg to run.

    Returns:
        Path to the extracted WAV file (under ``tmp/recon_media/``), or ``None``
        on failure. The caller must delete the file when done.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log_warning("[media_extract] ffmpeg not found; cannot extract audio.")
        return None
    if not os.path.exists(video_path):
        log_warning(f"[media_extract] Video not found: {video_path}")
        return None

    out_dir = _ensure_tmp_dir()
    out_fd, out_path = tempfile.mkstemp(suffix=".wav", dir=out_dir)
    os.close(out_fd)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                ffmpeg,
                "-y",
                "-i",
                video_path,
                "-vn",  # drop the video stream
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                out_path,
            ],
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            log_warning(
                "[media_extract] ffmpeg audio extraction failed "
                f"(rc={result.returncode}): "
                f"{result.stderr.decode('utf-8', 'replace')[:300]}"
            )
            _safe_remove(out_path)
            return None
        if os.path.getsize(out_path) == 0:
            log_warning("[media_extract] ffmpeg produced an empty audio file.")
            _safe_remove(out_path)
            return None
        return out_path
    except subprocess.TimeoutExpired:
        log_warning(
            f"[media_extract] ffmpeg audio extraction timed out after {timeout}s."
        )
        _safe_remove(out_path)
        return None
    except Exception as exc:
        log_warning(f"[media_extract] ffmpeg audio extraction raised: {exc!r}")
        _safe_remove(out_path)
        return None


@dataclass
class YouTubeFetchResult:
    """Result of a YouTube fetch.

    Exactly one of ``subtitle_text`` (fast path) or ``audio_path`` (STT path) is
    populated. ``temp_files`` lists paths the caller must delete when done.
    """

    title: str = ""
    duration_s: float | None = None
    subtitle_text: str | None = None
    audio_path: str | None = None
    temp_files: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        for path in self.temp_files:
            _safe_remove(path)
        self.temp_files = []


def _safe_remove(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def fetch_youtube(
    url: str,
    *,
    max_duration_s: int | None = None,
    prefer_subtitles: bool = True,
) -> YouTubeFetchResult | None:
    """Fetch a YouTube video's transcript source.

    Strategy:
        1. Probe metadata (title, duration). Reject if longer than
           ``max_duration_s``.
        2. If ``prefer_subtitles``, try to download existing subtitles
           (manual or auto-generated) — instant, no STT needed.
        3. Otherwise (or if no subtitles), download the best audio-only stream
           for downstream STT via Auris.

    Args:
        url:            A YouTube URL (validated by :func:`is_youtube_url`).
        max_duration_s: Skip videos longer than this (``None`` = no limit).
        prefer_subtitles: Use existing subtitles when available.

    Returns:
        A :class:`YouTubeFetchResult`, or ``None`` on failure. The caller must
        call :meth:`YouTubeFetchResult.cleanup` when finished.
    """
    if not is_youtube_url(url):
        log_debug(f"[media_extract] Not a YouTube URL, skipping: {url!r}")
        return None

    try:
        import yt_dlp
    except ImportError:
        log_warning("[media_extract] yt-dlp not installed; cannot fetch YouTube.")
        return None

    out_dir = _ensure_tmp_dir()

    # 1. Probe metadata without downloading.
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        log_warning(f"[media_extract] yt-dlp metadata probe failed: {exc!r}")
        return None

    title = str(info.get("title") or "").strip()
    duration = info.get("duration")
    duration_s = float(duration) if isinstance(duration, (int, float)) else None
    if (
        max_duration_s is not None
        and duration_s is not None
        and duration_s > max_duration_s
    ):
        log_warning(
            f"[media_extract] YouTube video too long "
            f"({duration_s:.0f}s > {max_duration_s}s): {title!r}"
        )
        return None

    result = YouTubeFetchResult(title=title, duration_s=duration_s)

    # 2. Fast path: existing subtitles.
    if prefer_subtitles:
        subtitle_text = _download_youtube_subtitles(url, out_dir, result)
        if subtitle_text:
            result.subtitle_text = subtitle_text
            return result

    # 3. Fallback: download audio for STT.
    audio_path = _download_youtube_audio(url, out_dir, result)
    if audio_path:
        result.audio_path = audio_path
        return result

    result.cleanup()
    return None


def _download_youtube_subtitles(
    url: str, out_dir: str, result: YouTubeFetchResult
) -> str | None:
    """Download manual or auto-generated subtitles and return their plain text."""
    import yt_dlp

    prefix = tempfile.mkstemp(dir=out_dir)[1]
    _safe_remove(prefix)  # we only wanted a unique base name
    opts = {
        "quiet": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "vtt",
        "outtmpl": prefix + ".%(ext)s",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        log_debug(f"[media_extract] Subtitle download failed: {exc!r}")
        return None

    # Locate any produced .vtt file sharing our prefix.
    base_dir = os.path.dirname(prefix)
    base_name = os.path.basename(prefix)
    vtt_path: str | None = None
    for fname in os.listdir(base_dir):
        if fname.startswith(base_name) and fname.endswith(".vtt"):
            vtt_path = os.path.join(base_dir, fname)
            break
    if not vtt_path:
        return None

    result.temp_files.append(vtt_path)
    try:
        with open(vtt_path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None

    text = _vtt_to_text(raw)
    return text or None


def _vtt_to_text(vtt: str) -> str:
    """Strip WebVTT cues/timestamps and collapse to deduplicated plain text."""
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in vtt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "WEBVTT" or line.startswith(("NOTE", "Kind:", "Language:")):
            continue
        if "-->" in line:  # timestamp cue
            continue
        if line.isdigit():  # numeric cue index
            continue
        # Strip inline VTT tags like <c> or <00:00:00.000>.
        cleaned = _strip_vtt_tags(line)
        if not cleaned:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        lines.append(cleaned)
    return " ".join(lines).strip()


def _strip_vtt_tags(text: str) -> str:
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "<":
            depth += 1
            continue
        if ch == ">":
            if depth > 0:
                depth -= 1
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out).strip()


def _download_youtube_audio(
    url: str, out_dir: str, result: YouTubeFetchResult
) -> str | None:
    """Download the best audio-only stream for downstream STT."""
    import yt_dlp

    prefix = tempfile.mkstemp(dir=out_dir)[1]
    _safe_remove(prefix)
    opts = {
        "quiet": True,
        "format": "bestaudio/best",
        "outtmpl": prefix + ".%(ext)s",
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        log_warning(f"[media_extract] YouTube audio download failed: {exc!r}")
        return None

    base_dir = os.path.dirname(prefix)
    base_name = os.path.basename(prefix)
    for fname in os.listdir(base_dir):
        if fname.startswith(base_name):
            audio_path = os.path.join(base_dir, fname)
            result.temp_files.append(audio_path)
            return audio_path
    return None
