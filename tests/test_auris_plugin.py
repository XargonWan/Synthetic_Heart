"""Tests for Auris STT registry and plugin."""

from __future__ import annotations

from unittest.mock import patch

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
    from plugins.auris_base import AurisEngineBase

    class FakeEngine(AurisEngineBase):
        def transcribe(self, file_path, mime_type=None):
            return "hello"

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
    """When AURIS_ENABLED=False the plugin returns None without calling engine."""
    with (
        patch("core.core_initializer.register_plugin"),
        patch.object(
            __import__(
                "core.config_manager", fromlist=["config_registry"]
            ).config_registry,
            "get_value",
            side_effect=_mock_auris_cfg(enabled=False),
        ),
    ):
        from plugins.auris_plugin import AurisPlugin

        plugin = AurisPlugin.__new__(AurisPlugin)
        plugin._enabled = False
        plugin._active_engine_name = "gemini"
        plugin._engine_settings = {}

        result = await plugin.transcribe_audio("/tmp/fake.wav")
        assert result is None


@pytest.mark.asyncio
async def test_auris_plugin_transcribe_calls_engine() -> None:
    """transcribe_audio should call the engine's transcribe method."""
    from core.auris_registry import AurisRegistry
    from plugins.auris_base import AurisEngineBase

    class MockEngine(AurisEngineBase):
        def transcribe(self, file_path, mime_type=None):
            return "transcribed text"

    mock_registry = AurisRegistry()
    mock_registry._engine_modules["mock"] = "mock_module"
    mock_registry._instances["mock"] = MockEngine()

    from plugins.auris_plugin import AurisPlugin

    plugin = AurisPlugin.__new__(AurisPlugin)
    plugin._enabled = True
    plugin._active_engine_name = "mock"
    plugin._engine_settings = {}

    with (
        patch("plugins.auris_plugin.AURIS_REGISTRY", mock_registry),
        patch("os.path.exists", return_value=True),
        patch.object(plugin, "refresh_config"),
    ):
        result = await plugin.transcribe_audio("/tmp/fake.wav")
    assert result == "transcribed text"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_auris_cfg(enabled: bool):
    """Return a side_effect for config_registry.get_value calls."""

    def _side_effect(key, default=None, **kwargs):
        mapping = {
            "AURIS_ENABLED": enabled,
            "ACTIVE_AURIS_ENGINE": "gemini",
            "AURIS_ENGINE_SETTINGS": "{}",
        }
        return mapping.get(key, default)

    return _side_effect
