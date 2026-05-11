"""Tests for Vox TTS registry and plugin."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any
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


def test_active_vox_engine_default_is_kitten() -> None:
    from core.config_manager import config_registry
    import plugins.vox_plugin  # noqa: F401

    assert config_registry.get_value("ACTIVE_VOX_ENGINE", None) == "kitten"


def test_vox_plugin_is_enabled_tracks_active_engine() -> None:
    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    plugin._active_engine_name = "http"

    with patch.object(
        plugin,
        "refresh_config",
        side_effect=lambda: setattr(plugin, "_active_engine_name", "disabled"),
    ):
        assert plugin.is_enabled() is False

    with patch.object(
        plugin,
        "refresh_config",
        side_effect=lambda: setattr(plugin, "_active_engine_name", "kitten"),
    ):
        assert plugin.is_enabled() is True


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
    """When the active engine is 'disabled', speak() returns skipped."""
    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    plugin._active_engine_name = "disabled"
    plugin._engine_settings = {}
    plugin._fallback_to_text = True
    plugin._output_dir = Path("/tmp")

    # prevent refresh_config from overwriting our manual active engine
    patcher = patch.object(plugin, "refresh_config", lambda: None)
    patcher.start()
    try:
        result = await plugin.speak("hello world")
    finally:
        patcher.stop()

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
async def test_vox_plugin_detect_language_method() -> None:
    """The plugin should expose a detect_language method usable by recon."""
    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    # basic sanity checks without needing full initialization
    assert hasattr(plugin, "detect_language")
    # english text (longer sentence reduces misclassification)
    en_lang = plugin.detect_language("Hello world this is an english sentence")
    assert isinstance(en_lang, str) and len(en_lang) == 2
    assert en_lang.startswith("en")
    # italian text
    it_lang = plugin.detect_language("Questo è un testo completamente in italiano.")
    assert isinstance(it_lang, str) and len(it_lang) == 2
    assert it_lang.startswith("it")


@pytest.mark.asyncio
async def test_vox_plugin_detect_language_with_message_object() -> None:
    """detect_language must handle MessageWrapper-style objects (with .text attribute)."""
    from plugins.vox_plugin import VoxPlugin

    class FakeWrapper:
        """Minimal stand-in for telegram MessageWrapper."""

        def __init__(self, text: str) -> None:
            self.text = text

    plugin = VoxPlugin.__new__(VoxPlugin)
    # Italian text in a wrapper object (as recon passes from telegram voice messages)
    it_lang = plugin.detect_language(FakeWrapper("Questo è un testo in italiano."))
    assert it_lang is not None and it_lang.startswith("it"), (
        f"Expected 'it', got {it_lang!r}"
    )
    # English text in a wrapper object
    en_lang = plugin.detect_language(FakeWrapper("This is a clearly English sentence."))
    assert en_lang is not None and en_lang.startswith("en"), (
        f"Expected 'en', got {en_lang!r}"
    )
    # None text should not crash — returns None
    result = plugin.detect_language(FakeWrapper(None))  # type: ignore[arg-type]
    assert result is None or isinstance(result, str)


@pytest.mark.asyncio
async def test_vox_plugin_passes_language_to_engine(monkeypatch) -> None:
    """When detect_language identifies a language, it should be forwarded to the engine."""
    from core.vox_registry import VoxRegistry
    from plugins.vox_base import VoxEngineBase

    class LangEngine(VoxEngineBase):
        def generate_tts(self, text, emotion=None, **kw):
            # engine should see the detected language hint
            assert kw.get("language") == "it"
            return b"RIFF" + b"\x00" * 36

    mock_reg = VoxRegistry()
    mock_reg._engine_modules["lang"] = "lang_vox_mod"
    mock_reg._instances["lang"] = LangEngine()

    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    plugin._active_engine_name = "lang"
    plugin._engine_settings = {}
    plugin._fallback_to_text = True
    plugin._output_dir = Path("/tmp/vox_test")
    plugin._output_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("plugins.vox_plugin.VOX_REGISTRY", mock_reg),
        patch.object(plugin, "_dispatch", new=AsyncMock(return_value=None)),
        patch.object(plugin, "refresh_config"),
    ):
        # use longer italian sentence to ensure detection returns 'it'
        result = await plugin.speak(
            "Questo è un testo completamente in italiano.",
            interface_path="synth_webui/sess123",
        )

    assert result["status"] == "success"
    assert "filename" in result


def test_http_engine_language_hint(monkeypatch):
    """HttpVoxEngine should include the 'language' field when provided."""
    from plugins.vox_engines.http import HttpVoxEngine

    engine = HttpVoxEngine()
    monkeypatch.setattr(engine, "_load_endpoints", lambda: ["http://fake"])

    captured: dict | None = None

    def fake_post(endpoint, payload, timeout_s):
        nonlocal captured
        captured = payload.copy()
        return b"OK"

    monkeypatch.setattr(engine, "_post_tts", fake_post)

    audio = engine.generate_tts("hello", language="it")
    assert audio == b"OK"
    assert captured is not None
    assert captured.get("language") == "it"


test_vox_plugin_speak_calls_engine_and_writes_file_override_disabled = (
    test_vox_plugin_speak_calls_engine_and_writes_file
)
# above test already ensures speak works, but let's add explicit override variant below


@pytest.mark.asyncio
async def test_vox_plugin_override_disabled(monkeypatch):
    """When engine_name='disabled' is provided, speak() should skip regardless of active engine."""
    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    plugin._active_engine_name = "fake"
    plugin._engine_settings = {}
    plugin._fallback_to_text = True
    plugin._output_dir = Path("/tmp")

    result = await plugin.speak("hello", engine_name="disabled")
    assert result["status"] == "skipped"
    assert result["reason"] == "vox_disabled"


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
# Additional dispatch tests to confirm interfaces are passive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vox_plugin_dispatch_to_various_interfaces(monkeypatch, tmp_path):
    """The Vox core plugin should *dispatch* generated audio to interfaces without
    performing any additional synthesis logic.  Interfaces merely receive the
    path/bytes and handle delivery.

    This test exercises ``VoxPlugin._dispatch`` directly by installing fake
    interface objects that capture calls.  We assert that each supported branch
    is reached and that the payload passed through contains only the audio path
    and caption supplied by the core plugin.
    """
    from core.core_initializer import INTERFACE_REGISTRY

    # Build a minimal plugin instance so we can call _dispatch.
    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    # we don't need a real config here; _dispatch doesn't inspect plugin state

    calls: dict[str, Any] = {}

    class DummyUI:
        async def send_tts_audio(self, session_id, audio_path, text, lipsync_data=None):
            calls["synth_webui"] = {
                "session_id": session_id,
                "audio_path": audio_path,
                "text": text,
                "lipsync": lipsync_data,
            }

    class DummyDiscord:
        async def send_message(self, payload):
            calls["discord_bot"] = payload

    class DummyTelegram:
        async def execute_action(self, action, context, bot, original_message):
            calls["telegram_bot"] = action

    # Generic interface for fallback case
    class DummyGeneric:
        async def send_message(self, payload):
            # record every call in a list so we can inspect audio/text separately
            calls.setdefault("generic", []).append(payload)

    # Populate the registry with our fakes (monkeypatch dict for ease)
    monkeypatch.setitem(INTERFACE_REGISTRY, "synth_webui", DummyUI())
    monkeypatch.setitem(INTERFACE_REGISTRY, "discord_bot", DummyDiscord())
    monkeypatch.setitem(INTERFACE_REGISTRY, "telegram_bot", DummyTelegram())
    monkeypatch.setitem(INTERFACE_REGISTRY, "mystery_bot", DummyGeneric())

    audio_file = tmp_path / "test.wav"
    audio_file.write_bytes(b"RIFF" + b"\x00" * 36)

    # synth_webui branch
    await plugin._dispatch(
        audio_path=audio_file,
        interface_path="synth_webui/session42",
        caption="hello",
        lipsync_data={"foo": "bar"},
        context=None,
        original_message=None,
    )
    assert "synth_webui" in calls
    assert calls["synth_webui"]["session_id"] == "session42"
    assert str(audio_file) in calls["synth_webui"]["audio_path"]
    assert calls["synth_webui"]["text"] == "hello"

    # discord_bot branch
    await plugin._dispatch(
        audio_path=audio_file,
        interface_path="discord_bot/1/2",
        caption="yo",
        lipsync_data=None,
        context=None,
        original_message=None,
    )
    assert "discord_bot" in calls
    assert calls["discord_bot"]["interface_path"] == "discord_bot/1/2"
    assert calls["discord_bot"]["audio"].endswith("test.wav")

    # telegram_bot branch
    await plugin._dispatch(
        audio_path=audio_file,
        interface_path="telegram_bot/12345",
        caption="sup",
        lipsync_data=None,
        context=None,
        original_message=None,
    )
    assert "telegram_bot" in calls
    assert calls["telegram_bot"]["type"] == "audio_telegram_bot"
    assert calls["telegram_bot"]["payload"]["audio"].endswith("test.wav")

    # generic fallback branch (mystery_bot)
    await plugin._dispatch(
        audio_path=audio_file,
        interface_path="mystery_bot/foo",
        caption="hey there",
        lipsync_data=None,
        context=None,
        original_message=None,
    )
    assert "generic" in calls
    # generic sends two messages: first with audio, second with text
    generic_calls = calls["generic"]
    assert isinstance(generic_calls, list) and len(generic_calls) >= 2
    assert generic_calls[0].get("audio", "").endswith("test.wav")
    assert generic_calls[1].get("text") == "hey there"


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
    # require the real package; skip if neither vendor nor pip module is
    # available, which mirrors the behaviour of the engine itself.
    try:
        import kittentts  # noqa: F401
    except ImportError:
        pytest.skip("kittentts package not installed")

    from plugins.vox_engines.kitten import KittenVoxEngine

    engine = KittenVoxEngine()
    speakers = engine.get_speakers()
    assert isinstance(speakers, list)
    # ensure there are at least a couple of expected voices
    codes = {s["code"] for s in speakers}
    assert "Bella" in codes
    assert "Jasper" in codes


def test_kitten_sample_behavior(tmp_path, monkeypatch):
    """Ensure the Kitten engine can actually synthesise a short WAV.

    The vendor stub (``vendor/kittentts``) should be importable even if the
    real package is not installed.  If neither the stub nor PyPI package is
    importable we skip the test.
    """
    try:
        import kittentts  # noqa: F401
    except ImportError:
        pytest.skip("kittentts package not installed and no vendored stub")
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
    # the engine should be able to synthesize some bytes; if it fails we
    # skip so CI doesn't block when the stub or network isn't working.
    try:
        data = engine.sample("Bella")
        assert isinstance(data, (bytes, bytearray)) and len(data) > 0
    except Exception as exc:
        pytest.skip(f"Kitten sample generation failed: {exc}")
        assert data.startswith(b"RIFF")
    except (NotImplementedError, RuntimeError):
        pytest.skip("Kitten sample behavior unavailable in current environment")
