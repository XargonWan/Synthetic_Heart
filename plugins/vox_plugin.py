# plugins/vox_plugin.py
"""Vox — core TTS + lip-sync plugin.

Centralises the complete text-to-speech output pipeline:
  1. Clean text (strip emoji / markup).
  2. Delegate to the active Vox engine for audio generation.
  3. Write the audio blob to disk (handles WAV and raw-PCM engines).
  4. Dispatch audio to the requesting interface (WebUI, Discord, Telegram, …).
  5. Trigger lip-sync animation in the WebUI if available.
  6. Optionally fall back to plain-text when generation fails.

Engines only need to produce bytes.  Everything else is handled here.
No interface, no plugin, and no engine should re-implement this pipeline.

Backward-compatibility: existing ``TTS_ENABLED`` / ``TTS_ENDPOINTS`` /
``TTS_FALLBACK_TO_TEXT`` variables are preserved and mapped to their Vox
equivalents so deployments that used ``tts_lipsync`` continue to work
without configuration changes.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Any

from core.ai_plugin_base import AIPluginBase
from core.beat_utils import is_outbound_beat
from core.config_manager import config_registry
from core.core_initializer import register_plugin, INTERFACE_REGISTRY
from core.facial_expression_parser import (
    FacialExpressionEvent,
    parse_facial_expressions,
)
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.vox_registry import VOX_REGISTRY

# ---------------------------------------------------------------------------
# Language detection helper (lingua — replaces langdetect for better accuracy)
# ---------------------------------------------------------------------------

_lingua_detector: Any | None = None
_lingua_detector_lock = threading.Lock()


def _get_lingua_detector() -> Any | None:
    """Return the singleton ``lingua`` detector, building it on first call.

    Uses ``lingua-language-detector`` (much more accurate than ``langdetect``
    for short texts and closely-related languages such as Italian / Spanish).
    Returns ``None`` gracefully when the package is not installed.
    """
    global _lingua_detector
    if _lingua_detector is not None:
        return _lingua_detector
    with _lingua_detector_lock:
        if _lingua_detector is not None:  # double-checked
            return _lingua_detector
        try:
            from lingua import LanguageDetectorBuilder

            _lingua_detector = (
                LanguageDetectorBuilder.from_all_languages()
                .with_minimum_relative_distance(0.1)
                .build()
            )
            log_info("[vox_plugin] lingua detector initialized.")
        except Exception as exc:
            log_warning(f"[vox_plugin] lingua not available: {exc}")
    return _lingua_detector


def _detect_language(text: str) -> str | None:
    """Return a BCP-47 / ISO-639-1 language code or ``None``.

    Uses ``lingua-language-detector`` for accurate detection, especially on
    short texts and closely-related language pairs (e.g. Italian / Spanish).
    Returns ``None`` when confidence is below the internal threshold or the
    detector is unavailable.
    """
    if not text or not text.strip():
        return None

    detector = _get_lingua_detector()
    if detector is not None:
        try:
            lang = detector.detect_language_of(text)
            if lang is not None:
                code = lang.iso_code_639_1.name.lower()
                log_debug(f"[vox_plugin] lingua detected: {code!r} for {text[:50]!r}")
                return code
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Config variables (hidden from Settings)
# ---------------------------------------------------------------------------
#
# Vox engine selection, default model and fallback-to-text now live in the
# Engines tab (external_endpoints / media-subsystem selectors), not in Settings.
# These keys remain readable at runtime via ``config_registry.get_value`` but
# are registered hidden so they no longer surface in the Settings UI.

config_registry.get_value(
    "ACTIVE_VOX_ENGINE",
    "disabled",
    value_type=str,
    group="plugins",
    component="vox_plugin",
    hidden=True,
)
config_registry.get_value(
    "VOX_ENGINE_SETTINGS",
    "{}",
    value_type=str,
    group="plugins",
    component="vox_plugin",
    hidden=True,
)
config_registry.get_value(
    "VOX_OUTPUT_DIR",
    "res/synth_webui/static/audio/tts",
    value_type=str,
    group="plugins",
    component="vox_plugin",
    hidden=True,
)
config_registry.get_value(
    "VOX_TIMEOUT_SECONDS",
    300,
    value_type=int,
    group="plugins",
    component="vox_plugin",
    hidden=True,
)
config_registry.get_value(
    "VOX_FALLBACK_TO_TEXT",
    True,
    value_type=bool,
    group="plugins",
    component="vox_plugin",
    hidden=True,
)
config_registry.get_value(
    "VOX_AUDIO_CACHE_SIZE",
    40,
    value_type=int,
    group="plugins",
    component="vox_plugin",
    hidden=True,
)
config_registry.get_value(
    "VOX_SENTENCE_CHUNKING",
    True,
    value_type=bool,
    group="plugins",
    component="vox_plugin",
    hidden=True,
)
# Visible toggle (Engines tab → vox_plugin box): when on, replies to typed
# text messages also get a TTS clip attached on every interface. Off keeps
# the default behavior (Telegram/Discord only speak in reply to voice input).
config_registry.get_value(
    "VOX_SPEAK_TEXT_REPLIES",
    False,
    label="Attach voice to text replies",
    description=(
        "When enabled, replies to typed text messages also get a voice clip "
        "attached (all interfaces). When disabled, Telegram/Discord replies "
        "are only spoken when the incoming message was voice."
    ),
    value_type=bool,
    group="plugins",
    component="vox_plugin",
)

# Legacy aliases (read by the HTTP engine via its original config keys)
# tts_lipsync variables are intentionally kept so existing .env files keep working.

# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

_EMOJI_RE = re.compile(r"[^\w\s,.!?;:\'\-\"\u2018\u2019]+")
_MULTI_SPACE_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s+")


def _split_sentences(text: str, min_chars: int = 12) -> list[str]:
    """Best-effort sentence splitter used only for TTS chunking.

    Not linguistically precise (no abbreviation handling, e.g. "Mr. Smith"
    will split) \u2014 good enough to find natural pause points for streaming
    playback, which tolerates an occasional wrong split far better than a
    missed one. Fragments shorter than ``min_chars`` are merged into a
    neighbour so a lone "Ok." doesn't become its own tiny synthesis call.
    """
    raw = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    if len(raw) <= 1:
        return raw

    merged: list[str] = []
    buffer = ""
    for piece in raw:
        buffer = f"{buffer} {piece}".strip() if buffer else piece
        if len(buffer) >= min_chars:
            merged.append(buffer)
            buffer = ""
    if buffer:
        if merged:
            merged[-1] = f"{merged[-1]} {buffer}"
        else:
            merged.append(buffer)
    return merged


def _get_wav_duration(path: Path) -> float | None:
    """Return the duration in seconds of a WAV file, or ``None`` on failure."""
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0:
                return frames / rate
    except Exception:
        pass
    return None


def _wav_header_needs_repair(audio_bytes: bytes) -> bool:
    """Return True when a RIFF/WAVE payload has a placeholder/invalid size field.

    Some engines (notably Fish Audio) stream WAV over ``Transfer-Encoding:
    chunked`` and cannot know the total length up front, so they write a
    placeholder ``RIFF`` chunk size (``0`` or ``0xFFFFFFFF``) and/or a bogus
    ``data`` sub-chunk size. Such a file plays back as garbage / undecodable in
    the browser and yields a wrong duration from :mod:`wave`. We treat the
    header as authoritative only when the declared ``RIFF`` size is consistent
    with the actual byte length.
    """
    if (
        len(audio_bytes) < 12
        or audio_bytes[0:4] != b"RIFF"
        or audio_bytes[8:12] != b"WAVE"
    ):
        return False
    riff_size = int.from_bytes(audio_bytes[4:8], "little")
    expected = len(audio_bytes) - 8
    # Placeholder markers or a size that disagrees with the real length.
    if riff_size in (0, 0xFFFFFFFF):
        return True
    # Allow small slack for trailing metadata chunks, but flag gross mismatches.
    if abs(riff_size - expected) > 8:
        return True
    return False


def _repair_wav_header(audio_bytes: bytes) -> bytes | None:
    """Re-mux a WAV blob so its header sizes are authoritative.

    Prefers a lossless decode+re-emit via :mod:`wave` (handles standard 8/16/
    24/32-bit PCM). Falls back to ``ffmpeg`` for anything :mod:`wave` cannot
    parse (float WAV, WAVE_FORMAT_EXTENSIBLE, compressed payloads). Returns the
    repaired WAV bytes, or ``None`` if repair is impossible (caller keeps the
    original bytes).
    """
    # Attempt 1: decode + re-emit via the stdlib wave module.
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as src:
            nchannels = src.getnchannels()
            sampwidth = src.getsampwidth()
            framerate = src.getframerate()
            frames = src.readframes(src.getnframes() or -1)
        if frames and framerate > 0 and nchannels > 0 and sampwidth > 0:
            out = io.BytesIO()
            with wave.open(out, "wb") as dst:
                dst.setnchannels(nchannels)
                dst.setsampwidth(sampwidth)
                dst.setframerate(framerate)
                dst.writeframes(frames)
            repaired = out.getvalue()
            if repaired.startswith(b"RIFF") and len(repaired) > 44:
                return repaired
    except Exception as exc:
        log_debug(f"[vox_plugin] wave-based WAV repair failed: {exc!r}")

    # Attempt 2: ffmpeg transcode (robust for non-standard/compressed WAV).
    repaired = _ffmpeg_to_wav(audio_bytes)
    if repaired:
        return repaired
    log_warning(
        "[vox_plugin] could not repair malformed WAV header; "
        "lip-sync/duration may be wrong for this clip"
    )
    return None


def _ffmpeg_to_wav(audio_bytes: bytes) -> bytes | None:
    """Transcode arbitrary audio to a well-formed WAV via ffmpeg, or ``None``."""
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
            [ffmpeg, "-y", "-i", in_path, "-f", "wav", out_path],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            log_warning(
                "[vox_plugin] ffmpeg WAV repair failed "
                f"(rc={result.returncode}): "
                f"{result.stderr.decode('utf-8', 'replace')[:300]}"
            )
            return None
        with open(out_path, "rb") as out_fh:
            wav_bytes = out_fh.read()
        return wav_bytes or None
    except Exception as exc:
        log_warning(f"[vox_plugin] ffmpeg WAV repair raised: {exc!r}")
        return None
    finally:
        for path in (in_path, out_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def is_vox_enabled() -> bool:
    """Return True when a Vox TTS engine is active (not ``disabled``).

    Reads the ``ACTIVE_VOX_ENGINE`` config value directly so callers (e.g.
    interfaces building their action catalog) can decide whether to advertise
    the ``send_as_voice`` flag, without needing a live plugin instance. When
    Vox is disabled, ``send_as_voice`` is not offered to the model at all —
    mirroring how Iris only exposes ``vision_describe`` when enabled.
    """
    try:
        engine = str(
            config_registry.get_value(
                "ACTIVE_VOX_ENGINE",
                "disabled",
                value_type=str,
                group="plugins",
                component="vox_plugin",
            )
        )
    except Exception:
        return False
    return engine.strip().lower() != "disabled"


class VoxPlugin(AIPluginBase):
    """Core TTS + lip-sync plugin.  Interfaces and agents call ``speak()``."""

    display_name = "Vox (TTS + Lip-Sync)"

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()
        # explicit enabled flag removed; use engine name "disabled" instead
        self._active_engine_name: str = "http"
        self._engine_settings: dict[str, Any] = {}
        self._output_dir: Path = Path("res/synth_webui/static/audio/tts")
        self._fallback_to_text: bool = True
        self._sentence_chunking_enabled: bool = True

        # Pre-warm the lingua detector in a daemon thread so it is ready before
        # the first detect_language() call arrives from recon.  Building all
        # language models can take 1-3 s on first run; doing it eagerly avoids
        # cold-start timeouts (default LANGUAGE_DETECTOR_TIMEOUT = 2 s).
        threading.Thread(
            target=_get_lingua_detector,
            daemon=True,
            name="lingua-warmup",
        ).start()

        # Import built-in engine modules so they self-register
        self._import_builtin_engines()

        self.refresh_config()
        register_plugin("vox_plugin", self)
        log_info("[vox_plugin] Initialized.")

    # ------------------------------------------------------------------
    # Public API — used by interfaces and other plugins
    # ------------------------------------------------------------------

    async def speak(
        self,
        text: str,
        interface_path: str | None = None,
        context: dict | None = None,
        original_message: Any = None,
        emotion: str | None = None,
        engine_name: str | None = None,
        merged_text: str | None = None,
        allow_fallback: bool = True,
        generate_only: bool = False,
        voice: str | None = None,
    ) -> dict[str, Any]:
        """Full TTS pipeline: generate → write → dispatch → lip-sync.

        Args:
            text:             Text to synthesise.
            interface_path:   Destination interface path (e.g. ``synth_webui/session_id``).
            context:          Message chain context dict.
            original_message: Original message object (used to recover interface_path).
            emotion:          Optional emotion/style hint forwarded to the engine.
            engine_name:      Override the active engine for this call.
            merged_text:      Pre-resolved display text (used as fallback caption).
            voice:            Optional explicit voice override. When omitted, the
                              engine resolves the configured voice via the normal
                              Vox flow (``<ENGINE>_VOICE`` config key). Only set
                              this for deliberate per-call overrides.

        Returns:
            ``{"status": "success"|"skipped"|"error", ...}``
        """
        self.refresh_config()

        # Determine which engine is actually being used for this call.
        chosen = engine_name or self._active_engine_name
        # A value of "disabled" means the subsystem is turned off.
        if chosen == "disabled":
            log_info("[vox_plugin] Engine disabled; skipping.")
            if allow_fallback:
                _ip = interface_path
                if not _ip and context:
                    _ip = str(context.get("interface_path") or "")
                if not _ip and original_message:
                    _ip = getattr(original_message, "interface_path", None)
                _fallback_text = merged_text or text
                if _ip and _fallback_text:
                    log_debug(
                        "[vox_plugin] Disabled but allow_fallback=True — sending text fallback."
                    )
                    return await self._send_fallback(_fallback_text, str(_ip))
            return {"status": "skipped", "reason": "vox_disabled"}

        # --- Resolve interface_path from context / message if not provided ---
        if not interface_path and context:
            interface_path = context.get("interface_path")
        if not interface_path and original_message:
            interface_path = getattr(original_message, "interface_path", None)

        async def _fallback(msg_text: str) -> dict[str, Any]:
            """Delegate to _send_fallback only when allowed.

            When ``allow_fallback=False`` (auto-injected TTS actions) we suppress
            the text fallback to avoid sending a duplicate: the text was already
            dispatched by the preceding ``message_*_bot`` action.
            """
            if allow_fallback:
                return await self._send_fallback(msg_text, interface_path)
            log_warning(
                "[vox_plugin] TTS failed (auto-injected action); "
                "suppressing text fallback to avoid duplicate message."
            )
            return {"status": "error", "reason": "tts_failed_no_fallback_allowed"}

        # --- Strip [em_*] facial expression tags FIRST ---
        # Tags must be removed before synthesis so they don't appear as
        # spoken text.  The parsed events are kept for optional scheduling
        # of the facial expression timeline and for emotion-aware engines.
        # This MUST run before the emoji/symbol cleanup below: _EMOJI_RE strips
        # square brackets, which would break the ``[em_*]`` tag pattern and
        # leave the bare ``em_smile:0.6`` text to be spoken aloud.
        clean, em_events = parse_facial_expressions(text)

        # --- Clean text for the engine ---
        clean = _EMOJI_RE.sub("", clean)
        clean = _MULTI_SPACE_RE.sub(" ", clean).strip()
        if not clean:
            return {"status": "skipped", "reason": "empty_text"}

        # Attempt language detection on the cleaned text so that downstream
        # engines (e.g. cloud APIs) can pick an appropriate voice or model.
        # Uses lingua-language-detector for much better accuracy on short texts
        # and on closely-related language pairs (e.g. Italian / Spanish).
        detected_lang: str | None = _detect_language(clean)
        if detected_lang:
            log_info(
                f"[vox_plugin] detected text language: '{detected_lang}'"
            )  # pragma: no branch

        # --- Load engine ---
        name = engine_name or self._active_engine_name
        try:
            engine = VOX_REGISTRY.load_engine(name)
        except ValueError as exc:
            log_error(f"[vox_plugin] Cannot load engine '{name}': {exc}")
            return await _fallback(merged_text or text)

        # --- Emotional text preprocessing for capable engines ---
        tts_text = clean
        if em_events and getattr(engine, "supports_emotion_tags", lambda: False)():
            try:
                emotion_tuples = [
                    (ev.position, ev.name, ev.intensity) for ev in em_events
                ]
                tts_text = engine.preprocess_emotional_text(clean, emotion_tuples)
            except Exception as exc:
                log_warning(f"[vox_plugin] preprocess_emotional_text failed: {exc}")

        # --- Generate audio ---
        # pass detected language hint to engine if available; engines may
        # ignore unexpected kwargs.
        kwargs: dict[str, Any] = {}
        if detected_lang:
            kwargs["language"] = detected_lang
        # Only forward an explicit voice override; when absent the engine/bridge
        # resolves the configured voice through the normal Vox flow. This keeps
        # callers (e.g. radio) following the same voice as regular chat unless
        # they deliberately override it.
        if voice:
            kwargs["voice"] = voice

        # Sentence-by-sentence streaming shrinks time-to-first-audio for the
        # live webui/stage avatar on multi-sentence replies — the first
        # sentence plays while later ones are still synthesizing, instead of
        # the client waiting for the entire reply. See _chunking_karada for
        # the (narrow) eligibility rules and _speak_chunked for the pipeline.
        chunk_karada = await self._chunking_karada(
            generate_only, em_events, interface_path
        )
        sentences = _split_sentences(tts_text) if chunk_karada else []

        already_streamed = False
        audio_bytes: bytes | None = None
        if len(sentences) > 1:
            audio_bytes = await self._speak_chunked(
                sentences, engine, emotion, kwargs, chunk_karada
            )
            already_streamed = audio_bytes is not None

        if audio_bytes is None:
            try:
                audio_bytes = await asyncio.to_thread(
                    engine.generate_tts, tts_text, emotion, **kwargs
                )
            except Exception as exc:
                log_error(f"[vox_plugin] Engine '{name}' generation error: {exc}")
                audio_bytes = None
            already_streamed = False

        if not audio_bytes:
            log_warning(f"[vox_plugin] Engine '{name}' returned no audio.")
            return await _fallback(merged_text or text)

        # --- Write to disk ---
        filename = f"vox_{int(time.time())}.wav"
        out_path = self._output_dir / filename
        try:
            self._write_audio(out_path, audio_bytes, engine)
        except Exception as exc:
            log_error(f"[vox_plugin] Failed to write audio file: {exc}")
            return await _fallback(merged_text or text)

        if not out_path.exists():
            log_error(f"[vox_plugin] Written file not found: {out_path}")
            return await _fallback(merged_text or text)

        # --- Optional lip-sync metadata ---
        lipsync_data: dict | None = None
        get_ls = getattr(engine, "get_lipsync_data", None)
        if callable(get_ls):
            try:
                lipsync_data = get_ls(audio_bytes)
            except Exception:
                pass

        # --- Extract actual audio duration ---
        audio_duration_s = _get_wav_duration(out_path)
        if audio_duration_s is not None:
            log_debug(f"[vox_plugin] audio duration: {audio_duration_s:.2f}s")

        # --- Stay in THINK until speech actually starts ---
        # The audio has been synthesised, but for voice-originated turns there is
        # no textual WRITE phase: the avatar is kept in THINK during generation
        # and must go straight to TALK the instant the audio begins playing. That
        # TALK transition is owned by the Karada state server, which fires it from
        # ``broadcast_audio()`` — the single choke point where the clip starts and
        # its exact duration is known. Setting WRITE here produced a spurious
        # animation flash *before* the synthesis was heard, so it is intentionally
        # not done: we let THINK hold until ``broadcast_audio()`` starts TALK.

        # --- Dispatch to interface (skip for internal callers like radio host) ---
        if not generate_only:
            await self._dispatch(
                audio_path=out_path,
                interface_path=interface_path,
                caption=merged_text or text,
                lipsync_data=lipsync_data,
                context=context,
                original_message=original_message,
                audio_duration_s=audio_duration_s,
                skip_karada_broadcast=already_streamed,
            )

        # --- Schedule facial expression timeline (voice responses) ---
        # For voice responses (allow_fallback=True, no parallel message_*
        # action), the expression timeline is driven by VoxPlugin using the
        # real audio duration.  For auto-injected TTS alongside a message_*
        # action, the action_parser already scheduled the timeline.
        if em_events and allow_fallback:
            self._schedule_expression_timeline(
                em_events, clean, interface_path, audio_duration_s
            )

        result: dict[str, Any] = {
            "status": "success",
            "audio_path": str(out_path),
            "filename": filename,
        }
        if audio_duration_s is not None:
            result["audio_duration_s"] = audio_duration_s
        if lipsync_data is not None:
            result["lipsync_data"] = lipsync_data
        return result

    # ------------------------------------------------------------------
    # WebUI broadcast for already-generated audio (e.g. radio host)
    # ------------------------------------------------------------------

    async def broadcast_audio_to_webui(
        self,
        audio_path: str,
        text: str | None = None,
        engine_name: str | None = None,
    ) -> bool:
        """Broadcast an already-generated TTS audio file to every connected
        Synth WebUI client so spectators *see and hear* the avatar speak.

        This is used by internal callers such as the radio host, which
        generate audio with ``speak(generate_only=True)`` and stream it to an
        external service (AzuraCast).  Calling this makes the shared avatar on
        the WebUI play the same voice, with lip-sync and the correct talking
        animation state — exactly as a normal voice message does.

        Args:
            audio_path:  Filesystem path to the audio file to broadcast.
            text:        Optional caption text shown as a chat bubble.
            engine_name: Optional Vox engine name used to derive lip-sync data.

        Returns:
            ``True`` if the audio was delivered to at least one client.
        """
        webui = INTERFACE_REGISTRY.get("synth_webui")
        if not webui or not hasattr(webui, "send_tts_audio"):
            return False

        path = Path(audio_path)
        if not path.exists():
            log_warning(
                f"[vox_plugin] broadcast_audio_to_webui: file not found: {audio_path}"
            )
            return False

        # Derive lip-sync metadata from the audio bytes using the active engine.
        lipsync_data: dict | None = None
        try:
            engine = VOX_REGISTRY.load_engine(engine_name or self._active_engine_name)
            get_ls = getattr(engine, "get_lipsync_data", None)
            if callable(get_ls):
                lipsync_data = get_ls(path.read_bytes())
        except Exception:
            pass

        audio_duration_s = _get_wav_duration(path)

        # Distribute via the Karada state server (single source of truth) so
        # every connected client (WebUI, and any future client) sees/hears the
        # shared avatar speak with lip-sync and the talking animation state.
        try:
            from core.animation_handler import get_karada_state_server

            karada = get_karada_state_server()
            if not karada or not karada.has_connected_clients():
                log_debug(
                    "[vox_plugin] broadcast_audio_to_webui: no connected clients."
                )
                return False
            await karada.broadcast_audio(
                audio_path=str(path),
                lipsync_data=lipsync_data,
                audio_duration_s=audio_duration_s,
                text=text,
            )
            return True
        except Exception as exc:
            log_warning(f"[vox_plugin] broadcast_audio_to_webui failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Language detection helper (used by recon and other components)
    # ------------------------------------------------------------------

    def detect_language(
        self, message: Any, interface_path: str | None = None
    ) -> str | None:
        """Return a detected language code (e.g. 'en' or 'it').

        This method implements the *language detector plugin* contract used by
        :mod:`core.recon`.  Uses ``lingua-language-detector`` for significantly
        better accuracy on short texts and on closely-related language pairs
        (e.g. Italian / Spanish / Portuguese).  Returns ``None`` gracefully
        when detection fails or confidence is too low.

        ``message`` may be a plain string, a dict (with a ``"text"`` key), or
        any object that exposes a ``.text`` attribute (e.g. ``MessageWrapper``).
        """
        text: str
        if isinstance(message, str):
            text = message
        elif isinstance(message, dict):
            text = str(message.get("text") or "")
        elif hasattr(message, "text"):
            # handles MessageWrapper and any similar proxy object;
            # .text may be None for voice messages before transcription
            raw = message.text
            text = str(raw) if raw is not None else ""
        else:
            # last resort: str() — at least we tried
            text = str(message)
        result = _detect_language(text)
        log_debug(f"[vox_plugin] detect_language: input={text[:60]!r} → {result!r}")
        return result

    # ------------------------------------------------------------------
    # Action wiring
    # ------------------------------------------------------------------

    @staticmethod
    def get_supported_actions() -> dict:
        return {
            "tts_speak": {
                "description": (
                    "Reply with a spoken voice message. Use this whenever the user asks "
                    "you to answer with voice/audio, or whenever you decide a spoken reply "
                    "fits better than plain text. The text you provide is turned into "
                    "audio and delivered as a single voice message (with the text as "
                    "caption). Works on any turn, including when the incoming message was "
                    "typed text."
                ),
                "required_fields": ["text"],
                "optional_fields": ["emo"],
            }
        }

    def is_enabled(self) -> bool:
        self.refresh_config()
        return self._active_engine_name != "disabled"

    def get_prompt_instructions(self, action_name: str) -> dict:
        if action_name == "tts_speak":
            return {
                "description": (
                    "Reply with a spoken voice message instead of (or in addition to) "
                    "plain text. The provided text is synthesised into audio, lip-synced, "
                    "and delivered to the chat as a single voice message whose caption is "
                    "that same text. Choose this action when the user asks to be answered "
                    "with voice/audio in any language, or whenever you judge a spoken reply "
                    "is more appropriate. When you use it, put your full reply in 'text' "
                    "and do NOT also emit a separate text-only message action \u2014 the "
                    "voice message already carries your words."
                ),
                "payload": {
                    "text": {
                        "type": "string",
                        "description": "The reply to speak (also shown as the caption).",
                    },
                    "emo": {
                        "type": "string",
                        "description": "Optional emotion style hint.",
                        "optional": True,
                    },
                },
            }
        return {}

    async def handle_custom_action(
        self, action_type: str, payload: dict
    ) -> dict[str, Any]:
        if action_type == "tts_speak":
            return await self.speak(
                text=payload.get("text", ""),
                emotion=payload.get("emo"),
                merged_text=payload.get("__merged_text"),
            )
        return {"status": "error", "message": f"Unknown action: {action_type}"}

    async def execute_action(
        self,
        action: dict,
        context: dict,
        bot: Any,
        original_message: Any,
    ) -> dict[str, Any]:
        payload = action.get("payload", {})

        # Skip TTS for Grillo internal beats (outbound beats speak)
        is_grillo_internal = context.get("grillo_beat", False) and not is_outbound_beat(
            context.get("beat_type")
        )
        if is_grillo_internal:
            return {"status": "skipped", "reason": "grillo_internal_beat"}

        return await self.speak(
            text=payload.get("text", ""),
            emotion=payload.get("emo"),
            interface_path=context.get("interface_path"),
            context=context,
            original_message=original_message,
            merged_text=payload.get("__merged_text"),
            # Fallback to text if TTS fails, even if auto-injected, because
            # the original message action might have been removed to merge text.
            allow_fallback=True,
        )

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def refresh_config(self) -> None:
        """Re-read exposed variables (allows WebUI hot-changes)."""
        try:
            import json

            # read active engine; "disabled" deactivates the subsystem
            self._active_engine_name = str(
                config_registry.get_value(
                    "ACTIVE_VOX_ENGINE",
                    "http",
                    value_type=str,
                    group="plugins",
                    component="vox_plugin",
                )
            )

            raw_settings = config_registry.get_value(
                "VOX_ENGINE_SETTINGS",
                "{}",
                value_type=str,
                group="plugins",
                component="vox_plugin",
            )
            try:
                self._engine_settings = json.loads(raw_settings or "{}")
            except Exception:
                self._engine_settings = {}

            output_dir_raw = config_registry.get_value(
                "VOX_OUTPUT_DIR",
                "res/synth_webui/static/audio/tts",
                value_type=str,
                group="plugins",
                component="vox_plugin",
            )
            self._output_dir = Path(str(output_dir_raw))
            self._output_dir.mkdir(parents=True, exist_ok=True)

            self._fallback_to_text = bool(
                config_registry.get_value(
                    "VOX_FALLBACK_TO_TEXT",
                    True,
                    value_type=bool,
                    group="plugins",
                    component="vox_plugin",
                )
            )

            self._sentence_chunking_enabled = bool(
                config_registry.get_value(
                    "VOX_SENTENCE_CHUNKING",
                    True,
                    value_type=bool,
                    group="plugins",
                    component="vox_plugin",
                )
            )
        except Exception as exc:
            log_warning(f"[vox_plugin] refresh_config failed: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _schedule_expression_timeline(
        self,
        events: list[FacialExpressionEvent],
        clean_text: str,
        interface_path: str | None,
        audio_duration_s: float | None,
    ) -> None:
        """Fire-and-forget the facial expression timeline for a voice response."""
        try:
            from core.animation_handler import get_karada_state_server

            karada = get_karada_state_server()
            if not karada or not karada.has_connected_clients():
                return

            from core.core_initializer import PLUGIN_REGISTRY
            from plugins.facial_expression_plugin import FacialExpressionPlugin

            expr_plugin: FacialExpressionPlugin | None = None
            if isinstance(PLUGIN_REGISTRY, dict):
                for p in PLUGIN_REGISTRY.values():
                    if isinstance(p, FacialExpressionPlugin):
                        expr_plugin = p
                        break
            if not expr_plugin:
                return

            from core.persona_manager import get_persona_manager

            persona_json: dict[str, Any] | None = None
            pm = get_persona_manager()
            current_persona = getattr(pm, "_current_persona", None) if pm else None
            if pm and current_persona:
                try:
                    persona_json = pm._load_persona_json(current_persona.name)
                except Exception:
                    persona_json = None

            chars_per_sec = (
                persona_json.get("facial_expression_chars_per_sec", 12)
                if persona_json
                else 12
            )
            expr_section = (
                persona_json.get("facial_expressions", {}) if persona_json else {}
            )

            session_id = ""
            if interface_path:
                parts = str(interface_path).split("/")
                if len(parts) >= 2:
                    session_id = parts[1]

            asyncio.create_task(
                expr_plugin._play_expression_timeline(
                    events,
                    len(clean_text),
                    session_id,
                    chars_per_sec,
                    expr_section=expr_section,
                    audio_duration_s=audio_duration_s,
                )
            )
        except Exception as exc:
            log_warning(f"[vox_plugin] Failed to schedule expression timeline: {exc}")

    async def _chunking_karada(
        self,
        generate_only: bool,
        em_events: list[FacialExpressionEvent],
        interface_path: str | None,
    ) -> Any | None:
        """Return the live Karada state server when sentence-chunked TTS
        streaming applies to this call, else ``None``.

        Chunking is scoped narrowly on purpose: only the live webui/stage
        avatar path (Telegram/Discord/etc. still get exactly one clip per
        reply — chunking would mean N separate voice messages, which is a
        regression there, not an improvement), never when facial-expression
        tags are present (``_schedule_expression_timeline`` anchors its
        timeline to whole-reply text offsets, which chunked synthesis would
        have to reproduce per-chunk), and gated by ``VOX_SENTENCE_CHUNKING``
        so it can be switched off without a code change.
        """
        if generate_only or em_events or not self._sentence_chunking_enabled:
            return None
        if not interface_path:
            return None
        try:
            from core.interface_path_utils import parse_interface_path

            iface_name, _ = parse_interface_path(interface_path)
        except Exception:
            return None
        if iface_name != "synth_webui":
            return None
        try:
            from core.animation_handler import get_karada_state_server

            karada = get_karada_state_server()
        except Exception:
            return None
        if not karada or not karada.has_connected_clients():
            return None
        return karada

    async def _speak_chunked(
        self,
        sentences: list[str],
        engine: Any,
        emotion: str | None,
        kwargs: dict[str, Any],
        karada: Any,
    ) -> bytes | None:
        """Synthesize *sentences* one at a time, broadcasting each to the
        live avatar as soon as it is ready while the next one synthesizes in
        the background — this is the actual latency win: the first sentence
        is audible without waiting for the whole reply to finish generating.

        Returns the full reply's concatenated WAV bytes (used for the
        persisted chat bubble / click-to-replay / non-live interfaces,
        exactly as the single-shot path would have produced), or ``None`` if
        every chunk failed to synthesize — the caller falls back to
        whole-reply synthesis in that case.

        Broadcasts are paced with ``asyncio.sleep(duration)`` between
        chunks so ``KaradaStateServer.broadcast_audio``'s talk-animation
        return-to-idle timer (reset on every call) stays roughly in step
        with actual client playback instead of firing early because
        synthesis outran real-time audio.
        """
        turn_id = f"vox-{uuid.uuid4().hex[:8]}"

        async def _synth(sentence: str) -> bytes | None:
            try:
                return await asyncio.to_thread(
                    engine.generate_tts, sentence, emotion, **kwargs
                )
            except Exception as exc:
                log_error(f"[vox_plugin] chunk synthesis error: {exc}")
                return None

        wav_chunks: list[bytes] = []
        pending: "asyncio.Task[bytes | None] | None" = None

        for i, sentence in enumerate(sentences):
            raw = await pending if pending is not None else await _synth(sentence)
            pending = None
            if not raw:
                continue

            wav_bytes = self._to_wav_bytes(raw, engine)
            wav_chunks.append(wav_bytes)

            more_coming = i + 1 < len(sentences)
            if more_coming:
                pending = asyncio.create_task(_synth(sentences[i + 1]))

            chunk_path = self._output_dir / f"vox_{turn_id}_{i}.wav"
            duration: float | None = None
            try:
                chunk_path.write_bytes(wav_bytes)
                duration = _get_wav_duration(chunk_path)
            except Exception as exc:
                log_error(f"[vox_plugin] failed to write chunk {i}: {exc}")

            try:
                await karada.broadcast_audio(
                    audio_path=str(chunk_path),
                    audio_duration_s=duration,
                    turn_id=turn_id,
                )
            except Exception as exc:
                log_warning(f"[vox_plugin] chunk broadcast failed: {exc}")

            if more_coming and duration:
                await asyncio.sleep(duration)

        if not wav_chunks:
            return None
        return self._concat_wav_bytes(wav_chunks)

    @staticmethod
    def _to_wav_bytes(audio_bytes: bytes, engine: Any) -> bytes:
        """Normalize raw engine output to WAV bytes, wrapping raw PCM if needed.

        Handles three cases:
          1. ``fmt == "pcm"`` or non-RIFF bytes → wrap raw PCM in a WAV header
             with correct sizes (engines that emit headerless PCM).
          2. RIFF bytes with a valid, authoritative header → pass through.
          3. RIFF bytes with a placeholder/invalid size field (e.g. Fish Audio
             streams WAV via chunked transfer and writes ``data``/``RIFF`` sizes
             of 0 or ``0xFFFFFFFF``) → re-mux so the header is authoritative.
             A broken size field makes ``wave``-based duration parsing and the
             browser ``<audio>`` element fail, which is why lip-sync latches the
             mouth open and the expression timeline collapses to ~0s. This must
             work for any engine, not just Fish, per the pipeline contract.
        """
        fmt = getattr(engine, "output_format", "wav")
        if fmt == "pcm" or not audio_bytes.startswith(b"RIFF"):
            sr = getattr(engine, "sample_rate", 22050)
            ch = getattr(engine, "channels", 1)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(ch)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(audio_bytes)
            return buf.getvalue()
        if _wav_header_needs_repair(audio_bytes):
            repaired = _repair_wav_header(audio_bytes)
            if repaired is not None:
                return repaired
        return audio_bytes

    def _write_audio(self, path: Path, audio_bytes: bytes, engine: Any) -> None:
        """Write audio bytes to disk, wrapping raw PCM in WAV if needed."""
        path.write_bytes(self._to_wav_bytes(audio_bytes, engine))

    @staticmethod
    def _concat_wav_bytes(wav_chunks: list[bytes]) -> bytes:
        """Concatenate same-format WAV byte blobs (already normalized via
        ``_to_wav_bytes``) into a single clip, for the persisted/dispatched
        file when a reply was synthesized sentence-by-sentence."""
        if len(wav_chunks) == 1:
            return wav_chunks[0]
        with wave.open(io.BytesIO(wav_chunks[0]), "rb") as first:
            params = first.getparams()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setparams(params)
            for chunk in wav_chunks:
                with wave.open(io.BytesIO(chunk), "rb") as wf:
                    out.writeframes(wf.readframes(wf.getnframes()))
        return buf.getvalue()

    async def _dispatch(
        self,
        audio_path: Path,
        interface_path: str | None,
        caption: str,
        lipsync_data: dict | None,
        context: dict | None,
        original_message: Any,
        audio_duration_s: float | None = None,
        skip_karada_broadcast: bool = False,
    ) -> None:
        """Dispatch generated audio to the correct interface.

        ``skip_karada_broadcast`` is set by ``speak()`` when the reply was
        already streamed to the live avatar sentence-by-sentence
        (``_speak_chunked``) — the karada fan-out for the combined clip
        would otherwise double-play the same audio the client just heard.
        """
        if not interface_path:
            log_warning("[vox_plugin] No interface_path; cannot dispatch audio.")
            return

        try:
            from core.interface_path_utils import parse_interface_path

            iface_name, levels = parse_interface_path(interface_path)
            target_iface = INTERFACE_REGISTRY.get(iface_name)

            if not target_iface:
                log_warning(
                    f"[vox_plugin] Interface '{iface_name}' not found in registry."
                )
                return

            # The Karada state server is the single source of truth for
            # "the avatar is speaking". Hand the audio to the server and let it
            # fan out a tts-play command to *every* connected client (WebUI
            # today, an Android app / XR headset tomorrow). This happens for
            # every turn regardless of which interface originated it (e.g. an
            # audio received via Telegram while the WebUI is open) and whether
            # it was automatic or explicitly triggered. We never iterate client
            # connections here — the server distributes to all transports.
            # Skipped when the reply was already streamed chunk-by-chunk
            # (see the skip_karada_broadcast docstring above).
            if not skip_karada_broadcast:
                await self._broadcast_audio_to_clients(
                    audio_path=audio_path,
                    lipsync_data=lipsync_data,
                    audio_duration_s=audio_duration_s,
                )

            if iface_name == "synth_webui" and hasattr(target_iface, "send_tts_audio"):
                session_id = levels[0] if levels else None
                if session_id:
                    send_kwargs: dict[str, Any] = {
                        "session_id": session_id,
                        "audio_path": str(audio_path),
                        "text": caption,
                        "lipsync_data": lipsync_data,
                    }
                    if audio_duration_s is not None:
                        send_kwargs["audio_duration_s"] = audio_duration_s
                    try:
                        await target_iface.send_tts_audio(**send_kwargs)
                    except TypeError as exc:
                        if (
                            "audio_duration_s" not in send_kwargs
                            or "audio_duration_s" not in str(exc)
                        ):
                            raise
                        send_kwargs.pop("audio_duration_s", None)
                        await target_iface.send_tts_audio(**send_kwargs)

            elif iface_name == "discord_bot" and hasattr(target_iface, "send_message"):
                await target_iface.send_message(
                    {
                        "interface_path": interface_path,
                        "audio": str(audio_path),
                        "text": caption,
                    }
                )

            elif iface_name == "telegram_bot" and hasattr(
                target_iface, "execute_action"
            ):
                _, levels_ = parse_interface_path(interface_path)
                if levels_:
                    await target_iface.execute_action(
                        {
                            "type": "audio_telegram_bot",
                            "payload": {
                                "interface_path": interface_path,
                                "audio": str(audio_path),
                                "caption": caption,
                            },
                        },
                        context or {},
                        None,
                        original_message,
                    )

            else:
                # Generic fallback: send audio first, then text as a separate
                # message immediately after (for interfaces that don't support
                # native audio+caption in a single call).
                if hasattr(target_iface, "send_message"):
                    await target_iface.send_message(
                        {
                            "interface_path": interface_path,
                            "audio": str(audio_path),
                        }
                    )
                    if caption:
                        await target_iface.send_message(
                            {
                                "interface_path": interface_path,
                                "text": caption,
                            }
                        )

        except Exception as exc:
            log_error(f"[vox_plugin] Dispatch error: {exc}")

    async def _broadcast_audio_to_clients(
        self,
        audio_path: Path,
        lipsync_data: dict | None,
        audio_duration_s: float | None,
    ) -> None:
        """Hand generated audio to the Karada state server for distribution.

        The Karada state server is the single source of truth for the
        "avatar is speaking" state: it fans a ``tts-play`` command out to every
        registered transport (all connected clients) and records the current
        audio so late-joining clients catch up. We never iterate individual
        client connections here. Best-effort; never raises.
        """
        try:
            from core.animation_handler import get_karada_state_server

            karada = get_karada_state_server()
            if not karada:
                return
            await karada.broadcast_audio(
                audio_path=str(audio_path),
                lipsync_data=lipsync_data,
                audio_duration_s=audio_duration_s,
            )
        except Exception as exc:
            log_debug(f"[vox_plugin] _broadcast_audio_to_clients error: {exc}")

    async def _send_fallback(
        self, text: str, interface_path: str | None
    ) -> dict[str, Any]:
        """Send a plain-text message when TTS fails (if fallback is enabled)."""
        if not self._fallback_to_text or not text or not interface_path:
            return {"status": "error", "reason": "tts_failed_no_fallback"}

        try:
            from core.interface_path_utils import parse_interface_path

            iface_name, _ = parse_interface_path(interface_path)
            target_iface = INTERFACE_REGISTRY.get(iface_name)
            if target_iface and hasattr(target_iface, "send_message"):
                await target_iface.send_message(
                    {"interface_path": interface_path, "text": text}
                )
                log_info("[vox_plugin] Text-only fallback sent.")
        except Exception as exc:
            log_error(f"[vox_plugin] Fallback send failed: {exc}")

        return {"status": "error", "reason": "tts_failed_fallback_sent"}

    @staticmethod
    def _import_builtin_engines() -> None:
        """Import built-in Vox engine modules so they self-register."""
        # The http engine is always imported (not gated on TTS_ENDPOINTS
        # anymore) so its config box shows in Engines → Vox even before an
        # endpoint has been configured.
        builtins = [
            "plugins.vox_engines.http",
            "plugins.vox_engines.kitten",
        ]

        for mod in builtins:
            try:
                module = importlib.import_module(mod)
                engine_name = mod.rsplit(".", 1)[-1]
                if engine_name not in VOX_REGISTRY.get_available_engines():
                    importlib.reload(module)
            except Exception as exc:
                log_warning(
                    f"[vox_plugin] Could not import engine module '{mod}': {exc}"
                )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

PLUGIN_CLASS = VoxPlugin
