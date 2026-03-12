"""Tests for Auris STT registry and plugin."""

from __future__ import annotations

from unittest.mock import patch

import sys
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# AurisRegistry unit tests
# ---------------------------------------------------------------------------


def test_registry_register_and_list() -> None:
    from core.auris_registry import AurisRegistry

    reg = AurisRegistry()
    reg.register_engine("test", "some.module.path", {"file_based": True}, "Test engine")
    assert "test" in reg.get_available_engines()


def test_registry_unknown_engine_raises() -> None:
    from core.auris_registry import AurisRegistry

    reg = AurisRegistry()
    with pytest.raises(ValueError, match="Unknown engine"):
        reg.load_engine("nonexistent")


def test_registry_find_by_capabilities() -> None:
    from core.auris_registry import AurisRegistry

    reg = AurisRegistry()
    reg.register_engine(
        "local_rt", "mod", {"file_based": True, "realtime": True, "local": True}
    )
    reg.register_engine(
        "cloud_file", "mod2", {"file_based": True, "realtime": False, "local": False}
    )

    result = reg.find_engine_by_capabilities({"realtime": True})
    assert result == "local_rt"

    result2 = reg.find_engine_by_capabilities({"local": True})
    assert result2 == "local_rt"


def test_registry_load_engine_missing_engine_class() -> None:
    import types
    from core.auris_registry import AurisRegistry

    dummy_mod = types.ModuleType("fake_auris_engine")
    # No ENGINE_CLASS attribute

    reg = AurisRegistry()
    reg._engine_modules["bad"] = "fake_auris_engine"

    with patch("importlib.import_module", return_value=dummy_mod):
        with pytest.raises(ValueError, match="ENGINE_CLASS"):
            reg.load_engine("bad")


def test_registry_load_engine_caches_instance() -> None:
    import types
    from core.auris_registry import AurisRegistry
    from plugins.auris_base import AurisEngineBase, AurisTranscriptResult

    class FakeEngine(AurisEngineBase):
        def transcribe(
            self, file_path: str, mime_type: str | None = None
        ) -> AurisTranscriptResult | None:
            return AurisTranscriptResult(text="hello", language="en")

    dummy_mod = types.ModuleType("fake_m")
    dummy_mod.ENGINE_CLASS = FakeEngine  # type: ignore[attr-defined]

    reg = AurisRegistry()
    reg._engine_modules["fake"] = "fake_m"
    with patch("importlib.import_module", return_value=dummy_mod):
        inst1 = reg.load_engine("fake")
        inst2 = reg.load_engine("fake")
    assert inst1 is inst2


# ---------------------------------------------------------------------------
# AurisPlugin integration-style tests (mocked engine)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auris_plugin_transcribe_disabled() -> None:
    """When the active engine is 'disabled' the plugin returns None."""
    with (
        patch("core.core_initializer.register_plugin"),
        patch.object(
            __import__(
                "core.config_manager", fromlist=["config_registry"]
            ).config_registry,
            "get_value",
            side_effect=lambda key, default=None, **kwargs: (
                "disabled" if key == "ACTIVE_AURIS_ENGINE" else default
            ),
        ),
    ):
        from plugins.auris_plugin import AurisPlugin

        plugin = AurisPlugin.__new__(AurisPlugin)
        plugin._active_engine_name = "disabled"
        plugin._engine_settings = {}

        result = await plugin.transcribe_audio("/tmp/fake.wav")
        assert result is None


@pytest.mark.asyncio
async def test_auris_plugin_transcribe_calls_engine() -> None:
    """transcribe_audio should call the engine's transcribe method."""
    from core.auris_registry import AurisRegistry
    from plugins.auris_base import AurisEngineBase, AurisTranscriptResult

    class MockEngine(AurisEngineBase):
        def transcribe(
            self, file_path: str, mime_type: str | None = None
        ) -> AurisTranscriptResult | None:
            return AurisTranscriptResult(text="transcribed text", language="en")

    mock_registry = AurisRegistry()
    mock_registry._engine_modules["mock"] = "mock_module"
    mock_registry._instances["mock"] = MockEngine()

    from plugins.auris_plugin import AurisPlugin

    plugin = AurisPlugin.__new__(AurisPlugin)
    plugin._active_engine_name = "mock"
    plugin._engine_settings = {}

    with (
        patch("plugins.auris_plugin.AURIS_REGISTRY", mock_registry),
        patch("os.path.exists", return_value=True),
        patch.object(plugin, "refresh_config"),
    ):
        result = await plugin.transcribe_audio("/tmp/fake.wav")
    assert result is not None
    assert result.text == "transcribed text"
    assert result.language == "en"


@pytest.mark.asyncio
async def test_stt_transcribe_action_includes_language() -> None:
    """handle_custom_action('stt_transcribe') should include 'language' in success result."""
    from core.auris_registry import AurisRegistry
    from plugins.auris_base import AurisEngineBase, AurisTranscriptResult
    from plugins.auris_plugin import AurisPlugin

    class MockEngine(AurisEngineBase):
        def transcribe(
            self, file_path: str, mime_type: str | None = None
        ) -> AurisTranscriptResult | None:
            return AurisTranscriptResult(text="ciao mondo", language="it")

    mock_registry = AurisRegistry()
    mock_registry._engine_modules["mock"] = "mock_module"
    mock_registry._instances["mock"] = MockEngine()

    plugin = AurisPlugin.__new__(AurisPlugin)
    plugin._active_engine_name = "mock"
    plugin._engine_settings = {}

    with (
        patch("plugins.auris_plugin.AURIS_REGISTRY", mock_registry),
        patch("os.path.exists", return_value=True),
        patch.object(plugin, "refresh_config"),
    ):
        result = await plugin.handle_custom_action(
            "stt_transcribe", {"audio_path": "/tmp/fake.wav"}
        )

    assert result["status"] == "success"
    assert result["text"] == "ciao mondo"
    assert result.get("language") == "it"


# ---------------------------------------------------------------------------
# Auto-download behaviour tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vosk_autodownload_flag(monkeypatch, tmp_path):
    """When MODEL_AUTO_DOWNLOAD is true the vosk engine should fetch missing models.

    We register a dummy model with MODEL_MANAGER, patch the download function to
    create the directory, and monkeypatch the vosk package to avoid import errors.
    """
    from core.config_manager import config_registry as cfg
    from core.model_manager import MODEL_MANAGER, ModelSpec
    from plugins.auris_engines import vosk_engine

    model_id = "vosk-test"
    if MODEL_MANAGER.get_spec(model_id) is None:
        MODEL_MANAGER.register(
            ModelSpec(
                model_id=model_id,
                plugin_id="auris_vosk",
                display_name="dummy",
                description="",
                tags=[],
                size_mb=1,
            )
        )
    # ensure directory does not exist and clear any cached load result
    dest = MODEL_MANAGER.model_dir(model_id)
    key = str(dest.resolve())
    from plugins.auris_engines.vosk_engine import _MODEL_CACHE

    _MODEL_CACHE.pop(key, None)
    if dest.exists():
        for child in dest.rglob("*"):
            if child.is_file():
                child.unlink()
        dest.rmdir()

    # patch config to enable auto download
    orig_get = cfg.get_value

    def fake_get(key, default=None, **kwargs):
        if key == "MODEL_AUTO_DOWNLOAD":
            return True
        return orig_get(key, default, **kwargs)

    monkeypatch.setattr(cfg, "get_value", fake_get)

    called = []

    def fake_download_sync(mid, on_progress=None):
        called.append(mid)
        MODEL_MANAGER.model_dir(mid).mkdir(parents=True, exist_ok=True)
        return True

    monkeypatch.setattr(MODEL_MANAGER, "_download_sync", fake_download_sync)

    async def fake_download(mid, on_progress=None):
        return fake_download_sync(mid, on_progress)

    monkeypatch.setattr(MODEL_MANAGER, "download", fake_download)

    class DummyModel:
        def __init__(self, path):
            self.path = path

    dummy = SimpleNamespace(Model=DummyModel, SetLogLevel=lambda lvl: None)
    monkeypatch.setitem(sys.modules, "vosk", dummy)

    result = vosk_engine._load_model(dest)
    assert isinstance(result, DummyModel)
    assert called == [model_id]


@pytest.mark.asyncio
async def test_vosk_autodownload_skipped(monkeypatch, tmp_path):
    """With auto-download disabled the engine should log a warning and not attempt."""
    from core.config_manager import config_registry as cfg
    from core.model_manager import MODEL_MANAGER, ModelSpec
    from plugins.auris_engines import vosk_engine

    model_id = "vosk-other"
    if MODEL_MANAGER.get_spec(model_id) is None:
        MODEL_MANAGER.register(
            ModelSpec(
                model_id=model_id,
                plugin_id="auris_vosk",
                display_name="dummy2",
                description="",
                tags=[],
                size_mb=1,
            )
        )
    dest = MODEL_MANAGER.model_dir(model_id)
    if dest.exists():
        for child in dest.rglob("*"):
            if child.is_file():
                child.unlink()
        dest.rmdir()

    orig_get = cfg.get_value

    def fake_get(key, default=None, **kwargs):
        if key == "MODEL_AUTO_DOWNLOAD":
            return False
        return orig_get(key, default, **kwargs)

    monkeypatch.setattr(cfg, "get_value", fake_get)

    called = []

    def fake_download_sync(mid, on_progress=None):
        called.append(mid)
        MODEL_MANAGER.model_dir(mid).mkdir(parents=True, exist_ok=True)
        return True

    monkeypatch.setattr(MODEL_MANAGER, "_download_sync", fake_download_sync)

    async def fake_download(mid, on_progress=None):
        return fake_download_sync(mid, on_progress)

    monkeypatch.setattr(MODEL_MANAGER, "download", fake_download)

    class DummyModel2:
        def __init__(self, path):
            pass

    dummy2 = SimpleNamespace(Model=DummyModel2, SetLogLevel=lambda lvl: None)
    monkeypatch.setitem(sys.modules, "vosk", dummy2)

    result = vosk_engine._load_model(dest)
    assert result is None
    assert called == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_auris_cfg(active_engine: str = "gemini"):
    """Return a side_effect for config_registry.get_value calls."""

    def _side_effect(key, default=None, **kwargs):
        mapping = {
            "ACTIVE_AURIS_ENGINE": active_engine,
            "AURIS_ENGINE_SETTINGS": "{}",
        }
        return mapping.get(key, default)

    return _side_effect
