# plugins/auris_engines/vosk_engine.py
"""Auris STT engine: Vosk (local, CPU-only, no cloud).

Vosk uses offline neural models for speech-to-text.  Models are **not**
bundled in the container image; the user selects and downloads them through
the WebUI → Components → Manage Models panel.

Requirements
------------
* ``vosk`` Python package (``uv add vosk``).
* ``ffmpeg`` in PATH for audio conversion.

Model storage
-------------
All models are managed by ``core.model_manager.MODEL_MANAGER``.  The engine
looks up the model directory via ``MODEL_MANAGER.model_dir(model_id)``; it
will never auto-download without explicit user action.

Configuration (via exposed vars, all optional)
----------------------------------------------
``VOSK_MODEL_PATH``
    Absolute or relative path to override the model directory.
    Leave blank to use the Model Manager's storage.
``VOSK_LANGUAGE``
    Language code; the matching model must be downloaded first.
    Default: ``en-us``.

Audio conversion
----------------
Audio files are converted to 16 kHz mono PCM WAV using ``ffmpeg`` before
being fed to KaldiRecognizer.  ``ffmpeg`` must be available in PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

from core.auris_registry import register_auris_engine
from core.logging_utils import log_error, log_info, log_warning
from core.model_manager import MODEL_MANAGER, ModelSpec
from plugins.auris_base import AurisEngineBase

_MODEL_CACHE: dict[str, Any] = {}  # path → vosk.Model singleton

# ---------------------------------------------------------------------------
# Vosk model catalog — registered with MODEL_MANAGER at import time.
# The model_id convention is  vosk-<lang>  e.g. vosk-en-us.
# ---------------------------------------------------------------------------
_VOSK_MODELS: list[ModelSpec] = [
    ModelSpec(
        model_id="vosk-en-us",
        plugin_id="auris_vosk",
        display_name="Vosk English (US) — small",
        description="Compact offline English STT model (~50 MB).",
        tags=["stt", "local", "offline", "cpu"],
        size_mb=50,
        voices=[],
        language="en",
        download_url="https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
    ),
    ModelSpec(
        model_id="vosk-en-us-large",
        plugin_id="auris_vosk",
        display_name="Vosk English (US) — large",
        description="High-accuracy offline English STT model (~1.8 GB).",
        tags=["stt", "local", "offline", "cpu"],
        size_mb=1800,
        voices=[],
        language="en",
        download_url="https://alphacephei.com/vosk/models/vosk-model-en-us-0.42-gigaspeech.zip",
    ),
    ModelSpec(
        model_id="vosk-it-it",
        plugin_id="auris_vosk",
        display_name="Vosk Italiano — small",
        description="Compact offline Italian STT model (~50 MB).",
        tags=["stt", "local", "offline", "cpu"],
        size_mb=50,
        voices=[],
        language="it",
        download_url="https://alphacephei.com/vosk/models/vosk-model-small-it-0.22.zip",
    ),
    ModelSpec(
        model_id="vosk-fr-fr",
        plugin_id="auris_vosk",
        display_name="Vosk Français — small",
        description="Compact offline French STT model (~50 MB).",
        tags=["stt", "local", "offline", "cpu"],
        size_mb=50,
        voices=[],
        language="fr",
        download_url="https://alphacephei.com/vosk/models/vosk-model-small-fr-0.22.zip",
    ),
    ModelSpec(
        model_id="vosk-es-es",
        plugin_id="auris_vosk",
        display_name="Vosk Español — small",
        description="Compact offline Spanish STT model (~50 MB).",
        tags=["stt", "local", "offline", "cpu"],
        size_mb=50,
        voices=[],
        language="es",
        download_url="https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip",
    ),
    ModelSpec(
        model_id="vosk-de-de",
        plugin_id="auris_vosk",
        display_name="Vosk Deutsch — small",
        description="Compact offline German STT model (~50 MB).",
        tags=["stt", "local", "offline", "cpu"],
        size_mb=50,
        voices=[],
        language="de",
        download_url="https://alphacephei.com/vosk/models/vosk-model-small-de-0.15.zip",
    ),
    ModelSpec(
        model_id="vosk-pt-pt",
        plugin_id="auris_vosk",
        display_name="Vosk Português — small",
        description="Compact offline Portuguese STT model (~50 MB).",
        tags=["stt", "local", "offline", "cpu"],
        size_mb=50,
        voices=[],
        language="pt",
        download_url="https://alphacephei.com/vosk/models/vosk-model-small-pt-0.3.zip",
    ),
    ModelSpec(
        model_id="vosk-zh-cn",
        plugin_id="auris_vosk",
        display_name="Vosk 中文 (Chinese) — small",
        description="Compact offline Mandarin STT model (~50 MB).",
        tags=["stt", "local", "offline", "cpu"],
        size_mb=50,
        voices=[],
        language="zh",
        download_url="https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip",
    ),
    ModelSpec(
        model_id="vosk-ja-jp",
        plugin_id="auris_vosk",
        display_name="Vosk 日本語 (Japanese) — small",
        description="Compact offline Japanese STT model (~50 MB).",
        tags=["stt", "local", "offline", "cpu"],
        size_mb=50,
        voices=[],
        language="ja",
        download_url="https://alphacephei.com/vosk/models/vosk-model-small-ja-0.22.zip",
    ),
    ModelSpec(
        model_id="vosk-ko-kr",
        plugin_id="auris_vosk",
        display_name="Vosk 한국어 (Korean) — small",
        description="Compact offline Korean STT model (~50 MB).",
        tags=["stt", "local", "offline", "cpu"],
        size_mb=50,
        voices=[],
        language="ko",
        download_url="https://alphacephei.com/vosk/models/vosk-model-small-ko-0.22.zip",
    ),
]

for _vspec in _VOSK_MODELS:
    MODEL_MANAGER.register(_vspec)

# language-code → model_id mapping (default small models)
_LANG_TO_MODEL_ID: dict[str, str] = {
    "en-us": "vosk-en-us",
    "en": "vosk-en-us",
    "it-it": "vosk-it-it",
    "it": "vosk-it-it",
    "fr-fr": "vosk-fr-fr",
    "fr": "vosk-fr-fr",
    "es-es": "vosk-es-es",
    "es": "vosk-es-es",
    "de-de": "vosk-de-de",
    "de": "vosk-de-de",
    "pt-pt": "vosk-pt-pt",
    "pt": "vosk-pt-pt",
    "zh": "vosk-zh-cn",
    "zh-cn": "vosk-zh-cn",
    "ja": "vosk-ja-jp",
    "ja-jp": "vosk-ja-jp",
    "ko": "vosk-ko-kr",
    "ko-kr": "vosk-ko-kr",
}


def _load_model(model_path: Path) -> Any:
    """Load (or return cached) a vosk.Model from *model_path*.

    The model directory must already exist (downloaded via MODEL_MANAGER).
    No automatic download is attempted here.
    """
    key = str(model_path.resolve())
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    try:
        import vosk  # type: ignore[import]

        vosk.SetLogLevel(-1)  # suppress verbose Kaldi output
        if not model_path.exists():
            log_warning(
                f"[auris/vosk] Model directory not found: {model_path}. "
                "Download the model via WebUI → Components → Manage Models."
            )
            return None

        model = vosk.Model(str(model_path))
        _MODEL_CACHE[key] = model
        log_info(f"[auris/vosk] Model loaded from {model_path}")
        return model
    except ImportError:
        log_error("[auris/vosk] 'vosk' package is not installed. Run: uv add vosk")
        return None
    except Exception as exc:
        log_error(f"[auris/vosk] Failed to load model: {exc}")
        return None


def _get_default_language() -> str:
    """Return configured VOSK_LANGUAGE (default 'en-us')."""
    try:
        from core.config_manager import config_registry  # type: ignore[import]

        lang = config_registry.get_value("VOSK_LANGUAGE", "en-us") or "en-us"
        return str(lang)
    except Exception:
        return "en-us"


def _default_model_path() -> Path:
    """Return the model path managed by MODEL_MANAGER for the current language."""
    lang = _get_default_language()
    model_id = _LANG_TO_MODEL_ID.get(lang, f"vosk-{lang}")
    # If explicitly overridden by VOSK_MODEL_PATH, use that.
    try:
        from core.config_manager import config_registry  # type: ignore[import]

        raw = config_registry.get_value("VOSK_MODEL_PATH", None)
        if raw and str(raw) not in ("", "None"):
            return Path(raw).expanduser()
    except Exception:
        pass
    return MODEL_MANAGER.model_dir(model_id)


def _model_path_from_language(lang: str) -> Path:
    """Return the MODEL_MANAGER storage path for a given language code."""
    model_id = _LANG_TO_MODEL_ID.get(lang.lower(), f"vosk-{lang.lower()}")
    return MODEL_MANAGER.model_dir(model_id)


def _convert_to_wav16k(src: str) -> str | None:
    """Convert *src* audio to a temporary 16 kHz mono WAV file using ffmpeg.

    Returns the path to the temp WAV, or *None* on failure.  Caller is
    responsible for deleting the file.
    """
    try:
        fd, tmp_wav = tempfile.mkstemp(suffix=".wav", prefix="vosk_")
        os.close(fd)
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            src,
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "wav",
            tmp_wav,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            log_warning(
                f"[auris/vosk] ffmpeg conversion failed (rc={result.returncode}): "
                f"{result.stderr.decode(errors='replace')[:300]}"
            )
            Path(tmp_wav).unlink(missing_ok=True)
            return None
        return tmp_wav
    except FileNotFoundError:
        log_error(
            "[auris/vosk] ffmpeg not found in PATH; cannot convert audio. Install ffmpeg."
        )
        return None
    except Exception as exc:
        log_error(f"[auris/vosk] Audio conversion error: {exc}")
        return None


class VoskAurisEngine(AurisEngineBase):
    """Local CPU Vosk STT engine for Auris."""

    display_name = "Vosk STT (local, offline)"

    def _model_path(self) -> Path:
        try:
            from core.config_manager import config_registry  # type: ignore[import]

            raw = config_registry.get_value("VOSK_MODEL_PATH", None)
            if raw and raw != "None":
                return Path(raw).expanduser()
        except Exception:
            pass
        # no explicit path, fall back to language-specific default
        return _default_model_path()

    # ------------------------------------------------------------------
    # AurisEngineBase
    # ------------------------------------------------------------------

    def transcribe(self, file_path: str, mime_type: str | None = None) -> str | None:
        """Transcribe *file_path* using a local Vosk model."""
        model = _load_model(self._model_path())
        if model is None:
            return None

        # Convert to 16 kHz mono WAV
        tmp_wav: str | None = None
        src_is_wav_16k = False
        try:
            # Quick check: if already a 16 kHz mono WAV, skip conversion
            if file_path.lower().endswith(".wav"):
                with wave.open(file_path, "rb") as wf:
                    if wf.getframerate() == 16000 and wf.getnchannels() == 1:
                        src_is_wav_16k = True
        except Exception:
            pass

        wav_path = file_path if src_is_wav_16k else _convert_to_wav16k(file_path)
        if not src_is_wav_16k:
            tmp_wav = wav_path

        if wav_path is None:
            return None

        try:
            import vosk  # type: ignore[import]

            with wave.open(wav_path, "rb") as wf:
                rate = wf.getframerate()
                rec = vosk.KaldiRecognizer(model, rate)
                rec.SetWords(True)

                parts: list[str] = []
                chunk_size = 4096
                while True:
                    data = wf.readframes(chunk_size)
                    if not data:
                        break
                    if rec.AcceptWaveform(data):
                        res = json.loads(rec.Result())
                        txt = res.get("text", "").strip()
                        if txt:
                            parts.append(txt)

                final = json.loads(rec.FinalResult())
                txt = final.get("text", "").strip()
                if txt:
                    parts.append(txt)

            full = " ".join(parts).strip()
            log_info(
                f"[auris/vosk] Transcribed {len(full)} chars from {Path(file_path).name}"
            )
            return full if full else None
        except Exception as exc:
            log_error(f"[auris/vosk] Transcription error: {exc}")
            return None
        finally:
            if tmp_wav:
                try:
                    Path(tmp_wav).unlink(missing_ok=True)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

ENGINE_CLASS = VoskAurisEngine

register_auris_engine(
    name="vosk",
    module_path=__name__,
    capabilities={
        "file_based": True,
        "realtime": False,
        "vad": False,
        "local": True,
        "offline": True,
    },
    label="Vosk STT (local, offline, CPU-only) — requires a Vosk model (~50 MB, auto-downloaded on first use when you select this engine in the WebUI).",
)
