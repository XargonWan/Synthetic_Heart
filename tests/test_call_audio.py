"""Speech in a call: frames -> utterance -> text -> turn (``core.call_audio``)."""

from __future__ import annotations

import array
import asyncio
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import core.call_audio as ca
import core.call_manager as cm
from core.call_manager import CallProvider, CallSession, get_call_manager

CHUNK_S = 0.02
CHUNK = int(ca.SAMPLE_RATE * CHUNK_S) * 2  # bytes in 20 ms of 16 kHz mono 16-bit


def _tone(seconds: float, level: int = 6000) -> list[bytes]:
    n = int(ca.SAMPLE_RATE * CHUNK_S)
    samples = array.array(
        "h", (int(level * math.sin(i * 0.3)) for i in range(n))
    ).tobytes()
    return [samples] * int(seconds / CHUNK_S)


def _silence(seconds: float) -> list[bytes]:
    return [b"\x00" * CHUNK] * int(seconds / CHUNK_S)


class _Provider(CallProvider):
    name = "fake"
    plays_speech_in_call = True

    def __init__(self) -> None:
        self.turns: list[tuple[str, str, str]] = []
        self.spoken: list[Path] = []
        self.speaking = False

    async def play_audio(self, session: CallSession, path: Path) -> None:
        return None

    async def play_speech(self, session: CallSession, path: Path) -> None:
        self.spoken.append(path)

    def is_agent_speaking(self, session: CallSession) -> bool:
        return self.speaking

    async def deliver_turn(
        self, session: CallSession, text: str, speaker_id: str, speaker_name: str
    ) -> None:
        self.turns.append((text, speaker_id, speaker_name))


class _Auris:
    def __init__(self, text: str | None = "ciao come va") -> None:
        self.text = text
        self.paths: list[str] = []

    async def transcribe_audio(self, path: str, **_: Any) -> Any:
        self.paths.append(path)
        return SimpleNamespace(text=self.text) if self.text is not None else None


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A call on a fake provider, the energy detector and a controllable clock."""
    import core.core_initializer as ci
    import core.media_url_utils as mu
    import core.vad_service as vad

    # The pickup/hang-up sounds are written under the audio dir (no config store here).
    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: tmp_path / "tts")

    monkeypatch.setattr(vad.VAD_SERVICE, "is_available", lambda: False)
    clock = {"t": 1000.0}
    # Replace only this module's `time` (patching time.monotonic itself would also
    # freeze the event loop's own timers).
    monkeypatch.setattr(ca, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    auris = _Auris()
    monkeypatch.setattr(ci, "PLUGIN_REGISTRY", {"auris_plugin": auris})
    # The "tu-din" is not under test here.
    monkeypatch.setattr(
        cm.CallManager, "on_message_transcribed", _record_heard, raising=True
    )
    provider = _Provider()
    get_call_manager().register_provider(provider)
    return SimpleNamespace(provider=provider, auris=auris, clock=clock, heard=_HEARD)


_HEARD: list[str] = []


async def _record_heard(
    self: Any, call_id: str | None, language: str | None = None
) -> bool:
    _HEARD.append(str(call_id))
    return True


async def _call() -> tuple[ca.CallAudioPipeline, CallSession]:
    session = await get_call_manager().request_call("fake", "fake/1")
    pipeline = ca.get_call_audio_pipeline()
    pipeline.bind_loop(asyncio.get_running_loop())
    return pipeline, session


def _play(
    env: Any,
    pipeline: ca.CallAudioPipeline,
    session: CallSession,
    chunks: list[bytes],
    speaker: str = "u1",
) -> None:
    for chunk in chunks:
        env.clock["t"] += CHUNK_S
        pipeline.feed(session.call_id, chunk, speaker, "Xargon")


async def _settle(pipeline: ca.CallAudioPipeline) -> None:
    await asyncio.gather(*list(pipeline._tasks))


@pytest.fixture(autouse=True)
def _clear_heard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import core.media_url_utils as mu

    _HEARD.clear()
    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: tmp_path / "tts")


@pytest.mark.asyncio
async def test_an_utterance_becomes_a_transcribed_call_turn(env: Any) -> None:
    pipeline, session = await _call()
    _play(env, pipeline, session, _silence(0.5) + _tone(1.2) + _silence(1.2))
    await _settle(pipeline)

    assert env.provider.turns == [("ciao come va", "u1", "Xargon")]
    assert _HEARD == [session.call_id]  # the signal follows the transcription
    # What was recognised is the sentence plus a little lead-in, as a 16 kHz mono WAV.
    assert len(env.auris.paths) == 1


@pytest.mark.asyncio
async def test_a_short_burst_is_ignored(env: Any) -> None:
    pipeline, session = await _call()
    _play(env, pipeline, session, _silence(0.5) + _tone(0.3) + _silence(1.5))
    await _settle(pipeline)
    assert env.provider.turns == [] and env.auris.paths == []


@pytest.mark.asyncio
async def test_silence_and_empty_transcriptions_deliver_nothing(env: Any) -> None:
    pipeline, session = await _call()
    _play(env, pipeline, session, _silence(3.0))
    await _settle(pipeline)
    assert env.auris.paths == []

    env.auris.text = ""  # a cough or typing that Auris hears as nothing
    _play(env, pipeline, session, _tone(1.2) + _silence(1.2))
    await _settle(pipeline)
    assert env.auris.paths and env.provider.turns == [] and _HEARD == []


@pytest.mark.asyncio
async def test_the_synth_is_not_listened_to_while_it_speaks(env: Any) -> None:
    pipeline, session = await _call()
    env.provider.speaking = True
    _play(env, pipeline, session, _tone(1.5) + _silence(1.5))
    await _settle(pipeline)
    assert env.provider.turns == [] and env.auris.paths == []


@pytest.mark.asyncio
async def test_each_speaker_has_their_own_turn(env: Any) -> None:
    pipeline, session = await _call()
    _play(env, pipeline, session, _tone(1.2) + _silence(1.2), speaker="u1")
    await _settle(pipeline)
    env.auris.text = "e io"
    _play(env, pipeline, session, _tone(1.2) + _silence(1.2), speaker="u2")
    await _settle(pipeline)
    assert [t[:2] for t in env.provider.turns] == [
        ("ciao come va", "u1"),
        ("e io", "u2"),
    ]


@pytest.mark.asyncio
async def test_a_call_that_ended_drops_late_frames(env: Any) -> None:
    pipeline, session = await _call()
    await get_call_manager().end_call(session.call_id)
    _play(env, pipeline, session, _tone(1.2) + _silence(1.2))
    assert env.provider.turns == [] and env.auris.paths == []


@pytest.mark.asyncio
async def test_frames_from_another_thread_are_processed_on_the_loop(env: Any) -> None:
    pipeline, session = await _call()
    chunks = _silence(0.4) + _tone(1.2) + _silence(1.2)

    def _worker() -> None:
        _play(env, pipeline, session, chunks)

    await asyncio.to_thread(_worker)
    await asyncio.sleep(0)  # let call_soon_threadsafe schedule the task
    await _settle(pipeline)
    assert [t[0] for t in env.provider.turns] == ["ciao come va"]


@pytest.mark.asyncio
async def test_spoken_replies_are_routed_into_the_call(env: Any) -> None:
    await get_call_manager().request_call("fake", "fake/1")
    clip = Path("/tmp/reply.wav")
    assert await get_call_manager().route_speech("fake/1", clip) is True
    assert (
        await get_call_manager().route_speech("fake/1/42", clip) is True
    )  # a thread of it
    assert env.provider.spoken == [clip, clip]
    # Another chat, or none: not ours.
    assert await get_call_manager().route_speech("fake/2", clip) is False
    assert await get_call_manager().route_speech(None, clip) is False


@pytest.mark.asyncio
async def test_a_provider_that_plays_speech_elsewhere_declines() -> None:
    class _Browser(_Provider):
        name = "browser"
        plays_speech_in_call = False

    get_call_manager().register_provider(_Browser())
    await get_call_manager().request_call("browser", "browser/1")
    assert await get_call_manager().route_speech("browser/1", Path("/x.wav")) is False


def test_the_live_route_is_off_by_default() -> None:
    assert get_call_manager().live_enabled() is False
