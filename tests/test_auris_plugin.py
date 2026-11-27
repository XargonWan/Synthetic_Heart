"""Tests for Auris STT registry and plugin."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
async def test_auris_plugin_transcribe_inline_returns_none() -> None:
    """'inline' has no transcription engine, so transcribe_audio returns None."""
    with (
        patch("core.core_initializer.register_plugin"),
        patch.object(
            __import__(
                "core.config_manager", fromlist=["config_registry"]
            ).config_registry,
            "get_value",
            side_effect=lambda key, default=None, **kwargs: (
                "inline" if key == "ACTIVE_AURIS_ENGINE" else default
            ),
        ),
    ):
        from plugins.auris_plugin import AurisPlugin

        plugin = AurisPlugin.__new__(AurisPlugin)
        plugin._active_engine_name = "inline"
        plugin._engine_settings = {}

        result = await plugin.transcribe_audio("/tmp/fake.wav")
        assert result is None


def test_auris_plugin_inline_is_not_enabled() -> None:
    """'inline' must not expose the stt_transcribe action (is_enabled False)."""
    from plugins.auris_plugin import AurisPlugin

    plugin = AurisPlugin.__new__(AurisPlugin)
    plugin._active_engine_name = "inline"

    with patch.object(
        plugin,
        "refresh_config",
        side_effect=lambda: setattr(plugin, "_active_engine_name", "inline"),
    ):
        assert plugin.is_enabled() is False


@pytest.mark.asyncio
async def test_dispatch_media_audio_inline_skips_transcription() -> None:
    """In Auris inline mode dispatch_media returns None without transcribing."""
    import core.media_dispatcher as md

    with (
        patch.object(md, "_is_subsystem_inline", return_value=True),
        patch.object(md, "_try_auris") as mock_auris,
        patch.object(md, "_try_live_fallback") as mock_live,
    ):
        result = await md.dispatch_media("/tmp/fake.ogg", "audio/ogg")

    assert result is None
    mock_auris.assert_not_called()
    mock_live.assert_not_called()


def test_get_active_auris_engine_reflects_pseudo_engine() -> None:
    """_get_active_auris_engine reports the configured engine, incl. 'inline'."""
    import core.plugin_instance as pi

    class _FakeAuris:
        def __init__(self, name: str) -> None:
            self._active_engine_name = name

        def refresh_config(self) -> None:  # no-op
            pass

    for engine_name in ("inline", "disabled", "vosk"):
        registry = {"auris_plugin": _FakeAuris(engine_name)}
        with patch("core.core_initializer.PLUGIN_REGISTRY", registry):
            assert pi._get_active_auris_engine() == engine_name


def test_auris_plugin_refresh_config_uses_vosk_default() -> None:
    from core.config_manager import config_registry as cfg
    from plugins.auris_plugin import AurisPlugin

    plugin = AurisPlugin.__new__(AurisPlugin)
    with patch.object(
        cfg, "get_value", side_effect=lambda key, default=None, **kwargs: default
    ):
        plugin.refresh_config()

    assert plugin._active_engine_name == "vosk"


def test_auris_plugin_is_enabled_tracks_active_engine() -> None:
    from plugins.auris_plugin import AurisPlugin

    plugin = AurisPlugin.__new__(AurisPlugin)
    plugin._active_engine_name = "vosk"

    with patch.object(
        plugin,
        "refresh_config",
        side_effect=lambda: setattr(plugin, "_active_engine_name", "disabled"),
    ):
        assert plugin.is_enabled() is False

    with patch.object(
        plugin,
        "refresh_config",
        side_effect=lambda: setattr(plugin, "_active_engine_name", "vosk"),
    ):
        assert plugin.is_enabled() is True


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


def _mock_auris_cfg(active_engine: str = "gemini"):
    """Return a side_effect for config_registry.get_value calls."""

    def _side_effect(key, default=None, **kwargs):
        mapping = {
            "ACTIVE_AURIS_ENGINE": active_engine,
            "AURIS_ENGINE_SETTINGS": "{}",
        }
        return mapping.get(key, default)

    return _side_effect


@pytest.mark.asyncio
async def test_auris_plugin_transcribe_tries_fallback_engines_in_order() -> None:
    """A failing active engine hands over to the first fallback that transcribes."""
    from core.auris_registry import AurisRegistry
    from plugins.auris_base import AurisEngineBase, AurisTranscriptResult

    calls: list[str] = []

    class Engine(AurisEngineBase):
        def __init__(self, label: str, text: str | None) -> None:
            self.label = label
            self.text = text

        def transcribe(
            self, file_path: str, mime_type: str | None = None
        ) -> AurisTranscriptResult | None:
            calls.append(self.label)
            if self.label == "boom":
                raise RuntimeError("engine down")
            if self.text is None:
                return None
            return AurisTranscriptResult(text=self.text, language="en")

    registry = AurisRegistry()
    for label, text in (("primary", None), ("boom", None), ("good", "heard you")):
        registry._engine_modules[label] = f"{label}_mod"
        registry._instances[label] = Engine(label, text)

    from plugins.auris_plugin import AurisPlugin

    plugin = AurisPlugin.__new__(AurisPlugin)
    plugin._active_engine_name = "primary"
    plugin._engine_settings = {}

    with (
        patch("plugins.auris_plugin.AURIS_REGISTRY", registry),
        patch(
            "plugins.auris_plugin.get_auris_fallback_engines_async",
            new=AsyncMock(return_value=["primary", "boom", "missing", "good"]),
        ),
        patch("os.path.exists", return_value=True),
        patch.object(plugin, "refresh_config"),
    ):
        result = await plugin.transcribe_audio("/tmp/fake.wav")

    assert result is not None and result.text == "heard you"
    assert calls == ["primary", "boom", "good"]
