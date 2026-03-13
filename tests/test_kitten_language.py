"""Tests for KittenTTS language selection in KittenVoxEngine."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# KittenVoxEngine — language kwarg propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kitten_engine_passes_language_to_generate() -> None:
    """generate_tts() language handling differs by backend:

    - Real kittentts package: the internal espeak phonemizer is swapped to the
      detected language before generate() is called, so generate() receives
      (text, voice) ONLY — no ``language`` kwarg in the call signature.
      The phonemizer swap is verified by inspecting ``_get_phonemizer`` calls.
    - Vendor gTTS stub: language must be forwarded explicitly so gTTS picks the
      right phonetic model — generate() is called WITH language=<code>.
    """
    from plugins.vox_engines.kitten import (
        KittenVoxEngine,
        LocalKittenTTS,
        _USING_VENDOR_STUB,
    )

    calls: list[dict[str, Any]] = []

    class FakeTTS:
        def generate(self, text: str, voice: str = "Bella", **kwargs: Any) -> bytes:
            calls.append({"text": text, "voice": voice, **kwargs})
            import io
            import wave

            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(b"\x00" * 100)
            return buf.getvalue()

    class FakeModel:
        """Minimal stand-in for KittenTTS_1_Onnx — has a mutable phonemizer attr."""

        phonemizer: Any = None

    fake_local = LocalKittenTTS.__new__(LocalKittenTTS)
    fake_local._engine = FakeTTS()
    fake_local._phonemizer_lock = __import__("threading").Lock()
    fake_local._phonemizer_cache = {}
    if not _USING_VENDOR_STUB:
        # Real package path reads self._engine.model.phonemizer.
        fake_local._engine.model = FakeModel()  # type: ignore[attr-defined]

    phonemizer_langs: list[str] = []

    def fake_get_phonemizer(lang: str) -> Any:
        phonemizer_langs.append(lang)
        return object()  # any sentinel — just needs to be assignable

    engine = KittenVoxEngine()
    with patch("plugins.vox_engines.kitten._get_model", return_value=fake_local):
        if not _USING_VENDOR_STUB:
            fake_local._get_phonemizer = fake_get_phonemizer  # type: ignore[method-assign]
        result = engine.generate_tts("Ciao mondo", language="it")

    assert result is not None
    assert calls, "generate() was never called"
    if _USING_VENDOR_STUB:
        # Stub: language must be forwarded to gTTS.
        assert calls[0].get("language") == "it", (
            f"Stub mode: expected language='it' but got {calls[0]!r}"
        )
    else:
        # Real package: 'language' is NOT a kwarg of generate() — phonemizer swap instead.
        assert "language" not in calls[0], (
            f"Real package mode: language should NOT be in generate() call, got {calls[0]!r}"
        )
        assert "it" in phonemizer_langs, (
            f"Real package mode: expected phonemizer for 'it', got {phonemizer_langs!r}"
        )


@pytest.mark.asyncio
async def test_kitten_engine_uses_language_model_map() -> None:
    """When KITTEN_LANGUAGE_MODELS maps a language to a model_id that model is used."""
    from plugins.vox_engines.kitten import KittenVoxEngine, LocalKittenTTS

    loaded_model_ids: list[str] = []

    def fake_get_model(model_id: str) -> Any:
        loaded_model_ids.append(model_id)

        class FakeModel:
            phonemizer: Any = None

        class FakeTTS:
            model = FakeModel()

            def generate(
                self, text: str, voice: str = "Bella", language: str = "en"
            ) -> bytes:
                import io
                import wave

                buf = io.BytesIO()
                with wave.open(buf, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(24000)
                    wf.writeframes(b"\x00" * 100)
                return buf.getvalue()

        fake = LocalKittenTTS.__new__(LocalKittenTTS)
        fake._engine = FakeTTS()
        fake._phonemizer_lock = __import__("threading").Lock()
        fake._phonemizer_cache = {}
        return fake

    engine = KittenVoxEngine()

    with (
        patch("plugins.vox_engines.kitten._get_model", side_effect=fake_get_model),
        patch.object(
            engine,
            "_active_language_model_map",
            return_value={"it": "kitten-tts-nano-it-0.1", "en": "kitten-tts-nano-0.8"},
        ),
    ):
        engine.generate_tts("Ciao mondo", language="it")

    assert loaded_model_ids, "_get_model was never called"
    assert loaded_model_ids[0] == "kitten-tts-nano-it-0.1", (
        f"Expected Italian model but got {loaded_model_ids[0]!r}"
    )


def test_kitten_engine_defaults_to_english_when_no_language() -> None:
    """Without a language kwarg the engine defaults to the configured model."""
    from plugins.vox_engines.kitten import KittenVoxEngine, LocalKittenTTS, _USING_VENDOR_STUB

    class FakeModel:
        phonemizer: Any = None

    class FakeTTS:
        model = FakeModel()

        def generate(
            self, text: str, voice: str = "Bella", language: str = "en"
        ) -> bytes:
            import io
            import wave

            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(b"\x00" * 100)
            return buf.getvalue()

    fake_local = LocalKittenTTS.__new__(LocalKittenTTS)
    fake_local._engine = FakeTTS()
    fake_local._phonemizer_lock = __import__("threading").Lock()
    fake_local._phonemizer_cache = {}

    engine = KittenVoxEngine()
    with patch("plugins.vox_engines.kitten._get_model", return_value=fake_local):
        # No 'language' kwarg — should not raise.
        result = engine.generate_tts("Hello world")

    assert result is not None


# ---------------------------------------------------------------------------
# vendored KittenTTS stub — language forwarding
# ---------------------------------------------------------------------------


def test_vendor_kittentts_stub_accepts_language() -> None:
    """KittenTTS vendor stub: generate() must pass 'language' to gTTS.

    Skipped when the real kittentts package is installed because in that case
    gTTS is never used and the language is handled by the neural model itself.
    """
    from plugins.vox_engines.kitten import _USING_VENDOR_STUB

    if not _USING_VENDOR_STUB:
        pytest.skip("Real kittentts package is installed; gTTS stub is not in use.")

    try:
        from kittentts import KittenTTS
    except ImportError:
        pytest.skip("kittentts stub not importable")

    tts = KittenTTS()
    gtts_calls: list[dict] = []

    class FakeGTTS:
        def __init__(self, text: str, lang: str) -> None:
            gtts_calls.append({"text": text, "lang": lang})

        def write_to_fp(self, buf: Any) -> None:
            buf.write(b"ID3")  # minimal MP3 stub

    from unittest.mock import MagicMock

    fake_seg = MagicMock()
    fake_seg.export = MagicMock(
        side_effect=lambda buf, format: buf.write(b"RIFF" + b"\x00" * 36)
    )

    with (
        patch("gtts.gTTS", FakeGTTS),
        patch("pydub.AudioSegment.from_file", return_value=fake_seg),
    ):
        try:
            tts.generate(text="Ciao mondo", language="it")
        except Exception:
            # export may fail in test env; what matters is gtts was called with lang="it"
            pass

    assert gtts_calls, "gTTS was never called"
    assert gtts_calls[0]["lang"] == "it", (
        f"Expected lang='it' but got {gtts_calls[0]['lang']!r}"
    )
