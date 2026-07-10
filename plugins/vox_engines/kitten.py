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

This module registers ``kitten-tts-nano-0.8`` with the model manager at
import time so it appears in the WebUI "Manage Models" list; the real
package downloads and caches models under ``SYNTH_MODELS_DIR``.
"""

from __future__ import annotations

import io
import json
import threading
from typing import Any

from core.config_manager import config_registry
from core.logging_utils import log_error, log_info
from core.variables_engine import register_exposed_var
from core.vox_registry import register_vox_engine
from plugins.vox_base import VoxEngineBase
from core.model_manager import MODEL_MANAGER, ModelSpec, VoiceSpec

# Import strategy: try the real kittentts package installed via uv/pip first.
# Only fall back to the vendored gTTS stub when the real package is absent.
# The vendor path must NOT be inserted before the real package is attempted,
# otherwise the stub would always shadow the installed package.
import os
import sys

# Default HuggingFace model used when no model_id is configured.
# kitten-tts-mini-0.8 is the higher-quality ONNX model (~80 MB); it is the
# default and is downloaded automatically on first use.
_DEFAULT_KITTENTTS_MODEL = "KittenML/kitten-tts-mini-0.8"

# Flag set at import time so generate() knows which API to call.
_USING_VENDOR_STUB: bool

try:
    # Prefer the real neural kittentts package — multi-voice, multilingual.
    from kittentts import KittenTTS  # type: ignore[import]

    _USING_VENDOR_STUB = False
    log_info("[vox/kitten] Using real kittentts package (multi-voice neural TTS).")
except ImportError:
    # Real package not installed — fall back to vendored gTTS stub.
    # NOTE: the stub uses gTTS which does NOT support multiple voice personas;
    # all voices will sound identical regardless of the ``voice`` parameter.
    log_info(
        "[vox/kitten] Real kittentts not found; falling back to vendored gTTS stub "
        "(voice selection not supported — install kittentts for multi-voice TTS)."
    )
    _vendor_base = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "vendor")
    )
    if os.path.isdir(os.path.join(_vendor_base, "kittentts")):
        sys.path.insert(0, _vendor_base)
    try:
        from kittentts import KittenTTS  # type: ignore[import]  # noqa: F811
    except ImportError:  # pragma: no cover
        KittenTTS = None  # type: ignore[assignment]
    _USING_VENDOR_STUB = True


# Mapping from ISO 639-1 codes to espeak-ng language identifiers.
# Used to replace the kittentts hardcoded ``en-us`` phonemizer at call time
# so that non-English text is phonemized correctly.
_ESPEAK_LANG_MAP: dict[str, str] = {
    "en": "en-us",
    "it": "it",
    "fr": "fr",
    "de": "de",
    "es": "es",
    "pt": "pt",
    "nl": "nl",
    "pl": "pl",
    "ru": "ru",
    "ko": "ko",
    "ar": "ar",
    # Japanese (ISO 639-1: ja; non-standard alias jp).
    # espeak-ng 'ja' is not reliably present in all deployments and kittentts
    # was trained on Latin-script phonemes.  Italian shares Japanese's open
    # vowel system and clear consonants, producing more intelligible output
    # than the English fallback.
    "ja": "it",
    "jp": "it",
}


def _espeak_lang_code(lang: str | None) -> str:
    """Return the espeak-ng language code for an ISO 639-1 *lang* tag.

    Falls back to ``"en-us"`` for unknown or None values.
    """
    if not lang:
        return "en-us"
    # Accept both 'it' and 'it-IT' style codes.
    base = lang.split("-")[0].lower()
    return _ESPEAK_LANG_MAP.get(base, "en-us")


class LocalKittenTTS:
    """Wrapper around the KittenTTS package (real neural or vendored gTTS stub).

    When the real ``kittentts`` package is installed the wrapper:
    - Initialises the model by downloading it from HuggingFace on first use.
    - Honours the ``voice`` parameter (each voice has a distinct acoustic profile).
    - Supports multilingual phonemization by swapping the espeak-ng phonemizer
      before each synthesis call (kittentts 0.8.x hardcodes ``en-us`` internally).
    - Returns a numpy audio array which is converted to WAV by the caller.

    When only the vendored gTTS stub is available:
    - ``voice`` is ignored (gTTS has a single voice per language).
    - ``language`` must be provided explicitly for correct phonetics.
    - Returns bytes directly.
    """

    def __init__(self, model_path: str | None = None) -> None:
        self._engine: Any | None = None
        # Lock protecting phonemizer swaps; cache avoids re-creating backends.
        self._phonemizer_lock = threading.Lock()
        self._phonemizer_cache: dict[str, Any] = {}
        if KittenTTS is None:
            return
        if _USING_VENDOR_STUB:
            # gTTS stub ignores model_path; pass it through for compat.
            self._engine = KittenTTS(model_path)
        else:
            # Real package: model_path is a HuggingFace repo ID or local dir.
            # Map sentinel values to the compact default model.
            effective = (
                _DEFAULT_KITTENTTS_MODEL
                if (model_path is None or model_path == "builtin")
                else model_path
            )
            self._engine = KittenTTS(effective)

    def _get_phonemizer(self, espeak_lang: str) -> Any:
        """Return a cached ``EspeakBackend`` for *espeak_lang*, creating if needed."""
        if espeak_lang not in self._phonemizer_cache:
            try:
                from phonemizer.backend import EspeakBackend  # type: ignore[import]

                self._phonemizer_cache[espeak_lang] = EspeakBackend(
                    language=espeak_lang,
                    preserve_punctuation=True,
                    with_stress=True,
                )
            except Exception as exc:
                log_error(
                    f"[vox/kitten] Could not create EspeakBackend for '{espeak_lang}': {exc}; "
                    "falling back to en-us."
                )
                # Re-use the English backend already cached (or create it).
                if "en-us" not in self._phonemizer_cache:
                    from phonemizer.backend import EspeakBackend  # type: ignore[import]

                    self._phonemizer_cache["en-us"] = EspeakBackend(
                        language="en-us", preserve_punctuation=True, with_stress=True
                    )
                self._phonemizer_cache[espeak_lang] = self._phonemizer_cache["en-us"]
        return self._phonemizer_cache[espeak_lang]

    def generate(
        self,
        text: str,
        voice: str = "Bella",
        language: str | None = None,
        speed: float = 1.3,
    ) -> Any:
        """Synthesise *text* and return audio (bytes or numpy array).

        The return type is ``Any`` because:
        - The vendored stub returns ``bytes`` directly.
        - The real kittentts returns a numpy array; callers must convert via
          ``_audio_to_wav()`` before writing to disk.

        For the real kittentts package the internal espeak phonemizer is swapped
        to match *language* before synthesis, then restored.  kittentts 0.8.x
        hardcodes ``en-us`` in its constructor, so without this swap non-English
        text is mispronounced.

        *speed* is forwarded to the ONNX model ( ``1.0`` = normal, ``>1`` = faster ).
        The vendored gTTS stub ignores the speed parameter.
        """
        if not self._engine:
            raise RuntimeError(
                "KittenTTS engine not available; install the 'kittentts' package"
            )
        if _USING_VENDOR_STUB:
            # gTTS stub: voice param is ignored, language drives phonetics.
            return self._engine.generate(
                text=text, voice=voice, language=language or "en"
            )
        # Real kittentts: swap the espeak phonemizer for the detected language
        # then call generate().  A lock serialises phonemizer swaps so that
        # concurrent TTS calls on the same model instance don't race.
        espeak_lang = _espeak_lang_code(language)
        log_info(
            f"[vox/kitten] generate: voice={voice!r} lang={language!r} "
            f"espeak={espeak_lang!r} speed={speed}"
        )
        model = self._engine.model  # KittenTTS_1_Onnx instance
        with self._phonemizer_lock:
            old_phonemizer = model.phonemizer
            new_phonemizer = self._get_phonemizer(espeak_lang)
            model.phonemizer = new_phonemizer
            log_info(
                f"[vox/kitten] phonemizer swapped to espeak '{espeak_lang}' (id={id(new_phonemizer)})"
            )
            try:
                result = self._engine.generate(text=text, voice=voice, speed=speed)
            finally:
                model.phonemizer = old_phonemizer
        return result

    @classmethod
    def list_voices(cls) -> list[str]:
        if not _USING_VENDOR_STUB:
            # Voices for kitten-tts-nano-0.8 (canonical names from model card).
            # Retrieving them dynamically requires loading the model first, which
            # triggers a HuggingFace download — avoid at import time.
            return ["Bella", "Jasper", "Luna", "Bruno", "Rosie", "Hugo", "Kiki", "Leo"]
        if KittenTTS is not None and hasattr(KittenTTS, "list_voices"):
            return KittenTTS.list_voices()
        return ["Bella", "Jasper", "Luna", "Bruno", "Rosie", "Hugo", "Kiki", "Leo"]


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
# Default MODEL_MANAGER model id. Mini 0.8 (~80 MB) is the higher-quality
# variant and is treated as present even before download — it is fetched
# automatically on first synthesis (see _DEFAULT_KITTENTTS_MODEL / _get_model).
_DEFAULT_MODEL = "kitten-tts-mini-0.8"
_SAMPLE_RATE = 24000

# ---------------------------------------------------------------------------
# KittenTTS model catalog — registered with MODEL_MANAGER at import time so it
# appears in the WebUI "Manage Models" list for the Vox subsystem. The model
# is downloaded from HuggingFace on demand (see MODEL_MANAGER.download).
# ---------------------------------------------------------------------------
_KITTEN_MODELS: list[ModelSpec] = [
    # Mini is the default model: it is auto-downloaded on first use when no
    # other KittenTTS model is present (see _DEFAULT_KITTENTTS_MODEL).
    ModelSpec(
        model_id="kitten-tts-mini-0.8",
        plugin_id="vox_kitten",
        display_name="KittenTTS Mini 0.8 (default)",
        description="Higher-quality multi-voice neural TTS model (~80 MB, ONNX, CPU). "
        "Default model — downloaded automatically on first use.",
        tags=["tts", "local", "cpu"],
        size_mb=80,
        voices_meta=_KITTEN_VOICE_META,
        hf_repo_id="KittenML/kitten-tts-mini-0.8",
    ),
    ModelSpec(
        model_id="kitten-tts-nano-0.8",
        plugin_id="vox_kitten",
        display_name="KittenTTS Nano 0.8",
        description="Compact multi-voice neural TTS model (~25 MB, ONNX, CPU).",
        tags=["tts", "local", "cpu"],
        size_mb=25,
        voices_meta=_KITTEN_VOICE_META,
        hf_repo_id="KittenML/kitten-tts-nano-0.8",
    ),
    ModelSpec(
        model_id="kitten-tts-micro-0.8",
        plugin_id="vox_kitten",
        display_name="KittenTTS Micro 0.8",
        description="Ultra-compact multi-voice neural TTS model (ONNX, CPU).",
        tags=["tts", "local", "cpu"],
        size_mb=15,
        voices_meta=_KITTEN_VOICE_META,
        hf_repo_id="KittenML/kitten-tts-micro-0.8",
    ),
]

for _kspec in _KITTEN_MODELS:
    MODEL_MANAGER.register(_kspec)

# Map MODEL_MANAGER model ids (e.g. "kitten-tts-nano-0.8") to the HuggingFace
# repo id the KittenTTS package expects (e.g. "KittenML/kitten-tts-nano-0.8").
# The WebUI stores the model_id in KITTEN_MODEL; the engine translates it here
# before instantiating the model.
_MODEL_ID_TO_HF_REPO: dict[str, str] = {
    _spec.model_id: _spec.hf_repo_id for _spec in _KITTEN_MODELS if _spec.hf_repo_id
}

# ---------------------------------------------------------------------------
# Localised sample texts used by KittenVoxEngine.sample()
# The {voice} placeholder is replaced with the actual speaker name so
# different personas are clearly distinguishable in the WebUI sample player.
# ---------------------------------------------------------------------------
_SAMPLE_TEXT_DEFAULT = "Hello! My name is {voice} and this is how I sound."
_SAMPLE_TEXTS: dict[str, str] = {
    "en": "Hello! My name is {voice} and this is how I sound.",
    "it": "Ciao! Mi chiamo {voice} e questo è il mio modo di parlare.",
    "fr": "Bonjour! Je m'appelle {voice} et voici comment je parle.",
    "es": "¡Hola! Me llamo {voice} y así es como sueno.",
    "de": "Hallo! Ich heiße {voice} und so klinge ich.",
    "pt": "Olá! Meu nome é {voice} e é assim que eu soo.",
    "ja": "こんにちは！私の名前は{voice}です。これが私の声です。",
    "zh": "你好！我叫{voice}，这就是我的声音。",
    "ko": "안녕하세요! 제 이름은 {voice}이고, 이것이 제 목소리입니다.",
}


# ---------------------------------------------------------------------------
# Expose engine settings in the WebUI → Components section
# ---------------------------------------------------------------------------
# Model selector — the value is a MODEL_MANAGER model id resolved to a
# HuggingFace repo id by _get_model(). The default (Mini 0.8) is treated as
# present even before download and fetched automatically on first use.
register_exposed_var(
    "KITTEN_MODEL",
    label="Kitten TTS — Model",
    default=_DEFAULT_MODEL,
    value_type=str,
    ui_type="select",
    options=[spec.model_id for spec in _KITTEN_MODELS],
    description=(
        "Which KittenTTS model variant to use. The WebUI Vox controls populate "
        "this from the downloaded models."
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

register_exposed_var(
    "KITTEN_LANGUAGE_MODELS",
    label="Kitten TTS — Language → Model mapping (JSON)",
    default="{}",
    value_type=str,
    ui_type="string",
    description=(
        "JSON object mapping ISO-639-1 language codes to KittenTTS model IDs. "
        'Example: {"it": "kitten-tts-nano-it-0.1", "en": "kitten-tts-nano-0.8"}. '
        "When a matching entry exists the specified model is used instead of the "
        "default KITTEN_MODEL.  Engines that support multiple languages can rely "
        "on this to select the right model automatically."
    ),
    scope="plugins",
    component="vox_plugin",
    advanced=True,
)

register_exposed_var(
    "KITTEN_SPEED",
    label="Kitten TTS — Speech speed",
    default=1.3,
    value_type=float,
    ui_type="string",
    description=(
        "Speaking rate multiplier. 1.0 = normal, >1 = faster, <1 = slower. "
        "Range 0.5–2.0."
    ),
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
    """Return a cached ``LocalKittenTTS`` instance for *model_id*.

    *model_id* may be a MODEL_MANAGER model id (e.g. ``"kitten-tts-nano-0.8"``),
    which is translated to its HuggingFace repo id before instantiation, the
    sentinel ``"builtin"`` (maps to the compact default variant), or a raw
    HuggingFace repo id / local dir which is passed through unchanged.
    """
    with _model_cache_lock:
        if model_id in _model_cache:
            return _model_cache[model_id]

    # Translate a MODEL_MANAGER model id to the HF repo id the package expects.
    effective_id = _MODEL_ID_TO_HF_REPO.get(model_id, model_id)

    try:
        instance = LocalKittenTTS(effective_id)  # real pkg maps "builtin" → default
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


def _normalize_wav_bytes(
    wav_bytes: bytes,
    target_level: float = 0.891,
) -> bytes:
    """Peak-normalize WAV bytes to *target_level* (default -1 dBFS).

    Uses only Python built-ins + numpy so it works even when pydub/soundfile
    are not installed.  Handles 8/16/32-bit PCM WAV.
    """
    import wave

    import numpy as np

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        nchannels = wav.getnchannels()
        sampwidth = wav.getsampwidth()
        framerate = wav.getframerate()
        nframes = wav.getnframes()
        raw = wav.readframes(nframes)

    dtype_map = {1: np.int8, 2: np.int16, 4: np.int32}
    max_int_map = {1: 127, 2: 32767, 4: 2147483647}
    dtype = dtype_map.get(sampwidth, np.int16)
    max_int = max_int_map.get(sampwidth, 32767)

    arr = np.frombuffer(raw, dtype=dtype).astype(np.float64)

    max_val = float(np.max(np.abs(arr)))
    if max_val > 1e-10:
        arr = arr * (target_level * max_int / max_val)
    arr = arr.astype(dtype)

    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(nchannels)
        wav.setsampwidth(sampwidth)
        wav.setframerate(framerate)
        wav.writeframes(arr.tobytes())
    return out.getvalue()


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

    def _active_speed(self) -> float:
        return float(
            config_registry.get_value(
                "KITTEN_SPEED",
                1.3,
                value_type=float,
                group="plugins",
                component="vox_plugin",
            )
        )

    # ------------------------------------------------------------------
    # Speaker metadata helpers (used by /api/vox/speakers endpoint)
    # ------------------------------------------------------------------

    def get_speakers(self) -> list[dict]:
        """Return list of voice dicts supported by the local engine.

        KittenTTS voices are language-agnostic: the same set of voices
        (Bella, Luna, Jasper …) works for any language — the model adapts
        phonetics to the input text language automatically.  We advertise
        ``"*"`` instead of ``"en"`` so callers never filter these out when
        the active language is not English.
        """
        return [{"code": v, "name": v, "language": "*"} for v in _KITTEN_VOICES]

    def sample(self, speaker: str, text_hint: str | None = None) -> bytes:
        """Return WAV bytes for a quick voice sample from the local engine.

        Uses a localised template that embeds the voice name so listeners
        can distinguish between personas even in the WebUI sample player.
        The real KittenTTS package renders each voice with a distinct
        acoustic profile; the vendored gTTS stub does not support multiple
        voice personas and will sound identical across voices.
        """
        if text_hint:
            text = text_hint
        else:
            template = _SAMPLE_TEXTS.get("en", _SAMPLE_TEXT_DEFAULT)
            text = template.format(voice=speaker)

        model_id = self._active_model_id()
        tts = _get_model(model_id)
        if tts is None:
            raise NotImplementedError("TTS engine unavailable")
        try:
            # Pass the speaker name explicitly so the real KittenTTS package
            # applies the correct voice model.
            # Samples always use 'en' language; LocalKittenTTS.generate() decides
            # whether to forward it to the underlying engine or not.
            speed = self._active_speed()
            audio = tts.generate(text=text, voice=speaker, language="en", speed=speed)
            if isinstance(audio, bytes):
                return audio
            wav = _audio_to_wav(audio, _SAMPLE_RATE)
            if wav is None:
                raise RuntimeError("WAV conversion failed for sample")
            return wav
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

    def _active_language_model_map(self) -> dict[str, str]:
        """Return the language → model_id map from ``KITTEN_LANGUAGE_MODELS``."""
        raw = str(
            config_registry.get_value(
                "KITTEN_LANGUAGE_MODELS",
                "{}",
                value_type=str,
                group="plugins",
                component="vox_plugin",
            )
        )
        try:
            result = json.loads(raw or "{}")
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}

    def generate_tts(
        self,
        text: str,
        emotion: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        language: str | None = kwargs.get("language")  # type: ignore[assignment]

        # Select model: prefer a language-specific model when configured.
        lang_models = self._active_language_model_map()
        if language and language in lang_models:
            model_id = str(lang_models[language])
            log_info(
                f"[vox/kitten] Using language-specific model '{model_id}' "
                f"for language '{language}'."
            )
        else:
            model_id = str(kwargs.get("model_id") or self._active_model_id())

        voice = str(
            kwargs.get("speaker") or kwargs.get("voice") or self._active_voice()
        )
        speed = float(kwargs.get("speed") or self._active_speed())

        tts = _get_model(model_id)
        if tts is None:
            return None

        try:
            # LocalKittenTTS.generate() internally decides whether to pass
            # ``language`` to the underlying engine based on _USING_VENDOR_STUB.
            audio = tts.generate(text=text, voice=voice, language=language, speed=speed)
            # Engine may return raw bytes (WAV/MP3) or a numpy array.
            if isinstance(audio, bytes):
                return _normalize_wav_bytes(audio, target_level=0.891)
            # Clean peak-normalize to -1 dBFS.  No makeup gain / hard
            # clipping here — transparent loudness is handled downstream
            # by ffmpeg dynaudnorm in broadcast_banter.
            import numpy as np

            arr = audio.numpy() if hasattr(audio, "numpy") else audio
            max_val = float(np.max(np.abs(arr)))
            if max_val > 1e-10:
                arr = (arr / max_val) * 0.891
            arr = np.clip(arr, -1.0, 1.0)
            return _audio_to_wav(arr, _SAMPLE_RATE)
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
