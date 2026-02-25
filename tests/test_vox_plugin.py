"""Tests for Vox TTS registry and plugin."""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# VoxRegistry unit tests
# ---------------------------------------------------------------------------


def test_vox_registry_register_and_list() -> None:
    from core.vox_registry import VoxRegistry

    reg = VoxRegistry()
    reg.register_engine(
        "http", "plugins.vox_engines.http", {"voice_cloning": True}, "HTTP"
    )
    assert "http" in reg.get_available_engines()


def test_vox_registry_unknown_engine_raises() -> None:
    from core.vox_registry import VoxRegistry

    reg = VoxRegistry()
    with pytest.raises(ValueError, match="Unknown engine"):
        reg.load_engine("does_not_exist")


def test_vox_registry_load_engine_missing_engine_class() -> None:
    from core.vox_registry import VoxRegistry

    dummy_mod = types.ModuleType("vox_no_class")
    reg = VoxRegistry()
    reg._engine_modules["bad"] = "vox_no_class"

    with patch("importlib.import_module", return_value=dummy_mod):
        with pytest.raises(ValueError, match="ENGINE_CLASS"):
            reg.load_engine("bad")


def test_vox_registry_find_by_capabilities() -> None:
    from core.vox_registry import VoxRegistry

    reg = VoxRegistry()
    reg.register_engine("clonable", "m1", {"voice_cloning": True, "local": False})
    reg.register_engine("offline", "m2", {"voice_cloning": False, "local": True})

    assert reg.find_engine_by_capabilities({"voice_cloning": True}) == "clonable"
    assert reg.find_engine_by_capabilities({"local": True}) == "offline"


# ---------------------------------------------------------------------------
# VoxPlugin tests (mocked engine + dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vox_plugin_speak_disabled() -> None:
    """When VOX_ENABLED=False speak() returns skipped."""
    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    plugin._enabled = False
    plugin._active_engine_name = "http"
    plugin._engine_settings = {}
    plugin._fallback_to_text = True
    plugin._output_dir = Path("/tmp")

    result = await plugin.speak("hello world")
    assert result["status"] == "skipped"
    assert result["reason"] == "vox_disabled"


@pytest.mark.asyncio
async def test_vox_plugin_speak_calls_engine_and_writes_file() -> None:
    """speak() should invoke the engine, write WAV, and dispatch."""
    from core.vox_registry import VoxRegistry
    from plugins.vox_base import VoxEngineBase

    class FakeEngine(VoxEngineBase):
        @property
        def output_format(self) -> str:
            return "wav"

        def generate_tts(self, text, emotion=None, **kw):
            # Return a minimal valid RIFF header so _write_audio uses direct write
            return b"RIFF" + b"\x00" * 36

    mock_reg = VoxRegistry()
    mock_reg._engine_modules["fake"] = "fake_vox_mod"
    mock_reg._instances["fake"] = FakeEngine()

    dispatched: list = []

    async def fake_dispatch(**kwargs):
        dispatched.append(kwargs)

    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    plugin._enabled = True
    plugin._active_engine_name = "fake"
    plugin._engine_settings = {}
    plugin._fallback_to_text = True
    plugin._output_dir = Path("/tmp/vox_test")
    plugin._output_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("plugins.vox_plugin.VOX_REGISTRY", mock_reg),
        patch.object(plugin, "_dispatch", new=AsyncMock(return_value=None)),
        patch.object(plugin, "refresh_config"),
    ):
        result = await plugin.speak("Hello Synth", interface_path="synth_webui/sess123")

    assert result["status"] == "success"
    assert "filename" in result


@pytest.mark.asyncio
async def test_vox_plugin_fallback_on_engine_failure() -> None:
    """When the engine returns None, fallback text should be attempted."""
    from core.vox_registry import VoxRegistry
    from plugins.vox_base import VoxEngineBase

    class BadEngine(VoxEngineBase):
        def generate_tts(self, text, emotion=None, **kw):
            return None  # simulate failure

    mock_reg = VoxRegistry()
    mock_reg._instances["bad"] = BadEngine()
    mock_reg._engine_modules["bad"] = "bad_mod"

    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    plugin._enabled = True
    plugin._active_engine_name = "bad"
    plugin._engine_settings = {}
    plugin._fallback_to_text = True
    plugin._output_dir = Path("/tmp")

    with (
        patch("plugins.vox_plugin.VOX_REGISTRY", mock_reg),
        patch.object(
            plugin,
            "_send_fallback",
            new=AsyncMock(
                return_value={"status": "error", "reason": "tts_failed_fallback_sent"}
            ),
        ),
        patch.object(plugin, "refresh_config"),
    ):
        result = await plugin.speak("test", interface_path="synth_webui/s1")

    assert result["reason"] == "tts_failed_fallback_sent"


# ---------------------------------------------------------------------------
# VoxEngineBase contract tests
# ---------------------------------------------------------------------------


def test_vox_engine_base_output_format_default() -> None:
    from plugins.vox_base import VoxEngineBase

    class MinimalEngine(VoxEngineBase):
        def generate_tts(self, text, emotion=None, **kw):
            return b""

    e = MinimalEngine()
    assert e.output_format == "wav"
    assert e.sample_rate == 22050
    assert e.channels == 1
    assert e.get_lipsync_data(b"") is None


def test_auris_engine_base_contract() -> None:
    """AurisEngineBase is file-based only — no streaming methods."""
    from plugins.auris_base import AurisEngineBase

    class MinimalSTT(AurisEngineBase):
        def transcribe(self, file_path, mime_type=None):
            return "ok"

    e = MinimalSTT()
    assert e.transcribe("/fake/path.wav") == "ok"
    # Streaming methods do NOT exist on AurisEngineBase anymore.
    # They live on LiveEngineBase (plugins/live_base.py).
    assert not hasattr(e, "supports_streaming")
    assert not hasattr(e, "process_chunk")
    assert not hasattr(e, "end_session")
