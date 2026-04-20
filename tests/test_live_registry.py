# tests/test_live_registry.py
"""Tests for the Live registry and LiveEngineBase contract."""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


def test_live_registry_register_and_list() -> None:
    from core.live_registry import LiveRegistry

    reg = LiveRegistry()
    reg.register_engine("test_eng", "some.module", {"input": True}, "Test engine")
    assert "test_eng" in reg.get_available_engines()
    meta = reg.get_engine_meta("test_eng")
    assert meta["capabilities"]["input"] is True
    assert meta["label"] == "Test engine"


def test_live_registry_unknown_engine_raises() -> None:
    from core.live_registry import LiveRegistry

    reg = LiveRegistry()
    with pytest.raises(ValueError, match="Unknown engine"):
        reg.load_engine("nonexistent")


def test_live_registry_missing_engine_class() -> None:
    import types
    from core.live_registry import LiveRegistry

    fake_mod = types.ModuleType("fake_live_mod")
    # No ENGINE_CLASS attribute

    import sys

    sys.modules["fake_live_mod"] = fake_mod
    try:
        reg = LiveRegistry()
        reg.register_engine("bad", "fake_live_mod")
        with pytest.raises(ValueError, match="no ENGINE_CLASS"):
            reg.load_engine("bad")
    finally:
        del sys.modules["fake_live_mod"]


def test_live_registry_find_by_capabilities() -> None:
    from core.live_registry import LiveRegistry

    reg = LiveRegistry()
    reg.register_engine("input_only", "m1", {"input": True, "output": False})
    reg.register_engine("bidirectional", "m2", {"input": True, "output": True})

    found = reg.find_engine_by_capabilities({"input": True, "output": True})
    assert found == "bidirectional"

    found_any_input = reg.find_engine_by_capabilities({"input": True})
    assert found_any_input == "input_only"


# ---------------------------------------------------------------------------
# LiveEngineBase contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_engine_base_defaults() -> None:
    """LiveEngineBase default capabilities are both False."""
    from plugins.live_base import LiveEngineBase

    class DummyLiveEngine(LiveEngineBase):
        async def open_session(self, session_id, **kwargs):
            pass

        async def close_session(self, session_id):
            pass

        async def receive_events(self, session_id):
            return
            yield  # make it an async generator

    e = DummyLiveEngine()
    assert e.supports_input is False
    assert e.supports_output is False
    # send_audio and send_text are no-ops by default
    await e.send_audio("s", b"", 16000)
    await e.send_text("s", "hello")


@pytest.mark.asyncio
async def test_live_event_types() -> None:
    """LiveEvent carries correct fields for each event type."""
    from plugins.live_base import LiveEvent, LiveEventType

    transcript = LiveEvent(type=LiveEventType.TRANSCRIPT, text="hello", is_final=True)
    audio = LiveEvent(type=LiveEventType.AUDIO, audio=b"\x00\x01")
    vad = LiveEvent(type=LiveEventType.VAD, vad_signal="speech_start")
    error = LiveEvent(type=LiveEventType.ERROR, detail="boom")

    assert transcript.text == "hello"
    assert transcript.is_final is True
    assert audio.audio == b"\x00\x01"
    assert vad.vad_signal == "speech_start"
    assert error.detail == "boom"
