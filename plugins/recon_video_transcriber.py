# plugins/recon_video_transcriber.py
"""Recon Video Transcriber.

An LLM-gated Recon plugin that transcribes videos — local attachments and
YouTube links alike — and attaches the result to the prompt as a snippet.

Flow
----
1. The shared Recon LLM call is asked (via :meth:`get_recon_instruction`) to
   reconstruct a list of canonical YouTube URLs from the user's message. The
   LLM handles partial IDs and malformed references (e.g. ``aoP81h68Xkk`` or
   ``htps:/youtube.com/watch?v=...``) — no keyword/regex matching is used.
2. :meth:`parse_recon_response` validates each candidate URL structurally
   (:func:`core.media_extract.is_youtube_url`), plus any local video file
   referenced on the incoming message's ``raw_data``.
3. For each source it obtains a transcript:
   * YouTube → existing subtitles (fast path) or downloaded audio → Auris STT.
   * Local file → audio extracted via ffmpeg → Auris STT.
   Optionally a visual description is produced via Iris.
4. Results are combined into a single ``snippet`` contribution (the only recon
   type surfaced by the prompt engine).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, List

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning
from core.media_extract import (
    YouTubeFetchResult,
    extract_audio_from_video,
    fetch_youtube,
    is_youtube_url,
)

display_name = "Recon Video Transcriber"


def _register_var(
    name: str,
    *,
    label: str,
    default: Any,
    value_type: type,
    ui_type: str,
    description: str,
) -> None:
    try:
        from core.variables_engine import register_exposed_var

        register_exposed_var(
            name,
            label=label,
            default=default,
            value_type=value_type,
            ui_type=ui_type,
            description=description,
            scope="agent",
            component="agent",
        )
    except Exception:
        config_registry.get_var(
            name,
            default,
            value_type=value_type,
            label=label,
            description=description,
            group="agent",
            component="agent",
        )


_register_var(
    "RECON_VIDEO_TRANSCRIBER_RECON_ENABLED",
    label="Enable Recon Video Transcriber",
    default=True,
    value_type=bool,
    ui_type="bool",
    description="Enable the Recon Video Transcriber plugin (transcribe local and "
    "YouTube videos and attach the transcript to the prompt).",
)
_register_var(
    "RECON_VIDEO_MAX_SECONDS",
    label="Recon Video Max Duration (s)",
    default=1800,
    value_type=int,
    ui_type="int",
    description="Skip videos longer than this many seconds (0 = no limit).",
)
_register_var(
    "RECON_VIDEO_INCLUDE_VISION",
    label="Recon Video Include Visual Description",
    default=True,
    value_type=bool,
    ui_type="bool",
    description="Also produce a visual description of the video via Iris.",
)
_register_var(
    "RECON_VIDEO_SNIPPET_MAX_CHARS",
    label="Recon Video Snippet Max Chars",
    default=12000,
    value_type=int,
    ui_type="int",
    description="Truncate each video's combined transcript to this many "
    "characters to avoid bloating the prompt (0 = no limit).",
)

# Local video file extensions recognised on incoming attachments.
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg"}


class ReconVideoTranscriberPlugin:
    display_name = display_name
    recon_priority = 5

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "video_media"

    def get_recon_instruction(self) -> str:
        return (
            "Determine whether the user's message references one or more videos "
            "that should be transcribed. This includes YouTube links, bare "
            "YouTube video IDs, and malformed/typo'd links. Reconstruct each "
            "reference into a canonical YouTube watch URL of the form "
            "https://www.youtube.com/watch?v=VIDEO_ID . For example a bare id "
            "like aoP81h68Xkk becomes https://www.youtube.com/watch?v=aoP81h68Xkk , "
            "and a malformed htps:/youtube.com/watch?v=aoP81h68Xkk is fixed to the "
            "same canonical URL. "
            'Return an object: {"video_media": {"youtube_urls": ["https://...", ...]}}. '
            "If no video is referenced, return an empty list for youtube_urls."
        )

    # ------------------------------------------------------------------
    # URL extraction helpers
    # ------------------------------------------------------------------

    def _urls_from_data(self, data: Any) -> list[str]:
        """Pull reconstructed YouTube URLs out of the parsed recon payload."""
        urls: list[str] = []
        payload = data
        if isinstance(payload, dict):
            inner = payload.get("video_media", payload)
            if isinstance(inner, dict):
                raw = inner.get("youtube_urls")
            elif isinstance(inner, list):
                raw = inner
            else:
                raw = None
            if isinstance(raw, list):
                urls = [str(u).strip() for u in raw if str(u).strip()]
        elif isinstance(payload, list):
            urls = [str(u).strip() for u in payload if str(u).strip()]
        return [u for u in urls if is_youtube_url(u)]

    def _urls_from_raw_text(self, raw_text: str | None) -> list[str]:
        """Fallback self-parse of the raw LLM text when central parsing missed."""
        if not raw_text or not raw_text.strip():
            return []
        try:
            parsed = json.loads(raw_text.strip())
        except json.JSONDecodeError:
            import re

            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if not match:
                return []
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        return self._urls_from_data(parsed)

    def _local_video_path(self, message: Any) -> str | None:
        """Resolve a local video file referenced on the incoming message."""
        if message is None:
            return None
        raw = getattr(message, "raw_data", None)
        if not isinstance(raw, dict):
            return None
        # Common keys interfaces use for downloaded media paths.
        for key in ("media_path", "file_path", "attachment_path", "video_path"):
            candidate = raw.get(key)
            if isinstance(candidate, str) and candidate.strip():
                path = candidate.strip()
                if os.path.exists(path):
                    ext = os.path.splitext(path)[1].lower()
                    if ext in _VIDEO_EXTS:
                        return path
        return None

    # ------------------------------------------------------------------
    # Transcription
    # ------------------------------------------------------------------

    async def _transcribe_audio(self, audio_path: str) -> str | None:
        from core.core_initializer import PLUGIN_REGISTRY

        auris = PLUGIN_REGISTRY.get("auris_plugin")
        if not auris or not hasattr(auris, "transcribe_audio"):
            log_debug("[recon_video] Auris plugin unavailable; skipping STT.")
            return None
        try:
            result = await auris.transcribe_audio(audio_path)
        except Exception as exc:
            log_warning(f"[recon_video] Auris transcription failed: {exc!r}")
            return None
        if result is None:
            return None
        text = getattr(result, "text", None)
        return text.strip() if isinstance(text, str) and text.strip() else None

    async def _describe_visual(self, file_path: str) -> str | None:
        from core.core_initializer import PLUGIN_REGISTRY

        iris = PLUGIN_REGISTRY.get("iris_plugin")
        if not iris or not hasattr(iris, "describe_media"):
            log_debug("[recon_video] Iris plugin unavailable; skipping vision.")
            return None
        try:
            result = await iris.describe_media(file_path, mime_type="video/mp4")
        except Exception as exc:
            log_warning(f"[recon_video] Iris description failed: {exc!r}")
            return None
        if result is None:
            return None
        desc = getattr(result, "description", None)
        return desc.strip() if isinstance(desc, str) and desc.strip() else None

    def _truncate(self, text: str) -> str:
        limit = int(
            config_registry.get_value(
                "RECON_VIDEO_SNIPPET_MAX_CHARS", 12000, value_type=int
            )
            or 0
        )
        if limit > 0 and len(text) > limit:
            return text[:limit] + "\n... (troncato)"
        return text

    async def _process_youtube(
        self, url: str, *, include_vision: bool, max_seconds: int
    ) -> str | None:
        fetched: YouTubeFetchResult | None = None
        try:
            # fetch_youtube performs blocking network/subprocess I/O (yt-dlp),
            # so run it off the event loop to avoid stalling the whole recon
            # (and therefore the message pipeline).
            fetched = await asyncio.to_thread(
                fetch_youtube,
                url,
                max_duration_s=max_seconds or None,
                prefer_subtitles=True,
            )
            if fetched is None:
                return None

            parts: list[str] = []
            header = fetched.title or url

            if fetched.subtitle_text:
                parts.append(f"Trascrizione (sottotitoli): {fetched.subtitle_text}")
            elif fetched.audio_path:
                transcript = await self._transcribe_audio(fetched.audio_path)
                if transcript:
                    parts.append(f"Trascrizione: {transcript}")

            if include_vision and fetched.audio_path:
                # Vision on a downloaded YouTube file is out of scope (audio-only
                # stream); visual analysis applies to local video files only.
                pass

            if not parts:
                return None
            body = "\n".join(parts)
            return f"[YouTube: {header}]\n{body}"
        finally:
            if fetched is not None:
                fetched.cleanup()

    async def _process_local(
        self, video_path: str, *, include_vision: bool, max_seconds: int
    ) -> str | None:
        parts: list[str] = []
        # ffmpeg audio extraction is blocking; keep it off the event loop.
        audio_path = await asyncio.to_thread(extract_audio_from_video, video_path)
        try:
            if audio_path:
                transcript = await self._transcribe_audio(audio_path)
                if transcript:
                    parts.append(f"Trascrizione: {transcript}")
            if include_vision:
                visual = await self._describe_visual(video_path)
                if visual:
                    parts.append(f"Descrizione visiva: {visual}")
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
        if not parts:
            return None
        name = os.path.basename(video_path)
        return f"[Video: {name}]\n" + "\n".join(parts)

    # ------------------------------------------------------------------
    # Recon entry point
    # ------------------------------------------------------------------

    async def parse_recon_response(
        self,
        data,
        *,
        message=None,
        context_memory=None,
        text: str | None = None,
        tags: List[str] | None = None,
        keywords: List[str] | None = None,
        max_results: int = 5,
        _raw_llm_text: str | None = None,
    ) -> list[dict]:
        enabled = bool(
            config_registry.get_value(
                "RECON_VIDEO_TRANSCRIBER_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        include_vision = bool(
            config_registry.get_value(
                "RECON_VIDEO_INCLUDE_VISION", True, value_type=bool
            )
        )
        max_seconds = int(
            config_registry.get_value("RECON_VIDEO_MAX_SECONDS", 1800, value_type=int)
            or 0
        )

        # Resolve YouTube URLs (parsed data first, raw-text fallback second).
        youtube_urls = self._urls_from_data(data)
        if not youtube_urls:
            youtube_urls = self._urls_from_raw_text(_raw_llm_text)
        # De-duplicate while preserving order.
        seen: set[str] = set()
        youtube_urls = [u for u in youtube_urls if not (u in seen or seen.add(u))]

        local_path = self._local_video_path(message)

        if not youtube_urls and not local_path:
            log_debug("[recon_video] No video source resolved.")
            return []

        snippets: list[str] = []

        for url in youtube_urls[:max_results]:
            try:
                snippet = await self._process_youtube(
                    url, include_vision=include_vision, max_seconds=max_seconds
                )
            except Exception as exc:
                log_warning(
                    f"[recon_video] YouTube processing failed for {url}: {exc!r}"
                )
                snippet = None
            if snippet:
                snippets.append(self._truncate(snippet))

        if local_path:
            try:
                snippet = await self._process_local(
                    local_path, include_vision=include_vision, max_seconds=max_seconds
                )
            except Exception as exc:
                log_warning(
                    f"[recon_video] Local video processing failed for "
                    f"{local_path}: {exc!r}"
                )
                snippet = None
            if snippet:
                snippets.append(self._truncate(snippet))

        if not snippets:
            return []

        log_info(f"[recon_video] Attached {len(snippets)} video transcript(s) to recon")
        combined = "\n\n---\n\n".join(snippets)
        return [
            {
                "type": "snippet",
                "content": combined,
                "source": "recon_video_transcriber",
                "priority": int(self.recon_priority),
            }
        ]


PLUGIN_CLASS = ReconVideoTranscriberPlugin
