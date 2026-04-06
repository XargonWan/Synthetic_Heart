import os

import pytest
from plugins.vox_plugin import VoxPlugin
from core.vox_registry import VOX_REGISTRY


def test_builtin_vox_engines_registered() -> None:
    """Ensure the expected Vox engines are available after plugin initialization."""
    saved_modules = VOX_REGISTRY._engine_modules.copy()
    saved_meta = VOX_REGISTRY._engine_meta.copy()
    saved_instances = VOX_REGISTRY._instances.copy()
    try:
        for name in list(VOX_REGISTRY.get_available_engines()):
            VOX_REGISTRY.unregister_engine(name)
        VoxPlugin()
        engines = set(VOX_REGISTRY.get_available_engines())
        # chatterbox is now a development-only engine and should not be imported
        assert engines == {"kitten"}
    finally:
        VOX_REGISTRY._engine_modules.clear()
        VOX_REGISTRY._engine_modules.update(saved_modules)
        VOX_REGISTRY._engine_meta.clear()
        VOX_REGISTRY._engine_meta.update(saved_meta)
        VOX_REGISTRY._instances.clear()
        VOX_REGISTRY._instances.update(saved_instances)


def test_http_vox_engine_registered_when_legacy_endpoints_configured() -> None:
    """HTTP Vox should register only when legacy TTS_ENDPOINTS are configured."""
    saved_modules = VOX_REGISTRY._engine_modules.copy()
    saved_meta = VOX_REGISTRY._engine_meta.copy()
    saved_instances = VOX_REGISTRY._instances.copy()
    original_tts_endpoints = os.environ.get("TTS_ENDPOINTS")
    os.environ["TTS_ENDPOINTS"] = "http://example.com/tts"
    try:
        for name in list(VOX_REGISTRY.get_available_engines()):
            VOX_REGISTRY.unregister_engine(name)
        VoxPlugin()
        engines = set(VOX_REGISTRY.get_available_engines())
        assert "http" in engines
    finally:
        if original_tts_endpoints is None:
            os.environ.pop("TTS_ENDPOINTS", None)
        else:
            os.environ["TTS_ENDPOINTS"] = original_tts_endpoints
        VOX_REGISTRY._engine_modules.clear()
        VOX_REGISTRY._engine_modules.update(saved_modules)
        VOX_REGISTRY._engine_meta.clear()
        VOX_REGISTRY._engine_meta.update(saved_meta)
        VOX_REGISTRY._instances.clear()
        VOX_REGISTRY._instances.update(saved_instances)


def test_kittentts_stub_importable() -> None:
    """The vendored ``kittentts`` package should import without blowing up.

    If the optional audio dependencies (``gtts``/``pydub``) are missing the
    module still has to load; generation will raise later.  Skip the test
    entirely on machines that don't have those dependencies installed.
    """
    try:
        import kittentts  # type: ignore
    except ImportError:
        pytest.skip("kittentts stub dependencies not available")

    assert hasattr(kittentts, "KittenTTS")
    # try at least constructing the class; actual generation is covered by
    # ``test_vox_plugin`` which already exercises the engine sample.
    tts = kittentts.KittenTTS()
    assert tts is not None
