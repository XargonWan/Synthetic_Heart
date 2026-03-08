# plugins/vox_engines/kitten.py
"""Vox TTS engine: KittenTTS — neural text-to-speech.

This plugin defers to the **actual KittenTTS package** (vendored under
``vendor/kittentts`` or installed from PyPI) which provides access to the
`kitten-tts-nano` family of models.  The earlier incarnation used the
system TTS backend via ``pyttsx3``; that produced robotic output and has been
removed.

When ``kittentts`` is unavailable the engine will refuse to synthesize and
log an error telling the operator to install the package via
``uv add kittentts`` or add it to the project dependencies.

The model manager already knows about ``kitten-tts-nano-0.8``; the real
package may download and cache models under ``SYNTH_MODELS_DIR``.
"""

from __future__ import annotations

import io
import threading
from typing import Any

from core.config_manager import config_registry
from core.logging_utils import log_error
from core.variables_engine import register_exposed_var
from core.vox_registry import register_vox_engine
from plugins.vox_base import VoxEngineBase
from core.model_manager import VoiceSpec

# real KittenTTS implementation (imports from vendored or pip package).
# when we ship a vendored copy under ``vendor/kittentts`` we must add the
# *parent* directory of that package to ``sys.path``. previously we were
# inserting ``.../vendor/kittentts`` itself which meant ``import kittentts``
# could not locate ``__init__.py`` (python looked for
# ``vendor/kittentts/kittentts``).
import os
import sys

_vendor_base = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "vendor")
)
# add path only if the vendored package actually exists
if os.path.isdir(os.path.join(_vendor_base, "kittentts")):
    sys.path.insert(0, _vendor_base)

try:
    from kittentts import KittenTTS  # type: ignore[import]
except ImportError:  # pragma: no cover - engine optional
    KittenTTS = None  # type: ignore[assignment]


class LocalKittenTTS:
    """Wrapper around the actual KittenTTS package.

    The previous version of this plugin used ``pyttsx3`` and the system
    TTS backend; the output was robotic and did not match the expectations
    for "KittenTTS".  We now require the real ``kittentts`` package (vendored
    under ``vendor/kittentts`` when not installed) which provides a neural
    model-based synthesiser.  If the package is missing the engine will log
    and simply refuse to generate audio.
    """

    def __init__(self, model_path: str | None = None) -> None:
        # ``model_path`` may be used by the real package to locate a
        # downloaded model; we forward it through.
        self._engine: Any | None = None
        if KittenTTS is not None:
            self._engine = KittenTTS(model_path)

    def generate(self, text: str, voice: str = "Bella") -> bytes:
        if not self._engine:
            raise RuntimeError(
                "KittenTTS engine not available; install the 'kittentts' package"
            )
        return self._engine.generate(text=text, voice=voice)

    @classmethod
    def list_voices(cls) -> list[str]:
        if KittenTTS is not None and hasattr(KittenTTS, "list_voices"):
            return KittenTTS.list_voices()
        # fallback to the model manager defaults
        return [
            "Bella",
            "Jasper",
            "Luna",
            "Bruno",
            "Rosie",
            "Hugo",
            "Kiki",
            "Leo",
        ]


# ---------------------------------------------------------------------------
# Available voices with gender metadata (system voices exposed by LocalKittenTTS)
# ---------------------------------------------------------------------------
_KITTEN_VOICE_META: list[VoiceSpec] = [
    VoiceSpec(name="Bella", gender="F", languages=["*"]),
    VoiceSpec(name="Jasper", gender="M", languages=["*"]),
    VoiceSpec(name="Luna", gender="F", languages=["*"]),
    VoiceSpec(name="Bruno", gender="M", languages=["*"]),
    VoiceSpec(name="Rosie", gender="F", languages=["*"]),
    VoiceSpec(name="Hugo", gender="M", languages=["*"]),
    VoiceSpec(name="Kiki", gender="F", languages=["*"]),
    VoiceSpec(name="Leo", gender="M", languages=["*"]),
]

# Flat list kept for backward-compat code that only needs names
_KITTEN_VOICES: list[str] = [v.name for v in _KITTEN_VOICE_META]

_DEFAULT_VOICE = "Bella"
_DEFAULT_MODEL = "builtin"
_SAMPLE_RATE = 24000


# ---------------------------------------------------------------------------
# Expose engine settings in the WebUI → Components section
# ---------------------------------------------------------------------------
# model selector kept for compatibility but has no effect
register_exposed_var(
    "KITTEN_MODEL",
    label="Kitten TTS — Model",
    default=_DEFAULT_MODEL,
    value_type=str,
    ui_type="select",
    options=[_DEFAULT_MODEL],
    description=("Which KittenTTS model variant to use (ignored by local engine)."),
    scope="plugins",
    component="vox_plugin",
    advanced=False,
)

register_exposed_var(
    "KITTEN_VOICE",
    label="Kitten TTS — Voice",
    default=_DEFAULT_VOICE,
    value_type=str,
    ui_type="select",
    options=_KITTEN_VOICES,
    description="Active voice for KittenTTS synthesis.",
    scope="plugins",
    component="vox_plugin",
    advanced=False,
)

# ---------------------------------------------------------------------------
# Model cache (one instance per model_id to avoid repeated loads)
# ---------------------------------------------------------------------------
_model_cache: dict[str, Any] = {}
_model_cache_lock = threading.Lock()


def _get_model(model_id: str) -> Any | None:
    """Return a cached local TTS engine instance.

    The *model_id* parameter is accepted for API compatibility but is
    ignored; all syntheses are handled by ``LocalKittenTTS``.
    """
    with _model_cache_lock:
        if model_id in _model_cache:
            return _model_cache[model_id]

    try:
        instance = LocalKittenTTS(None)
        with _model_cache_lock:
            _model_cache[model_id] = instance
        return instance
    except Exception as exc:
        log_error(f"[vox/kitten] Could not instantiate local TTS engine: {exc}")
    return None


def _audio_to_mp3(audio_array: Any, sample_rate: int) -> bytes | None:
    """Convert a numpy-compatible audio array to MP3 bytes via pydub (optional)."""
    try:
        import soundfile as sf  # type: ignore[import]
        from pydub import AudioSegment

        arr = audio_array
        if hasattr(arr, "numpy"):
            arr = arr.numpy()
        wav_buf = io.BytesIO()
        sf.write(wav_buf, arr, sample_rate, format="WAV")
        wav_buf.seek(0)
        seg = AudioSegment.from_wav(wav_buf)
        mp3_buf = io.BytesIO()
        seg.export(mp3_buf, format="mp3", bitrate="128k")
        return mp3_buf.getvalue()
    except Exception:
        return None


def _audio_to_wav(audio_array: Any, sample_rate: int) -> bytes | None:
    """Convert numpy audio array to WAV bytes."""
    try:
        import soundfile as sf  # type: ignore[import]

        arr = audio_array
        if hasattr(arr, "numpy"):
            arr = arr.numpy()
        buf = io.BytesIO()
        sf.write(buf, arr, sample_rate, format="WAV")
        return buf.getvalue()
    except Exception as exc:
        log_error(f"[vox/kitten] WAV conversion failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# KittenVoxEngine
# ---------------------------------------------------------------------------
class KittenVoxEngine(VoxEngineBase):
    """KittenTTS engine — local CPU text-to-speech via system voices."""

    display_name = "KittenTTS"

    def _active_model_id(self) -> str:
        return str(
            config_registry.get_value(
                "KITTEN_MODEL",
                _DEFAULT_MODEL,
                value_type=str,
                group="plugins",
                component="vox_plugin",
            )
        )

    def _active_voice(self) -> str:
        return str(
            config_registry.get_value(
                "KITTEN_VOICE",
                _DEFAULT_VOICE,
                value_type=str,
                group="plugins",
                component="vox_plugin",
            )
        )

    # ------------------------------------------------------------------
    # Speaker metadata helpers (used by /api/vox/speakers endpoint)
    # ------------------------------------------------------------------

    def get_speakers(self) -> list[dict]:
        """Return list of voice dicts supported by the local engine."""
        return [{"code": v, "name": v, "language": "en"} for v in _KITTEN_VOICES]

    def sample(self, speaker: str, text_hint: str | None = None) -> bytes:
        """Return WAV bytes for a quick voice sample from the local engine."""
        text = text_hint or f"Sample voice {speaker}"
        tts = _get_model(self._active_model_id())
        if tts is None:
            raise NotImplementedError("TTS engine unavailable")
        try:
            return tts.generate(text=text, voice=speaker)
        except Exception as exc:
            log_error(f"[vox/kitten] sample generation failed: {exc}")
            raise

    @property
    def output_format(self) -> str:
        # the engine always provides WAV-formatted audio
        return "wav"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        # Prefetch in the background; works even without a downloaded model
        # because _get_model now handles the no-path (online-only) case.
        model_id = self._active_model_id()
        t = threading.Thread(target=_get_model, args=(model_id,), daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # TTS generation
    # ------------------------------------------------------------------

    def generate_tts(
        self,
        text: str,
        emotion: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        model_id = str(kwargs.get("model_id") or self._active_model_id())
        voice = str(
            kwargs.get("speaker") or kwargs.get("voice") or self._active_voice()
        )

        tts = _get_model(model_id)
        if tts is None:
            return None

        try:
            audio = tts.generate(text=text, voice=voice)
            # Engine may return raw bytes (WAV/MP3) — pass through directly.
            if isinstance(audio, bytes):
                return audio
            return _audio_to_wav(audio, _SAMPLE_RATE)
        except Exception as exc:
            log_error(f"[vox/kitten] TTS generation failed: {exc}")
            return None


ENGINE_CLASS = KittenVoxEngine

register_vox_engine(
    name="kitten",
    module_path=__name__,
    capabilities={
        "voice_cloning": False,
        "emotions": False,
        "streaming": False,
        "local": True,
    },
    label="KittenTTS — local TTS using system voices (pyttsx3).",
)
