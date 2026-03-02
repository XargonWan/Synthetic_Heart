#!/usr/bin/env python3
"""Generate MP3 samples for all downloaded models/voices.

This helper is intended to be run manually by a developer or CI job after
models have been downloaded via the WebUI (or by invoking
``MODEL_MANAGER.download()`` directly).  It will iterate through the
registered catalog and, for each model that has one or more voices and has
already been downloaded to disk, will produce an MP3 file containing the
voice reading the sample text in the specified language.

Samples are written into ``res/synth_webui/static/audio/model_samples`` as
``<voice>_<lang>.mp3`` and are surfaced by the WebUI "Manage Models" modal as
well as the ``/api/models/{id}/voice/{voice}/sample?lang=<lang>`` endpoints.

The script will use any installed VOX engines (for example KittenTTS) when
possible, falling back to the ``edge-tts`` library.  ``edge-tts`` voices are
filtered to match the requested language and the voice's gender metadata so
samples sound correct (e.g. Bruno → male voice, Kiki → female voice).

The script does **not** automatically download models; it only operates on
models that are already present on disk.  To include a download step use the
``--download`` flag and make sure you have network access and the necessary
HuggingFace/URL dependencies installed.
"""

from __future__ import annotations

import argparse
import io
from typing import Callable

from core.model_manager import MODEL_MANAGER
from core.vox_registry import VOX_REGISTRY

try:
    from pydub import AudioSegment
except ImportError:  # pragma: no cover - optional
    AudioSegment = None  # type: ignore


def _convert_to_mp3(data: bytes) -> bytes:
    """Attempt to convert a WAV byte stream to MP3 using pydub.

    If conversion fails (missing pydub or incompatible data) the original
    bytes are returned unmodified.
    """
    if AudioSegment is None:
        return data
    try:
        audio = AudioSegment.from_file(io.BytesIO(data), format="wav")
        out = io.BytesIO()
        audio.export(out, format="mp3")
        return out.getvalue()
    except Exception:
        # leave data untouched
        return data


# ---------------------------------------------------------------------------
# edge-tts voice cache: keyed by (lang, gender) tuple
# ---------------------------------------------------------------------------
_edge_voice_cache: dict[tuple[str, str], list[str]] = {}


def _get_edge_voices(lang: str, gender: str) -> list[str]:
    """Return filtered edge-tts voice short-names for *lang* and *gender*.

    :param lang: BCP-47 language prefix, e.g. ``"en"``, ``"it"``, ``"de"``.
    :param gender: ``"M"`` / ``"F"`` / ``"N"`` (neutral = accept any gender).

    Results are cached so the API is only called once per unique combination.
    """
    cache_key = (lang, gender)
    if cache_key in _edge_voice_cache:
        return _edge_voice_cache[cache_key]

    try:
        import asyncio
        import edge_tts  # type: ignore[import]

        all_voices = asyncio.run(asyncio.wait_for(edge_tts.list_voices(), timeout=15))
    except Exception:
        _edge_voice_cache[cache_key] = []
        return []

    locale_prefix = f"{lang}-"
    gender_map = {"M": "male", "F": "female"}
    want_gender = gender_map.get(gender)

    filtered = [
        v["ShortName"]
        for v in all_voices
        if v.get("Locale", "").startswith(locale_prefix)
        and (want_gender is None or v.get("Gender", "").lower() == want_gender)
    ]

    if not filtered:
        # Relax gender constraint and retry with the locale only
        filtered = [
            v["ShortName"]
            for v in all_voices
            if v.get("Locale", "").startswith(locale_prefix)
        ]

    if not filtered:
        # Last resort: pick any voice from a wider locale family
        broader = lang.split("-")[0]
        filtered = [
            v["ShortName"]
            for v in all_voices
            if v.get("Locale", "").lower().startswith(broader)
        ]

    _edge_voice_cache[cache_key] = filtered
    return filtered


def _edge_generate(
    text: str,
    voice: str | None,
    lang: str = "en",
    gender: str = "N",
) -> bytes | None:
    """Use ``edge-tts`` to synthesise *text* into an MP3 byte stream.

    A voice is chosen deterministically based on the combination of *voice*
    identifier, *lang* and *gender* so results are reproducible across runs.
    Returns ``None`` when ``edge-tts`` is unavailable or any error occurs.
    """
    try:
        import edge_tts  # noqa: F401  (import just to verify it's installed)
    except ImportError:  # pragma: no cover
        return None

    import asyncio
    import hashlib
    import os
    import tempfile

    voices = _get_edge_voices(lang, gender)
    if not voices:
        # Absolute fallback: English female — always available on edge-tts
        voices = _get_edge_voices("en", "F") or _get_edge_voices("en", "N")
    if not voices:
        return None

    # Deterministic selection: sha256 of "<voice>_<lang>" to keep the
    # assignment stable even when the voice list changes slightly.
    key = f"{voice or 'default'}_{lang}"
    idx = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % len(voices)
    chosen = voices[idx]

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    fname = tmp.name
    tmp.close()
    try:

        async def _run_save() -> None:
            import edge_tts as et  # type: ignore[import]

            await et.Communicate(text, chosen).save(fname)

        asyncio.run(asyncio.wait_for(_run_save(), timeout=20))
        with open(fname, "rb") as f:
            data = f.read()
    except Exception as exc:  # pragma: no cover
        from core.logging_utils import log_warning

        log_warning(f"[generate_model_samples] edge-tts failed ({exc}), skipping")
        data = None
    finally:
        try:
            os.unlink(fname)
        except Exception:
            pass
    return data


def _tweak_audio(data: bytes, voice: str | None, lang: str = "en") -> bytes:
    """Return a lightly modified version of *data* based on *voice* + *lang*.

    When the same native TTS engine is used for every voice we still want each
    sample file to feel different.  We deterministically shift the pitch by a
    few semitones; if ``pydub`` / ffmpeg is unavailable we append a small hash
    suffix so file contents at least differ between voices.
    """
    if AudioSegment is None or voice is None:
        return data
    try:
        import hashlib

        audio = AudioSegment.from_file(io.BytesIO(data), format="mp3")
        h = int(hashlib.sha256(f"{voice}_{lang}".encode()).hexdigest(), 16)
        semitones = (h % 5) - 2  # range[-2, 2]
        factor = 2.0 ** (semitones / 12.0)
        new_rate = int(audio.frame_rate * factor)
        modified = audio._spawn(audio.raw_data, overrides={"frame_rate": new_rate})
        modified = modified.set_frame_rate(audio.frame_rate)
        out = io.BytesIO()
        modified.export(out, format="mp3")
        return out.getvalue()
    except Exception:
        try:
            import hashlib

            suffix = hashlib.sha256(f"{voice}_{lang}".encode()).digest()[:8]
            return data + suffix
        except Exception:
            return data


def _make_generate_callback(
    gender: str = "N",
    lang: str = "en",
) -> Callable[[str, str | None], "bytes | None"]:
    """Return a generate_fn closure bound to *gender* and *lang*."""
    import inspect

    def _generate_callback(text: str, voice: str | None) -> bytes | None:
        """Produce sample audio bytes for *text* and *voice*.

        Tries registered VOX engines first (respecting 1- vs 2-arg ``sample()``
        signature), then falls back to ``edge-tts`` with the correct locale and
        gender filter.
        """
        text_to_say = text if voice is None else f"{text}"

        for engine_name in VOX_REGISTRY.get_available_engines():
            try:
                engine = VOX_REGISTRY.load_engine(engine_name)
            except Exception:
                continue
            if not hasattr(engine, "sample"):
                continue
            try:
                sig = inspect.signature(engine.sample)
                if len(sig.parameters) == 2:
                    data = engine.sample(text_to_say, voice)
                else:
                    data = engine.sample(voice)
            except Exception:
                continue
            if data:
                return _tweak_audio(_convert_to_mp3(data), voice, lang)

        # Fallback: edge-tts with correct locale + gender
        data = _edge_generate(text_to_say, voice, lang=lang, gender=gender)
        if data:
            return _tweak_audio(data, voice, lang)
        return None

    return _generate_callback


# Ensure plugin modules are imported so they can register their models with
# MODEL_MANAGER.  This is particularly important when running as a stand-alone
# script where the normal application import machinery may not have triggered
# plugin loading.
try:
    import plugins.vox_engines.kitten  # registers KittenTTS specs
except ImportError:
    pass
try:
    import plugins.auris_engines.vosk  # noqa: F401  — registers Vosk specs
except ImportError:
    pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate MP3 samples for all downloaded models"
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Attempt to download missing models before generating samples",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate samples even if they already exist",
    )
    parser.add_argument(
        "--lang",
        metavar="CODE",
        default=None,
        help=(
            "Only generate samples for this language code (e.g. 'en', 'it'). "
            "Defaults to all languages supported by each model."
        ),
    )
    args = parser.parse_args()

    if args.download:
        print("Downloading all registered models (may take a while)...")
        for entry in MODEL_MANAGER.catalog():
            mid = entry["model_id"]
            if not MODEL_MANAGER.is_downloaded(mid):
                print(f" - downloading {mid}...", end="")
                import asyncio as _asyncio

                _asyncio.run(MODEL_MANAGER.download(mid))
                print(" done")

    for entry in MODEL_MANAGER.catalog():
        mid = entry["model_id"]
        if not MODEL_MANAGER.is_downloaded(mid):
            print(f"skipping {mid} (not downloaded)")
            continue

        spec = MODEL_MANAGER.get_spec(mid)
        if not spec or not spec.voices:
            continue

        # Determine which languages to generate
        supported_langs = spec.supported_languages or ["en"]
        langs_to_gen = [args.lang] if args.lang else supported_langs

        # Build voice → gender map from voices_meta (falls back to "N")
        gender_map: dict[str, str] = {}
        for vm in spec.voices_meta:
            gender_map[vm.name] = vm.gender

        for lang in langs_to_gen:
            for voice in spec.voices:
                gender = gender_map.get(voice, "N")

                # Remove existing sample if forcing regeneration
                path = MODEL_MANAGER._sample_path(mid, voice, lang)
                if args.force and path and path.exists():
                    try:
                        path.unlink()
                    except Exception:
                        pass

                print(f"generating sample for {mid}/{voice}/{lang}…", end=" ")
                generate_fn = _make_generate_callback(gender=gender, lang=lang)
                path = MODEL_MANAGER.ensure_sample(mid, voice, generate_fn, lang=lang)
                if path:
                    print(f"ok ({path})")
                else:
                    print("failed")

    print("\ndone")


if __name__ == "__main__":
    main()
