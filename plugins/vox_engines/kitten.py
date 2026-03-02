# plugins/vox_engines/kitten.py
"""Vox TTS engine: KittenTTS — lightweight neural TTS (CPU-optimised).

Uses the KittenML HuggingFace models::

    KittenML/kitten-tts-mini-0.8     (~450 MB, 8 voices)
    KittenML/kitten-tts-micro-0.8    (~300 MB, 8 voices)
    KittenML/kitten-tts-nano-0.8     (~150 MB, 8 voices)
    KittenML/kitten-tts-nano-0.8-int8 (~150 MB INT8, 8 voices)

Models are **not** bundled in the container image.  The user selects and
downloads each model through the WebUI → Components → Manage Models panel.

Requirements
------------
``kittentts`` package (``uv add kittentts`` or installed from
https://github.com/KittenML/KittenTTS).
``pydub`` for WAV→MP3 conversion of sample files (optional, graceful degradation).
``soundfile`` for WAV serialisation.
"""

from __future__ import annotations

import io
import threading
from typing import Any

from core.config_manager import config_registry
from core.logging_utils import log_error, log_info, log_warning
from core.model_manager import MODEL_MANAGER, ModelSpec, VoiceSpec
from core.variables_engine import register_exposed_var
from core.vox_registry import register_vox_engine
from plugins.vox_base import VoxEngineBase

# ---------------------------------------------------------------------------
# Available voices with gender metadata (all KittenML models share this list)
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
_DEFAULT_MODEL = "kitten-tts-nano-0.8"
_SAMPLE_RATE = 24000

# ---------------------------------------------------------------------------
# Register models with the Model Manager (one entry per HF model variant)
# ---------------------------------------------------------------------------
_MODEL_SPECS: list[ModelSpec] = [
    ModelSpec(
        model_id="kitten-tts-mini-0.8",
        plugin_id="vox_kitten",
        display_name="KittenTTS Mini 0.8",
        description="Highest quality KittenTTS variant (~450 MB). Best for production use.",
        tags=["tts", "local", "cpu", "neural"],
        size_mb=450,
        voices_meta=_KITTEN_VOICE_META,
        supported_languages=["en"],
        hf_repo_id="KittenML/kitten-tts-mini-0.8",
    ),
    ModelSpec(
        model_id="kitten-tts-micro-0.8",
        plugin_id="vox_kitten",
        display_name="KittenTTS Micro 0.8",
        description="Balanced quality/speed KittenTTS variant (~300 MB).",
        tags=["tts", "local", "cpu", "neural"],
        size_mb=300,
        voices_meta=_KITTEN_VOICE_META,
        supported_languages=["en"],
        hf_repo_id="KittenML/kitten-tts-micro-0.8",
    ),
    ModelSpec(
        model_id="kitten-tts-nano-0.8",
        plugin_id="vox_kitten",
        display_name="KittenTTS Nano 0.8",
        description="Fast, lightweight KittenTTS variant (~150 MB). Recommended for CPU-only setups.",
        tags=["tts", "local", "cpu", "neural", "recommended"],
        size_mb=150,
        voices_meta=_KITTEN_VOICE_META,
        supported_languages=["en"],
        hf_repo_id="KittenML/kitten-tts-nano-0.8",
    ),
    ModelSpec(
        model_id="kitten-tts-nano-0.8-int8",
        plugin_id="vox_kitten",
        display_name="KittenTTS Nano 0.8 INT8",
        description="INT8 quantised nano variant (~150 MB). Lower memory footprint.",
        tags=["tts", "local", "cpu", "neural", "quantised"],
        size_mb=150,
        voices_meta=_KITTEN_VOICE_META,
        supported_languages=["en"],
        hf_repo_id="KittenML/kitten-tts-nano-0.8-int8",
    ),
]

for _spec in _MODEL_SPECS:
    MODEL_MANAGER.register(_spec)

# ---------------------------------------------------------------------------
# Expose engine settings in the WebUI → Components section
# ---------------------------------------------------------------------------
register_exposed_var(
    "KITTEN_MODEL",
    label="Kitten TTS — Model",
    default=_DEFAULT_MODEL,
    value_type=str,
    ui_type="select",
    options=[s.model_id for s in _MODEL_SPECS],
    description=(
        "Which KittenTTS model variant to use. "
        "Download the model first via the Manage Models panel."
    ),
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
    """Return a cached KittenTTS instance, loading from disk if needed."""
    with _model_cache_lock:
        if model_id in _model_cache:
            return _model_cache[model_id]

    if not MODEL_MANAGER.is_downloaded(model_id):
        log_warning(
            f"[vox/kitten] Model '{model_id}' is not downloaded. "
            "Download it via the WebUI → Components → Manage Models."
        )
        return None

    model_path = MODEL_MANAGER.model_dir(model_id)
    try:
        from kittentts import KittenTTS  # type: ignore[import]

        log_info(f"[vox/kitten] Loading KittenTTS from {model_path} …")
        instance = KittenTTS(str(model_path))
        with _model_cache_lock:
            _model_cache[model_id] = instance
        log_info(f"[vox/kitten] KittenTTS '{model_id}' ready.")
        return instance
    except ImportError:
        log_error(
            "[vox/kitten] 'kittentts' package not installed. "
            "Install from https://github.com/KittenML/KittenTTS"
        )
    except Exception as exc:
        log_error(f"[vox/kitten] Failed to load model '{model_id}': {exc}")
    return None


def _audio_to_mp3(audio_array: Any, sample_rate: int) -> bytes | None:
    """Convert a numpy-compatible audio array to MP3 bytes via pydub (optional)."""
    try:
        import soundfile as sf
        from pydub import AudioSegment  # type: ignore[import]

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
        import soundfile as sf

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
    """KittenTTS engine — CPU-capable neural TTS via KittenML HuggingFace models."""

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
        """Return list of voice dicts for the currently-selected model."""
        model_id = self._active_model_id()
        spec = MODEL_MANAGER.get_spec(model_id)
        voices = spec.voices if spec else _KITTEN_VOICES
        return [{"code": v, "name": v, "language": "en"} for v in voices]

    def sample(self, speaker: str, text_hint: str | None = None) -> bytes:
        """Return MP3 bytes for a pre-generated voice sample.

        The method accepts an optional ``text_hint`` which is the sample text
        that the caller would like the engine to speak; this is used by the
        generator to append the voice name so each file is distinct.  Backward
        compatibility is preserved by defaulting to ``None``.

        Generates the sample on first call if the model is downloaded.
        Raises NotImplementedError if sample cannot be produced.
        """

        def _generate(text: str, voice: str | None) -> bytes | None:
            # incorporate the voice into the prompt so samples are recognisable
            if voice:
                text = f"{text} (voice {voice})"
            model_id = self._active_model_id()
            tts = _get_model(model_id)
            if tts is None:
                return None
            try:
                audio = tts.generate(text=text, voice=voice)
                mp3 = _audio_to_mp3(audio, _SAMPLE_RATE)
                return mp3 if mp3 else _audio_to_wav(audio, _SAMPLE_RATE)
            except Exception as exc:
                log_error(f"[vox/kitten] sample generation failed: {exc}")
                return None

        model_id = self._active_model_id()
        # ensure_sample will call _generate with the spec's sample_text
        path = MODEL_MANAGER.ensure_sample(model_id, speaker, _generate)
        if path and path.exists():
            return path.read_bytes()
        raise NotImplementedError(
            f"No sample available for voice '{speaker}' "
            f"(model '{model_id}' may not be downloaded)."
        )

    @property
    def output_format(self) -> str:
        return "wav"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        model_id = self._active_model_id()
        if MODEL_MANAGER.is_downloaded(model_id):
            t = threading.Thread(target=_get_model, args=(model_id,), daemon=True)
            t.start()
        else:
            log_warning(
                f"[vox/kitten] Model '{model_id}' not downloaded — "
                "TTS unavailable until the model is downloaded via the UI."
            )

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
    label="KittenTTS — lightweight neural TTS (CPU-friendly, multiple quality tiers).",
)
