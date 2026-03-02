"""Tests for Vox TTS registry and plugin."""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface
from core.vox_registry import VoxRegistry

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


# additional tests for new vox HTTP helpers and Kitten engine


def _make_test_client_with_engine(monkeypatch, engine_name: str, engine_instance):
    """Helper: create webui TestClient with a registry containing a single engine."""
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)
    reg = VoxRegistry()
    reg._engine_modules[engine_name] = "dummy_module"
    reg._instances[engine_name] = engine_instance
    # override the global singleton used by the webui endpoints
    monkeypatch.setattr("core.vox_registry.VOX_REGISTRY", reg)
    return client


class DummyEngine:
    def get_speakers(self):
        return [{"code": "a", "name": "Alpha"}]

    def sample(self, speaker):
        if speaker == "a":
            return b"RIFF" + b"\x00" * 36
        raise NotImplementedError()


def test_vox_speakers_endpoint_success(monkeypatch):
    client = _make_test_client_with_engine(monkeypatch, "dummy", DummyEngine())
    resp = client.get("/api/vox/speakers?engine=dummy")
    assert resp.status_code == 200
    assert resp.json() == [{"code": "a", "name": "Alpha"}]


def test_vox_speakers_endpoint_not_found(monkeypatch):
    client = _make_test_client_with_engine(monkeypatch, "dummy", DummyEngine())
    resp = client.get("/api/vox/speakers?engine=missing")
    assert resp.status_code == 404


def test_vox_sample_endpoint_success(monkeypatch):
    client = _make_test_client_with_engine(monkeypatch, "dummy", DummyEngine())
    resp = client.get("/api/vox/sample?engine=dummy&speaker=a")
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("audio/")


def test_vox_sample_endpoint_missing_speaker(monkeypatch):
    client = _make_test_client_with_engine(monkeypatch, "dummy", DummyEngine())
    resp = client.get("/api/vox/sample?engine=dummy")
    assert resp.status_code == 400


def test_vox_sample_endpoint_engine_not_found(monkeypatch):
    client = _make_test_client_with_engine(monkeypatch, "dummy", DummyEngine())
    resp = client.get("/api/vox/sample?engine=none&speaker=a")
    assert resp.status_code == 404


def test_vox_sample_endpoint_not_implemented(monkeypatch):
    client = _make_test_client_with_engine(monkeypatch, "dummy", DummyEngine())
    resp = client.get("/api/vox/sample?engine=dummy&speaker=bad")
    assert resp.status_code == 404


# tests for the Kitten engine helpers


def test_kitten_speaker_metadata():
    from plugins.vox_engines.kitten import KittenVoxEngine, _SPEAKER_METADATA

    engine = KittenVoxEngine()
    speakers = engine.get_speakers()
    assert isinstance(speakers, list) and len(speakers) == 118

    # verify a few known entries
    for code, meta in _SPEAKER_METADATA.items():
        found = next((s for s in speakers if s["code"] == code), None)
        assert found is not None
        assert found["name"] == meta["name"]
        assert found["language"] == meta["language"]


def test_kitten_sample_behavior(tmp_path, monkeypatch):
    from plugins.vox_engines.kitten import KittenVoxEngine
    from core.config_manager import config_registry

    # make sure engine will look under our temporary directory
    original_get = config_registry.get_value

    def fake_get(key, default=None, **kw):
        if key == "VOX_OUTPUT_DIR":
            return str(tmp_path)
        return original_get(key, default, **kw)

    monkeypatch.setattr(config_registry, "get_value", fake_get)

    engine = KittenVoxEngine()
    with pytest.raises(NotImplementedError):
        engine.sample("nonexistent")

    # create a fake sample file
    sample_dir = tmp_path / "samples" / "kitten"
    sample_dir.mkdir(parents=True)
    wav_path = sample_dir / "kitten_test.wav"
    wav_path.write_bytes(b"RIFF" + b"\x00" * 36)

    data = engine.sample("test")
    assert data.startswith(b"RIFF")
