"""KittenTTS: local, neural, multi-voice text-to-speech (ONNX, CPU).

The model is fetched from HuggingFace on first use (``kittentts`` downloads and caches it)
and kept in memory per model id. Voices are language-agnostic: the model adapts its phonetics
to the text, and the espeak-ng phonemizer is swapped per call so non-English text is not
read with English rules (kittentts 0.8.x hardcodes ``en-us`` inside its constructor).
"""

from __future__ import annotations

import io
import json
import threading
import wave
from typing import Any

DEFAULT_MODEL = "kitten-tts-mini-0.8"
DEFAULT_VOICE = "Bella"
DEFAULT_SPEED = 1.3
SAMPLE_RATE = 24000

#: The models the engine offers: id -> (HuggingFace repo, label, approximate size in MB).
MODELS: dict[str, tuple[str, str, int]] = {
    "kitten-tts-mini-0.8": (
        "KittenML/kitten-tts-mini-0.8",
        "KittenTTS Mini 0.8 (default): higher-quality multi-voice model, ONNX, CPU",
        80,
    ),
    "kitten-tts-nano-0.8": (
        "KittenML/kitten-tts-nano-0.8",
        "KittenTTS Nano 0.8: compact multi-voice model, ONNX, CPU",
        25,
    ),
    "kitten-tts-micro-0.8": (
        "KittenML/kitten-tts-micro-0.8",
        "KittenTTS Micro 0.8: ultra-compact multi-voice model, ONNX, CPU",
        15,
    ),
}

# The model card's canonical voice names; a voice is language-agnostic.
VOICES: tuple[tuple[str, str], ...] = (
    ("Bella", "F"),
    ("Jasper", "M"),
    ("Luna", "F"),
    ("Bruno", "M"),
    ("Rosie", "F"),
    ("Hugo", "M"),
    ("Kiki", "F"),
    ("Leo", "M"),
)

# ISO 639-1 -> espeak-ng language identifier, to replace kittentts' hardcoded ``en-us``.
ESPEAK_LANG: dict[str, str] = {
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
    # Japanese: espeak-ng 'ja' is not reliably present and kittentts was trained on
    # Latin-script phonemes; Italian shares its open vowels and clear consonants, so it is
    # more intelligible than the English fallback.
    "ja": "it",
    "jp": "it",
}


class Log:
    """Where this module reports; ``ext_main`` points it at ``ctx.log``."""

    target: Any = None

    @classmethod
    def info(cls, message: str) -> None:
        if cls.target is not None:
            cls.target.info(message)

    @classmethod
    def error(cls, message: str) -> None:
        if cls.target is not None:
            cls.target.error(message)


def espeak_lang_code(lang: str | None) -> str:
    """The espeak-ng code for an ISO 639-1 tag (``it`` and ``it-IT`` alike); ``en-us`` if unknown."""
    if not lang:
        return "en-us"
    return ESPEAK_LANG.get(lang.split("-")[0].lower(), "en-us")


class LocalKittenTTS:
    """Wrapper around the ``kittentts`` package: one loaded model, thread-safe phonemizer swaps."""

    def __init__(self, repo: str, factory: Any) -> None:
        self._engine = factory(repo)
        self._lock = threading.Lock()
        self._phonemizers: dict[str, Any] = {}

    def _phonemizer(self, espeak_lang: str) -> Any:
        if espeak_lang not in self._phonemizers:
            from phonemizer.backend import EspeakBackend  # ty: ignore[unresolved-import]

            try:
                self._phonemizers[espeak_lang] = EspeakBackend(
                    language=espeak_lang, preserve_punctuation=True, with_stress=True
                )
            except Exception as exc:  # noqa: BLE001
                Log.error(
                    f"[vox/kitten] no espeak backend for {espeak_lang!r}: {exc}; using en-us"
                )
                if "en-us" not in self._phonemizers:
                    self._phonemizers["en-us"] = EspeakBackend(
                        language="en-us", preserve_punctuation=True, with_stress=True
                    )
                self._phonemizers[espeak_lang] = self._phonemizers["en-us"]
        return self._phonemizers[espeak_lang]

    def generate(
        self, text: str, voice: str, language: str | None, speed: float
    ) -> Any:
        """The audio of ``text`` (a numpy array). The phonemizer is swapped for the language
        and restored; a lock serialises the swap so concurrent calls do not race."""
        espeak_lang = espeak_lang_code(language)
        Log.info(
            f"[vox/kitten] generate: voice={voice!r} lang={language!r} espeak={espeak_lang!r} speed={speed}"
        )
        model = self._engine.model
        with self._lock:
            previous = model.phonemizer
            model.phonemizer = self._phonemizer(espeak_lang)
            try:
                return self._engine.generate(text=text, voice=voice, speed=speed)
            finally:
                model.phonemizer = previous


_models: dict[str, LocalKittenTTS] = {}
_models_lock = threading.Lock()


def load_model(model_id: str, factory: Any = None) -> LocalKittenTTS:
    """The cached engine for a model id (a catalogue id or a raw HuggingFace repo / folder)."""
    with _models_lock:
        if model_id in _models:
            return _models[model_id]
    repo = MODELS[model_id][0] if model_id in MODELS else model_id
    if factory is None:
        from kittentts import KittenTTS  # ty: ignore[unresolved-import]

        factory = KittenTTS
    instance = LocalKittenTTS(repo, factory)
    with _models_lock:
        _models[model_id] = instance
    return instance


def to_wav(audio: Any, sample_rate: int = SAMPLE_RATE) -> bytes:
    """A float audio array as 16-bit mono WAV bytes, peak-normalised to -1 dBFS.

    Clean peak normalisation only: no makeup gain and no hard clipping, because transparent
    loudness is handled downstream (ffmpeg ``dynaudnorm`` when broadcasting).
    """
    import numpy as np

    arr = audio.numpy() if hasattr(audio, "numpy") else audio
    arr = np.asarray(arr, dtype=np.float64)
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    if peak > 1e-10:
        arr = arr / peak * 0.891
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


def parse_language_models(raw: Any) -> dict[str, str]:
    """``{"it": "kitten-tts-nano-it-0.1"}`` from the setting's JSON text; ``{}`` if unusable."""
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def speakers() -> list[dict[str, str]]:
    return [
        {"reference_id": name, "title": name, "language": "*", "gender": gender}
        for name, gender in VOICES
    ]


def model_infos() -> list[dict[str, Any]]:
    return [
        {
            "id": model_id,
            "name": label,
            "owned_by": "KittenML",
            "capabilities": {"vox": True},
            "model_type": "tts",
            "input_modalities": ["text"],
            "output_modalities": ["audio"],
        }
        for model_id, (_, label, _size) in MODELS.items()
    ]


def synthesize(
    text: str,
    voice: str | None,
    kwargs: dict[str, Any],
    *,
    default_model: str,
    language_models: dict[str, str],
    speed: float,
    factory: Any = None,
) -> bytes | None:
    """The text as WAV bytes (blocking: run it in a thread), or ``None`` on failure."""
    language = kwargs.get("language") or None
    if language and language in language_models:
        model_id = language_models[language]
        Log.info(f"[vox/kitten] using the model {model_id!r} for language {language!r}")
    else:
        model_id = str(kwargs.get("model") or kwargs.get("model_id") or default_model)
    chosen_voice = str(voice or kwargs.get("speaker") or DEFAULT_VOICE)
    try:
        chosen_speed = float(kwargs.get("speed") or speed)
    except (TypeError, ValueError):
        chosen_speed = speed
    try:
        engine = load_model(model_id, factory)
        return to_wav(engine.generate(text, chosen_voice, language, chosen_speed))
    except Exception as exc:  # noqa: BLE001 - the caller falls back to text
        Log.error(f"[vox/kitten] TTS generation failed: {exc}")
        return None
