import asyncio
import struct
import tempfile
import wave
from pathlib import Path

import pytest

from plugins.facial_expression_plugin import (
    FacialExpressionPlugin,
    FacialExpressionEvent,
)
from core.animation_handler import KaradaStateServer


class DummyKarada(KaradaStateServer):
    def __init__(self):
        super().__init__(webui=None)
        self.sent = []

    def has_connected_clients(self) -> bool:  # always pretend a client is connected
        return True

    async def push_face_expression(self, name, intensity, targets=None):
        self.sent.append((name, intensity))


@pytest.mark.asyncio
async def test_timeline_basic(monkeypatch):
    plugin = FacialExpressionPlugin()
    # patch get_karada_state_server
    dummy = DummyKarada()
    monkeypatch.setattr(
        "plugins.facial_expression_plugin.get_karada_state_server", lambda: dummy
    )
    events = [
        FacialExpressionEvent(position=0, name="smile", intensity=0.5),
        FacialExpressionEvent(position=5, name="sad", intensity=0.2),
    ]
    # total_chars such that first event at 0 sec, second at >0
    await plugin._play_expression_timeline(
        events, total_chars=10, session_id="x", chars_per_sec=10
    )
    # after timeline and cooldown, we should see three pushes (two events + clear)
    assert dummy.sent[0] == ("smile", 0.5)
    assert dummy.sent[1] == ("sad", 0.2)
    assert dummy.sent[2] == (None, 0)


@pytest.mark.asyncio
async def test_process_message_text(monkeypatch):
    plugin = FacialExpressionPlugin()
    dummy = DummyKarada()
    monkeypatch.setattr(
        "plugins.facial_expression_plugin.get_karada_state_server", lambda: dummy
    )

    # monkeypatch persona manager to give default settings
    class PM:
        _current_persona = type("X", (), {"name": "Rei"})

        def _load_persona_json(self, name):
            return {
                "facial_expression_chars_per_sec": 1000,
            }

    monkeypatch.setattr(
        "plugins.facial_expression_plugin.get_persona_manager", lambda: PM()
    )

    # Capture background tasks so we can await them deterministically
    created_tasks: list = []
    _real_create_task = asyncio.create_task

    def _capturing_create_task(coro, **kw):
        t = _real_create_task(coro, **kw)
        created_tasks.append(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", _capturing_create_task)

    text = "Hello [em_grin:0.9] world"
    clean = await plugin.process_message_text(text, session_id="s")
    assert clean == "Hello  world"
    # await all background tasks to completion (avoids timing-dependent sleep)
    if created_tasks:
        await asyncio.gather(*created_tasks)
    assert ("grin", 0.9) in dummy.sent


@pytest.mark.asyncio
async def test_timeline_with_audio_duration(monkeypatch):
    """When audio_duration_s is provided, delays are proportional to audio length."""
    plugin = FacialExpressionPlugin()
    dummy = DummyKarada()
    monkeypatch.setattr(
        "plugins.facial_expression_plugin.get_karada_state_server", lambda: dummy
    )
    events = [
        FacialExpressionEvent(position=0, name="smile", intensity=0.5),
        FacialExpressionEvent(position=5, name="sad", intensity=0.2),
    ]
    # audio_duration_s=0.2 means second event at ~0.1s instead of chars-based timing
    await plugin._play_expression_timeline(
        events,
        total_chars=10,
        session_id="x",
        chars_per_sec=10,
        audio_duration_s=0.2,
    )
    assert dummy.sent[0] == ("smile", 0.5)
    assert dummy.sent[1] == ("sad", 0.2)
    assert dummy.sent[2] == (None, 0)


def test_get_wav_duration():
    """_get_wav_duration extracts correct duration from a WAV file."""
    from plugins.vox_plugin import _get_wav_duration

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = Path(f.name)
    # Write a 1-second WAV: 22050 samples at 22050 Hz, 16-bit mono
    sample_rate = 22050
    n_frames = sample_rate  # 1 second
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{n_frames}h", *([0] * n_frames)))

    dur = _get_wav_duration(path)
    assert dur is not None
    assert abs(dur - 1.0) < 0.01
    path.unlink()


def test_get_wav_duration_bad_file():
    """_get_wav_duration returns None for non-WAV files."""
    from plugins.vox_plugin import _get_wav_duration

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"not a wav file")
        path = Path(f.name)
    assert _get_wav_duration(path) is None
    path.unlink()


@pytest.mark.asyncio
async def test_expression_persists_until_end_of_audio(monkeypatch):
    """Last expression is held until total_duration elapses, then cleared."""
    plugin = FacialExpressionPlugin()
    dummy = DummyKarada()
    monkeypatch.setattr(
        "plugins.facial_expression_plugin.get_karada_state_server", lambda: dummy
    )
    events = [
        # Single expression at the very start
        FacialExpressionEvent(position=0, name="grin", intensity=0.9),
    ]
    t0 = asyncio.get_event_loop().time()
    await plugin._play_expression_timeline(
        events,
        total_chars=100,
        session_id="x",
        chars_per_sec=100,  # total_duration = 1.0s
    )
    elapsed = asyncio.get_event_loop().time() - t0
    # Expression should have been held for ~1.0s (total_duration), not 0s
    assert elapsed >= 0.8, f"Expected ~1s hold, got {elapsed:.2f}s"
    # Two pushes: the expression + the final clear
    assert dummy.sent[0] == ("grin", 0.9)
    assert dummy.sent[-1] == (None, 0)


@pytest.mark.asyncio
async def test_bare_em_reset_produces_clear(monkeypatch):
    """A bare [em] tag (name=None) should produce a clear to return to base."""
    plugin = FacialExpressionPlugin()
    dummy = DummyKarada()
    monkeypatch.setattr(
        "plugins.facial_expression_plugin.get_karada_state_server", lambda: dummy
    )
    events = [
        FacialExpressionEvent(position=0, name="smile", intensity=0.8),
        # bare [em] reset mid-text
        FacialExpressionEvent(position=5, name=None, intensity=1.0),
    ]
    await plugin._play_expression_timeline(
        events,
        total_chars=10,
        session_id="x",
        chars_per_sec=100,  # fast — total_duration = 0.1s
    )
    # First: smile expression
    assert dummy.sent[0] == ("smile", 0.8)
    # Second: bare [em] → name=None, dispatched as-is (clear at frontend)
    assert dummy.sent[1] == (None, 1.0)
    # Third: final clear at end of audio
    assert dummy.sent[-1] == (None, 0)


@pytest.mark.asyncio
async def test_neutral_expression_clears(monkeypatch):
    """An [em_neutral] with empty targets produces a clear (return to base)."""
    plugin = FacialExpressionPlugin()
    dummy = DummyKarada()
    monkeypatch.setattr(
        "plugins.facial_expression_plugin.get_karada_state_server", lambda: dummy
    )
    expr_section = {
        "grin": {"targets": {"mouth_smile": 0.9}},
        "neutral": {"description": "return to base emotional state", "targets": {}},
    }
    events = [
        FacialExpressionEvent(position=0, name="grin", intensity=0.9),
        FacialExpressionEvent(position=5, name="neutral", intensity=1.0),
    ]
    await plugin._play_expression_timeline(
        events,
        total_chars=10,
        session_id="x",
        chars_per_sec=100,
        expr_section=expr_section,
    )
    # First: grin expression
    assert dummy.sent[0] == ("grin", 0.9)
    # Second: neutral → clear (empty targets)
    assert dummy.sent[1] == (None, 0)
    # Last: final clear at end of audio
    assert dummy.sent[-1] == (None, 0)
