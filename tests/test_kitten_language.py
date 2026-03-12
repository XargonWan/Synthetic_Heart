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
    """generate_tts() must forward the 'language' kwarg to the underlying model."""
    from plugins.vox_engines.kitten import KittenVoxEngine, LocalKittenTTS

    calls: list[dict[str, Any]] = []

    class FakeTTS:
        def generate(
            self, text: str, voice: str = "Bella", language: str = "en"
        ) -> bytes:
            calls.append({"text": text, "voice": voice, "language": language})
            # return a minimal valid WAV header so the engine doesn't try to
            # convert non-bytes output.
            import wave
            import io

            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(b"\x00" * 100)
            return buf.getvalue()

    fake_local = LocalKittenTTS.__new__(LocalKittenTTS)
    fake_local._engine = FakeTTS()

    engine = KittenVoxEngine()
    with patch("plugins.vox_engines.kitten._get_model", return_value=fake_local):
        result = engine.generate_tts("Ciao mondo", language="it")

    assert result is not None
    assert calls, "generate() was never called"
    assert calls[0]["language"] == "it", (
        f"Expected language='it' but got {calls[0]['language']!r}"
    )


@pytest.mark.asyncio
async def test_kitten_engine_uses_language_model_map() -> None:
    """When KITTEN_LANGUAGE_MODELS maps a language to a model_id that model is used."""
    from plugins.vox_engines.kitten import KittenVoxEngine, LocalKittenTTS

    loaded_model_ids: list[str] = []

    def fake_get_model(model_id: str) -> Any:
        loaded_model_ids.append(model_id)

        class FakeTTS:
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
    from plugins.vox_engines.kitten import KittenVoxEngine, LocalKittenTTS

    class FakeTTS:
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

    engine = KittenVoxEngine()
    with patch("plugins.vox_engines.kitten._get_model", return_value=fake_local):
        # No 'language' kwarg — should not raise.
        result = engine.generate_tts("Hello world")

    assert result is not None


# ---------------------------------------------------------------------------
# vendored KittenTTS stub — language forwarding
# ---------------------------------------------------------------------------


def test_vendor_kittentts_stub_accepts_language() -> None:
    """KittenTTS.generate() must accept and honour the 'language' parameter."""
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
