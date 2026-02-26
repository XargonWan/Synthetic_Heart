# plugins/auris_engines/vosk_engine.py
"""Auris STT engine: Vosk (local, CPU-only, no cloud).

Vosk uses offline neural models for speech-to-text.  The smallest English
model (``vosk-model-small-en-us``) is ~50 MB and runs well on CPU.

Requirements
------------
* ``vosk`` Python package (``pip install vosk`` or ``uv add vosk``).
* A downloaded Vosk model directory, pointed to by ``VOSK_MODEL_PATH``.
  If the path does not exist the engine will attempt to download the small
  English model automatically using the ``vosk`` downloader utility (requires
  internet access on first run only).

Configuration (via exposed vars, all optional)
----------------------------------------------
``VOSK_MODEL_PATH``
    Absolute or relative path to the Vosk model directory.
    Default: ``~/.cache/vosk/vosk-model-small-en-us``.
``VOSK_LANGUAGE``
    Language code used when no explicit model path is set.  In the WebUI a
    language selector appears when the active Auris engine is ``vosk``; choosing
    a value here will also trigger an automatic model download on the backend
    (first transcription will also download if invoked programmatically).
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
from plugins.auris_base import AurisEngineBase

_MODEL_CACHE: dict[str, Any] = {}  # path → vosk.Model singleton
# default points only to english; other languages will be derived dynamically
_DEFAULT_MODEL_PATH = Path.home() / ".cache" / "vosk" / "vosk-model-small-en-us"
_SMALL_MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"

# map of language codes -> download URL; we only populate a few common ones here,
# fall back to a templated URL (may need manual correction if the version changes).
_LANGUAGE_URLS: dict[str, str] = {
    "en-us": _SMALL_MODEL_URL,
    "it-it": "https://alphacephei.com/vosk/models/vosk-model-small-it-0.22.zip",
    "fr-fr": "https://alphacephei.com/vosk/models/vosk-model-small-fr-0.22.zip",
    "es-es": "https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip",
}


def _load_model(model_path: Path) -> Any:
    """Load (or return cached) a vosk.Model from *model_path*.

    If the directory does not exist it will be downloaded automatically; the
    download URL is inferred from the language code embedded in the directory
    name (e.g. ``vosk-model-small-it-it``).
    """
    key = str(model_path.resolve())
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    try:
        import vosk  # type: ignore[import]

        vosk.SetLogLevel(-1)  # suppress verbose Kaldi output
        if not model_path.exists():
            log_info(
                f"[auris/vosk] Model not found at {model_path}; attempting download…"
            )
            _download_model(model_path)

        if not model_path.exists():
            log_error(f"[auris/vosk] Model directory still missing: {model_path}")
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


def _download_model(dest: Path) -> None:
    """Download and extract a small Vosk model appropriate for *dest*.

    The target language is inferred from the directory name (prefix
    ``vosk-model-small-``).  If a known URL is registered in
    :data:`_LANGUAGE_URLS` it will be used; otherwise a generic URL template is
    attempted.  Failures just log a warning and leave the directory missing.
    """
    try:
        import urllib.request
        import zipfile

        # determine language code from dest name
        lang = dest.name.replace("vosk-model-small-", "")
        url = _LANGUAGE_URLS.get(lang) or f"https://alphacephei.com/vosk/models/vosk-model-small-{lang}-0.22.zip"

        dest.parent.mkdir(parents=True, exist_ok=True)
        zip_path = dest.parent / (f"{dest.name}.zip")
        log_info(f"[auris/vosk] Downloading model ({lang}) from {url} …")
        urllib.request.urlretrieve(url, zip_path)

        log_info("[auris/vosk] Extracting model…")
        with zipfile.ZipFile(zip_path, "r") as zf:
            top = zf.namelist()[0].split("/")[0]
            zf.extractall(dest.parent)
        extracted = dest.parent / top
        if extracted != dest:
            extracted.rename(dest)

        zip_path.unlink(missing_ok=True)
        log_info(f"[auris/vosk] Model downloaded to {dest}")
    except Exception as exc:
        log_warning(
            f"[auris/vosk] Auto-download failed: {exc}. Download manually from {url if 'url' in locals() else _SMALL_MODEL_URL}"
        )




def _get_default_language() -> str:
    """Return configured VOSK_LANGUAGE (default 'en-us')."""
    try:
        from core.config_manager import config_registry  # type: ignore[import]

        lang = config_registry.get_value("VOSK_LANGUAGE", "en-us") or "en-us"
        return str(lang)
    except Exception:
        return "en-us"


def _default_model_path() -> Path:
    """Return the default model path based on the current language setting."""
    lang = _get_default_language()
    return Path.home() / ".cache" / "vosk" / f"vosk-model-small-{lang}"


def _model_path_from_language(lang: str) -> Path:
    """Helper for constructing a model directory given a language code."""
    return Path.home() / ".cache" / "vosk" / f"vosk-model-small-{lang}"


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
    capabilities={"file_based": True, "realtime": False, "vad": False, "local": True, "offline": True},
    label="Vosk STT (local, offline, CPU-only) — requires a Vosk model (~50 MB, auto-downloaded on first use when you select this engine in the WebUI).",
)
