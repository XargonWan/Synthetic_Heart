import pytest

from plugins.auris_engines.vosk_engine import (
    _resolve_auto_language,
    _get_configured_language,
    _detect_language_with_whisper,
)


@pytest.mark.parametrize("configured,expected", [("en-us", "en-us"), ("it", "it")])
def test_get_configured_language_respects_env(monkeypatch, configured, expected):
    # _get_configured_language reads from config_registry; monkeypatch it
    class DummyRegistry:
        def get_value(self, *args, **kwargs):
            return configured

    monkeypatch.setattr("core.config_manager.config_registry", DummyRegistry())
    assert _get_configured_language() == expected


def test_get_configured_language_defaults_to_auto(monkeypatch):
    # when config registry returns None/empty, default should be 'auto'
    class DummyRegistry:
        def get_value(self, *args, **kwargs):
            return None

    monkeypatch.setattr("core.config_manager.config_registry", DummyRegistry())
    assert _get_configured_language() == "auto"


@pytest.mark.asyncio
async def test_resolve_auto_language_prefers_whisper(monkeypatch):
    # simulate whisper detection returning 'es'
    monkeypatch.setattr(
        "plugins.auris_engines.vosk_engine._detect_language_with_whisper",
        lambda audio_path: "es",
    )
    lang = _resolve_auto_language(audio_path="dummy.wav")
    assert lang == "es"


@pytest.mark.asyncio
async def test_resolve_auto_language_fallbacks_to_first_downloaded(monkeypatch):
    # whisper returns None, so should fallback to model manager data
    monkeypatch.setattr(
        "plugins.auris_engines.vosk_engine._detect_language_with_whisper",
        lambda audio_path: None,
    )

    # fake MODEL_MANAGER.downloaded_models()
    class FakeModel:
        def __init__(self, model_id, language):
            self.model_id = model_id
            self.language = language

    fake_list = [
        {"plugin_id": "auris_vosk", "model_id": "vosk-fr-fr", "language": "fr"}
    ]
    monkeypatch.setattr(
        "core.model_manager.MODEL_MANAGER.downloaded_models",
        lambda: fake_list,
    )
    lang = _resolve_auto_language(audio_path=None)
    assert lang == "fr"


@pytest.mark.asyncio
async def test_resolve_auto_language_last_resort(monkeypatch):
    # nothing available, return en-us
    monkeypatch.setattr(
        "plugins.auris_engines.vosk_engine._detect_language_with_whisper",
        lambda audio_path: None,
    )
    monkeypatch.setattr(
        "core.model_manager.MODEL_MANAGER.downloaded_models",
        lambda: [],
    )
    lang = _resolve_auto_language(audio_path=None)
    assert lang == "en-us"


def test_detect_language_when_whisper_missing(monkeypatch):
    # simulate faster-whisper import failure: the loader returns None
    monkeypatch.setattr(
        "plugins.auris_engines.vosk_engine._get_whisper_lid_model",
        lambda: None,
    )
    # detection should quietly return None
    assert _detect_language_with_whisper("dummy.wav") is None
