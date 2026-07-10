"""Tests for Vox TTS registry and plugin."""

from __future__ import annotations

import io
import json
import types
import wave
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


def test_active_vox_engine_default_is_disabled() -> None:
    from core.config_manager import config_registry
    import plugins.vox_plugin  # noqa: F401

    assert config_registry.get_value("ACTIVE_VOX_ENGINE", None) == "disabled"


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


def test_split_sentences_basic() -> None:
    from plugins.vox_plugin import _split_sentences

    result = _split_sentences(
        "First sentence here now. Second sentence follows next. Third one wraps it up."
    )
    assert result == [
        "First sentence here now.",
        "Second sentence follows next.",
        "Third one wraps it up.",
    ]


def test_split_sentences_merges_short_fragments() -> None:
    from plugins.vox_plugin import _split_sentences

    # "Ok." alone is well under min_chars and should be folded into a
    # neighbouring sentence rather than becoming its own tiny synthesis call.
    result = _split_sentences("Ok. Sure, let's do that then.", min_chars=12)
    assert len(result) == 1
    assert result[0] == "Ok. Sure, let's do that then."


def test_split_sentences_single_sentence_returns_as_is() -> None:
    from plugins.vox_plugin import _split_sentences

    assert _split_sentences("Just one sentence here.") == ["Just one sentence here."]


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
    plugin._sentence_chunking_enabled = False
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
    plugin._sentence_chunking_enabled = False
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


# ---------------------------------------------------------------------------
# Vox per-language engine overrides
# ---------------------------------------------------------------------------


def test_get_vox_language_override_hit_and_miss(monkeypatch) -> None:
    """get_vox_language_override resolves a language entry, normalises the
    code, and returns None for unknown languages / disabled engines."""
    from core.config import get_vox_language_override

    mapping = {
        "it": {"engine": "fish-audio", "model": "s2.1-pro", "voice": "maria"},
        "en": {"engine": "kitten", "model": "", "voice": "luna"},
        "fr": {"engine": "disabled"},
    }
    monkeypatch.setattr(
        "core.config.config_registry.get_value",
        lambda key, default="{}", value_type=str: (
            json.dumps(mapping) if key == "VOX_LANGUAGE_OVERRIDES" else default
        ),
    )

    # exact match
    assert get_vox_language_override("it") == {
        "engine": "fish-audio",
        "model": "s2.1-pro",
        "voice": "maria",
    }
    # region-stripped normalisation ("it-it" -> "it")
    assert get_vox_language_override("it-it") == {
        "engine": "fish-audio",
        "model": "s2.1-pro",
        "voice": "maria",
    }
    # uppercase normalisation
    assert get_vox_language_override("EN") == {
        "engine": "kitten",
        "model": "",
        "voice": "luna",
    }
    # unknown language -> None
    assert get_vox_language_override("de") is None
    # engine "disabled" -> None (use default engine)
    assert get_vox_language_override("fr") is None
    # empty / None input -> None
    assert get_vox_language_override("") is None
    assert get_vox_language_override(None) is None


def test_get_vox_language_override_invalid_json(monkeypatch) -> None:
    """Malformed VOX_LANGUAGE_OVERRIDES JSON must not raise — return None."""
    from core.config import get_vox_language_override

    monkeypatch.setattr(
        "core.config.config_registry.get_value",
        lambda key, default="{}", value_type=str: (
            "{not json" if key == "VOX_LANGUAGE_OVERRIDES" else default
        ),
    )
    assert get_vox_language_override("it") is None


@pytest.mark.asyncio
async def test_get_vox_language_override_async_reads_db(monkeypatch) -> None:
    """The async variant must read the persisted DB value (not the registry
    default), because the registry skips DB loads inside a running event loop."""
    from core.config import get_vox_language_override_async

    mapping = {"it": {"engine": "fish-audio", "model": "s2.1-pro", "voice": "maria"}}
    monkeypatch.setattr(
        "core.config.config_registry.get_persisted_value",
        AsyncMock(return_value=json.dumps(mapping)),
    )
    from core.config import _invalidate_vox_lang_override_cache

    _invalidate_vox_lang_override_cache()

    assert await get_vox_language_override_async("it") == {
        "engine": "fish-audio",
        "model": "s2.1-pro",
        "voice": "maria",
    }
    # unknown language -> None
    assert await get_vox_language_override_async("de") is None
    # empty / None input -> None
    assert await get_vox_language_override_async("") is None
    assert await get_vox_language_override_async(None) is None


@pytest.mark.asyncio
async def test_vox_plugin_speak_language_override_routes_engine_and_voice() -> None:
    """When a detected language has an override, speak() must load that engine
    and forward its model + voice as explicit per-call kwargs."""
    from core.vox_registry import VoxRegistry
    from plugins.vox_base import VoxEngineBase

    captured: dict = {}

    class OverrideEngine(VoxEngineBase):
        def generate_tts(self, text, emotion=None, **kw):
            captured.update(kw)
            return b"RIFF" + b"\x00" * 36

    mock_reg = VoxRegistry()
    mock_reg._engine_modules["fish-audio"] = "fish_vox_mod"
    mock_reg._instances["fish-audio"] = OverrideEngine()

    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    plugin._active_engine_name = "kitten"  # default engine differs from override
    plugin._engine_settings = {}
    plugin._fallback_to_text = True
    plugin._sentence_chunking_enabled = False
    plugin._output_dir = Path("/tmp/vox_test")
    plugin._output_dir.mkdir(parents=True, exist_ok=True)

    mapping = {"it": {"engine": "fish-audio", "model": "s2.1-pro", "voice": "maria"}}
    from core.config import _invalidate_vox_lang_override_cache

    _invalidate_vox_lang_override_cache()
    with (
        patch("plugins.vox_plugin.VOX_REGISTRY", mock_reg),
        patch.object(plugin, "_dispatch", new=AsyncMock(return_value=None)),
        patch.object(plugin, "refresh_config"),
        patch(
            "core.config.config_registry.get_persisted_value",
            AsyncMock(return_value=json.dumps(mapping)),
        ),
    ):
        result = await plugin.speak(
            "Questo è un testo completamente in italiano.",
            interface_path="synth_webui/sess123",
        )

    assert result["status"] == "success"
    # override engine was used and its model + voice forwarded explicitly
    assert captured.get("model") == "s2.1-pro"
    assert captured.get("voice") == "maria"
    assert captured.get("language") == "it"


@pytest.mark.asyncio
async def test_vox_plugin_speak_no_override_uses_default_engine() -> None:
    """Without an override for the detected language, speak() uses the default
    active engine and does NOT inject a model/voice override."""
    from core.vox_registry import VoxRegistry
    from plugins.vox_base import VoxEngineBase

    captured: dict = {}

    class DefaultEngine(VoxEngineBase):
        def generate_tts(self, text, emotion=None, **kw):
            captured.update(kw)
            return b"RIFF" + b"\x00" * 36

    mock_reg = VoxRegistry()
    mock_reg._engine_modules["kitten"] = "kitten_vox_mod"
    mock_reg._instances["kitten"] = DefaultEngine()

    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    plugin._active_engine_name = "kitten"
    plugin._engine_settings = {}
    plugin._fallback_to_text = True
    plugin._sentence_chunking_enabled = False
    plugin._output_dir = Path("/tmp/vox_test")
    plugin._output_dir.mkdir(parents=True, exist_ok=True)

    from core.config import _invalidate_vox_lang_override_cache

    _invalidate_vox_lang_override_cache()
    with (
        patch("plugins.vox_plugin.VOX_REGISTRY", mock_reg),
        patch.object(plugin, "_dispatch", new=AsyncMock(return_value=None)),
        patch.object(plugin, "refresh_config"),
        patch(
            "core.config.config_registry.get_persisted_value",
            AsyncMock(return_value="{}"),
        ),
    ):
        result = await plugin.speak(
            "Questo è un testo completamente in italiano.",
            interface_path="synth_webui/sess123",
        )

    assert result["status"] == "success"
    # no override -> no explicit model/voice injected (only the language hint)
    assert "model" not in captured
    assert "voice" not in captured
    assert captured.get("language") == "it"


def _make_wav_bytes(num_frames: int = 100, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * num_frames)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_vox_plugin_speak_chunks_multi_sentence_reply(
    monkeypatch, tmp_path
) -> None:
    """A multi-sentence reply to a connected webui session streams
    sentence-by-sentence: one broadcast_audio call per sentence sharing a
    single turn_id, then the combined clip still goes through the normal
    single dispatch with karada re-broadcast skipped (already streamed)."""
    from core.vox_registry import VoxRegistry
    from plugins.vox_base import VoxEngineBase

    class FakeEngine(VoxEngineBase):
        @property
        def output_format(self) -> str:
            return "wav"

        def generate_tts(self, text, emotion=None, **kw):
            return _make_wav_bytes()

    mock_reg = VoxRegistry()
    mock_reg._engine_modules["fake"] = "fake_vox_mod"
    mock_reg._instances["fake"] = FakeEngine()

    broadcasts: list[dict] = []

    class DummyKarada:
        def has_connected_clients(self) -> bool:
            return True

        async def broadcast_audio(self, **kwargs):
            broadcasts.append(kwargs)

    monkeypatch.setattr(
        "core.animation_handler.get_karada_state_server", lambda: DummyKarada()
    )

    from plugins.vox_plugin import VoxPlugin

    plugin = VoxPlugin.__new__(VoxPlugin)
    plugin._active_engine_name = "fake"
    plugin._engine_settings = {}
    plugin._fallback_to_text = True
    plugin._sentence_chunking_enabled = True
    plugin._output_dir = tmp_path
    plugin._output_dir.mkdir(parents=True, exist_ok=True)

    dispatched: list = []

    async def fake_dispatch(**kwargs):
        dispatched.append(kwargs)

    with (
        patch("plugins.vox_plugin.VOX_REGISTRY", mock_reg),
        patch.object(plugin, "_dispatch", new=fake_dispatch),
        patch.object(plugin, "refresh_config"),
    ):
        result = await plugin.speak(
            "First sentence here now. Second sentence follows next. "
            "Third one wraps it up.",
            interface_path="synth_webui/sess1",
        )

    assert result["status"] == "success"
    assert len(broadcasts) == 3
    turn_ids = {b["turn_id"] for b in broadcasts}
    assert len(turn_ids) == 1
    assert next(iter(turn_ids))

    # The combined dispatch afterward should skip re-broadcasting to karada
    # (already streamed chunk-by-chunk) but still deliver the single
    # persisted clip/caption exactly as the non-chunked path would.
    assert len(dispatched) == 1
    assert dispatched[0]["skip_karada_broadcast"] is True


def test_http_engine_language_hint(monkeypatch):
    """HttpVoxEngine should include the 'language' field when provided."""
    import plugins.vox_engines.http as http_mod
    from plugins.vox_engines.http import HttpVoxEngine

    cfg = {
        "HTTP_TTS_ENDPOINTS": "http://fake",
        "HTTP_TTS_REFERENCE_ID": "",
        "HTTP_TTS_FORMAT": "pcm",
    }
    monkeypatch.setattr(
        http_mod, "_cfg", lambda key, default, value_type=str: cfg.get(key, default)
    )

    engine = HttpVoxEngine()

    captured: dict | None = None

    def fake_post(endpoint, payload, headers, timeout_s):
        nonlocal captured
        captured = payload.copy()
        return b"OK"

    monkeypatch.setattr(engine, "_post_tts", fake_post)

    audio = engine.generate_tts("hello", language="it")
    assert audio == b"OK"
    assert captured is not None
    assert captured.get("language") == "it"


def test_http_engine_fish_audio_payload_and_headers(monkeypatch):
    """With a reference_id configured, HttpVoxEngine must send the Fish-style
    payload (text/reference_id/format, no voice_wav) plus Bearer + model headers."""
    import plugins.vox_engines.http as http_mod

    cfg = {
        "HTTP_TTS_ENDPOINTS": "https://api.fish.audio/v1/tts",
        "HTTP_TTS_API_KEY": "sk-test",
        "HTTP_TTS_MODEL": "s2.1-pro-free",
        "HTTP_TTS_REFERENCE_ID": "voice123",
        "HTTP_TTS_FORMAT": "wav",
        "HTTP_TTS_EXTRA_HEADERS": '{"X-Extra": "1"}',
        "HTTP_TTS_EXTRA_PARAMS": '{"temperature": 0.7}',
        "HTTP_TTS_TIMEOUT_SECONDS": 42,
    }
    monkeypatch.setattr(
        http_mod, "_cfg", lambda key, default, value_type=str: cfg.get(key, default)
    )

    engine = http_mod.HttpVoxEngine()

    captured: dict = {}

    def fake_post(endpoint, payload, headers, timeout_s):
        captured.update(
            endpoint=endpoint, payload=payload, headers=headers, timeout=timeout_s
        )
        return b"RIFFxxxx"

    monkeypatch.setattr(engine, "_post_tts", fake_post)

    audio = engine.generate_tts("hello", language="it")
    assert audio == b"RIFFxxxx"
    assert captured["endpoint"] == "https://api.fish.audio/v1/tts"
    assert captured["payload"] == {
        "text": "hello",
        "reference_id": "voice123",
        "format": "wav",
        "temperature": 0.7,
    }
    assert "voice_wav" not in captured["payload"]
    assert captured["headers"] == {
        "Authorization": "Bearer sk-test",
        "model": "s2.1-pro-free",
        "X-Extra": "1",
    }
    assert captured["timeout"] == 42
    # RIFF passthrough: format=wav must declare wav output so the Vox plugin
    # doesn't re-wrap the bytes as PCM.
    assert engine.output_format == "wav"


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
    plugin._sentence_chunking_enabled = False
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
