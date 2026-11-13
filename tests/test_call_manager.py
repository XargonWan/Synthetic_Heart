"""Tests for the centralised call service (``core.call_manager``)."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import core.call_ack_cache as cac
import core.call_manager as cm
from core.call_manager import (
    CallCapabilities,
    CallError,
    CallKind,
    CallProvider,
    CallSession,
    CallState,
    get_call_manager,
)


class _FakeProvider(CallProvider):
    def __init__(
        self,
        name: str = "fake",
        capabilities: CallCapabilities = CallCapabilities(),
    ) -> None:
        self.name = name
        self.capabilities = capabilities
        self.played: list[Path] = []
        self.speaking = False
        self.started: list[str] = []
        self.ended: list[str] = []

    async def play_audio(self, session: CallSession, path: Path) -> None:
        self.played.append(path)

    def is_agent_speaking(self, session: CallSession) -> bool:
        return self.speaking

    async def on_call_started(self, session: CallSession) -> None:
        self.started.append(session.call_id)

    async def on_call_ended(self, session: CallSession) -> None:
        self.ended.append(session.call_id)


_REAL_PLAY_SIGNAL = cm.CallManager._play_signal


@pytest.fixture(autouse=True)
def _no_start_end_sounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pickup / hang-up sounds are tested on their own below."""

    async def _silent(self: Any, session: Any, kind: str) -> None:
        return None

    monkeypatch.setattr(cm.CallManager, "_play_signal", _silent)


@pytest.fixture(autouse=True)
def _no_ack_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never reach Vox/DB from the manager's background ack warm-up."""

    async def _prepare(self: Any, languages: Any = None) -> dict[str, Path]:
        return {}

    monkeypatch.setattr(cac.CallAckCache, "prepare", _prepare)


def _config(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    def _get_value(key: str, default: Any = None, **_: Any) -> Any:
        return overrides.get(key, default)

    monkeypatch.setattr(cm.config_registry, "get_value", _get_value)


def _jpeg(size: tuple[int, int] = (64, 32), color: str = "red") -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_request_call_without_provider_fails_closed() -> None:
    with pytest.raises(CallError):
        await get_call_manager().request_call("ghost", "ghost/1")


@pytest.mark.asyncio
async def test_request_call_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = get_call_manager()
    mgr.register_provider(_FakeProvider())
    _config(monkeypatch, CALL_ENABLED=False)
    with pytest.raises(CallError):
        await mgr.request_call("fake", "fake/1")


@pytest.mark.asyncio
async def test_request_call_lifecycle_and_hooks() -> None:
    mgr = get_call_manager()
    provider = _FakeProvider()
    mgr.register_provider(provider)

    session = await mgr.request_call("fake", "fake/1", user_id="u1")
    assert session.state is CallState.ACTIVE
    assert mgr.get_active_call("fake/1") is session
    assert provider.started == [session.call_id]

    # A second request on the same path reuses the call.
    assert await mgr.request_call("fake", "fake/1") is session

    assert await mgr.end_call(session.call_id) is True
    assert session.state is CallState.ENDED
    assert mgr.get_active_call("fake/1") is None
    assert provider.ended == [session.call_id]
    assert await mgr.end_call(session.call_id) is False


@pytest.mark.asyncio
async def test_video_degrades_to_audio_without_capability() -> None:
    mgr = get_call_manager()
    mgr.register_provider(_FakeProvider("audio_only"))
    mgr.register_provider(
        _FakeProvider("cam", CallCapabilities(audio=True, video=True))
    )

    degraded = await mgr.request_call("audio_only", "a/1", kind=CallKind.VIDEO)
    assert degraded.kind is CallKind.AUDIO and degraded.degraded is True

    video = await mgr.request_call("cam", "c/1", kind=CallKind.VIDEO)
    assert video.kind is CallKind.VIDEO and video.degraded is False


@pytest.mark.asyncio
async def test_unregister_provider_ends_its_calls() -> None:
    mgr = get_call_manager()
    mgr.register_provider(_FakeProvider())
    session = await mgr.request_call("fake", "fake/1")
    mgr.unregister_provider("fake")
    assert session.state is CallState.ENDED
    assert mgr.get_active_call("fake/1") is None


@pytest.mark.asyncio
async def test_transcribed_message_plays_the_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mgr = get_call_manager()
    provider = _FakeProvider()
    mgr.register_provider(provider)
    session = await mgr.request_call("fake", "fake/1", languages=["it"])

    # The signal cannot be prepared -> silent no-op, with the reason recorded.
    with monkeypatch.context() as patch:
        patch.setattr(cac.CallAckCache, "get", lambda self, language=None: None)
        assert await mgr.on_message_transcribed(session.call_id) is False
    assert session.last_ack == "not_ready"

    clip = tmp_path / "chime.wav"
    clip.write_bytes(b"RIFF")
    cache = cac.get_call_ack_cache()
    cache._path = clip
    assert await mgr.on_message_transcribed(session.call_id) is True
    assert provider.played == [clip]
    assert session.utterances == 2

    # A burst of speech-end events for one utterance acks only once.
    assert await mgr.on_message_transcribed(session.call_id) is False
    assert session.last_ack == "debounced"
    assert provider.played == [clip]
    session._last_ack_at -= 60
    assert await mgr.on_message_transcribed(session.call_id) is True
    assert provider.played == [clip, clip]

    # Unknown call and disabled ack are no-ops.
    assert await mgr.on_message_transcribed("nope") is False
    _config(monkeypatch, CALL_ACK_ENABLED=False)
    session._last_ack_at -= 60
    assert await mgr.on_message_transcribed(session.call_id) is False
    assert session.last_ack == "disabled"
    assert provider.played == [clip, clip]


@pytest.mark.asyncio
async def test_screen_frames_throttle_dedup_and_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = get_call_manager()
    mgr.register_provider(_FakeProvider("noshare"))
    mgr.register_provider(
        _FakeProvider("share", CallCapabilities(audio=True, screen_share=True))
    )
    no = await mgr.request_call("noshare", "n/1")
    yes = await mgr.request_call("share", "s/1")

    frame = _jpeg()
    assert mgr.submit_screen_frame(no.call_id, frame) is False
    assert mgr.submit_screen_frame("nope", frame) is False

    _config(monkeypatch, CALL_SCREEN_FRAME_MIN_INTERVAL_SEC=0.0)
    assert mgr.submit_screen_frame(yes.call_id, frame) is True
    assert yes.latest_screen_frame
    # Identical frame -> dropped.
    assert mgr.submit_screen_frame(yes.call_id, frame) is False
    # Different frame -> kept.
    assert mgr.submit_screen_frame(yes.call_id, _jpeg(color="blue")) is True

    # Faster than the minimum interval -> dropped.
    _config(monkeypatch, CALL_SCREEN_FRAME_MIN_INTERVAL_SEC=3600.0)
    assert mgr.submit_screen_frame(yes.call_id, _jpeg(color="green")) is False


@pytest.mark.asyncio
async def test_screen_frame_downscaled(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import Image

    mgr = get_call_manager()
    mgr.register_provider(
        _FakeProvider("share", CallCapabilities(audio=True, screen_share=True))
    )
    session = await mgr.request_call("share", "s/1")
    _config(
        monkeypatch,
        CALL_SCREEN_FRAME_MIN_INTERVAL_SEC=0.0,
        CALL_SCREEN_MAX_EDGE_PX=100,
    )
    assert mgr.submit_screen_frame(session.call_id, _jpeg((400, 200))) is True
    assert session.latest_screen_frame is not None
    with Image.open(io.BytesIO(session.latest_screen_frame)) as img:
        assert max(img.size) <= 100


@pytest.mark.asyncio
async def test_mark_turn_flags_message_and_attaches_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = get_call_manager()
    mgr.register_provider(
        _FakeProvider("share", CallCapabilities(audio=True, screen_share=True))
    )
    session = await mgr.request_call("share", "s/1")

    plain = mgr.mark_turn(SimpleNamespace(text="hi"), session.call_id)
    assert plain._call is True and plain.call_id == session.call_id
    # The reply is spoken: the explicit TTS request and the voice-input mark.
    assert plain.request_tts is True and plain.is_voice_input is True
    assert not getattr(plain, "attachments", None)

    _config(monkeypatch, CALL_SCREEN_FRAME_MIN_INTERVAL_SEC=0.0)
    mgr.submit_screen_frame(session.call_id, _jpeg())
    existing = {"mime_type": "image/png", "data": "x", "filename": "a.png"}
    msg = mgr.mark_turn(SimpleNamespace(text="hi", attachments=[existing]), session)
    assert msg.attachments[0] == existing
    shot = msg.attachments[1]
    assert shot["mime_type"] == "image/jpeg"
    assert base64.b64decode(shot["data"]) == session.latest_screen_frame

    # Unknown / ended calls leave the message untouched.
    other = SimpleNamespace(text="hi")
    assert mgr.mark_turn(other, "nope") is other
    assert not hasattr(other, "_call") and not hasattr(other, "request_tts")
    await mgr.end_call(session.call_id)
    ended = SimpleNamespace(text="hi")
    mgr.mark_turn(ended, session)
    assert not hasattr(ended, "_call")


@pytest.mark.asyncio
async def test_no_signal_while_the_synth_is_speaking(tmp_path: Path) -> None:
    mgr = get_call_manager()
    provider = _FakeProvider()
    mgr.register_provider(provider)
    session = await mgr.request_call("fake", "fake/1")
    clip = tmp_path / "chime.wav"
    clip.write_bytes(b"RIFF")
    cac.get_call_ack_cache()._path = clip

    provider.speaking = True
    assert await mgr.on_message_transcribed(session.call_id) is False
    assert session.last_ack == "synth_speaking" and provider.played == []
    provider.speaking = False
    assert await mgr.on_message_transcribed(session.call_id) is True


@pytest.mark.asyncio
async def test_pickup_sound_on_start_and_hangup_sound_on_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import core.media_url_utils as mu

    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: tmp_path / "tts")
    monkeypatch.setattr(cm.CallManager, "_play_signal", _REAL_PLAY_SIGNAL)
    mgr = get_call_manager()
    provider = _FakeProvider()
    mgr.register_provider(provider)

    session = await mgr.request_call("fake", "fake/1")
    assert [p.name for p in provider.played] == ["pickup-v1.wav"]

    await mgr.end_call(session.call_id)
    assert [p.name for p in provider.played] == ["pickup-v1.wav", "hangup-v1.wav"]
    assert all(p.is_file() for p in provider.played)


@pytest.mark.asyncio
async def test_start_end_sounds_can_be_switched_off_and_never_break_a_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import core.media_url_utils as mu

    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: tmp_path / "tts")
    monkeypatch.setattr(cm.CallManager, "_play_signal", _REAL_PLAY_SIGNAL)
    mgr = get_call_manager()
    provider = _FakeProvider()
    mgr.register_provider(provider)

    _config(monkeypatch, CALL_SOUNDS_ENABLED=False)
    session = await mgr.request_call("fake", "fake/1")
    await mgr.end_call(session.call_id)
    assert provider.played == []

    # A provider that cannot play audio must not stop a call from starting or ending.
    _config(monkeypatch, CALL_SOUNDS_ENABLED=True)

    async def _boom(path: Path) -> None:
        raise RuntimeError("no speaker")

    class _Deaf(_FakeProvider):
        async def play_audio(self, session: CallSession, path: Path) -> None:
            await _boom(path)

    mgr.register_provider(_Deaf("deaf"))
    session = await mgr.request_call("deaf", "deaf/1")
    assert session.state is CallState.ACTIVE
    assert await mgr.end_call(session.call_id) is True
