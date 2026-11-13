"""Discord as a call provider: a live voice session is a call (audio only)."""

from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.media_url_utils as mu
from core.call_manager import CallKind, CallState, get_call_manager
from core.interface_capabilities import interface_capabilities
from interface.discord_interface import (
    DiscordCallProvider,
    DiscordInterface,
    LiveAudioBuffer,
    _wav_to_pcm_24k_mono,
)

PATH = "discord_live_42"


@pytest.fixture(autouse=True)
def _signals_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: tmp_path / "tts")


def _interface_with_live_session() -> tuple[DiscordInterface, LiveAudioBuffer]:
    interface = DiscordInterface(bot_token="")
    buffer = LiveAudioBuffer()
    interface._live_voice_state = {  # type: ignore[attr-defined]
        42: {"interface_path": PATH, "audio_buffer": buffer}
    }
    interface._register_call_provider()
    return interface, buffer


def _drain(buffer: LiveAudioBuffer) -> int:
    total = 0
    while chunk := buffer.read(1 << 20):
        total += len(chunk)
    return total


def test_discord_is_audio_only_and_advertises_calls() -> None:
    caps = DiscordCallProvider.capabilities
    assert caps.audio and not caps.video and not caps.screen_share
    assert "call" in interface_capabilities(DiscordInterface(bot_token=""))


@pytest.mark.asyncio
async def test_pickup_and_hangup_tones_go_through_the_live_audio_buffer() -> None:
    interface, buffer = _interface_with_live_session()

    await interface._open_call(PATH, "Xargon")
    session = get_call_manager().get_active_call(PATH)
    assert session is not None and session.state is CallState.ACTIVE
    # The pickup (~0.4 s) was mixed into the buffer the Live model speaks through:
    # 48 kHz stereo 16-bit = 192000 bytes per second.
    assert _drain(buffer) > 0.3 * 192000

    assert await interface._close_call(PATH) is True
    assert get_call_manager().get_active_call(PATH) is None
    assert _drain(buffer) > 0.3 * 192000  # the hang-up tone


@pytest.mark.asyncio
async def test_closing_a_call_that_is_not_open_is_a_noop() -> None:
    interface, _ = _interface_with_live_session()
    assert await interface._close_call(PATH) is False
    assert await interface._close_call(None) is False


@pytest.mark.asyncio
async def test_video_call_request_degrades_to_audio() -> None:
    interface, _ = _interface_with_live_session()
    session = await get_call_manager().request_call(
        "discord_bot", PATH, kind=CallKind.VIDEO
    )
    assert session.kind is CallKind.AUDIO and session.degraded is True


@pytest.mark.asyncio
async def test_no_live_session_means_silent_tones_but_a_working_call() -> None:
    interface = DiscordInterface(bot_token="")
    interface._register_call_provider()
    await interface._open_call(PATH)  # no _live_voice_state at all
    assert get_call_manager().get_active_call(PATH) is not None
    assert await interface._close_call(PATH) is True


@pytest.mark.asyncio
async def test_provider_knows_when_the_model_is_speaking() -> None:
    interface, buffer = _interface_with_live_session()
    session = await get_call_manager().request_call("discord_bot", PATH)
    provider = get_call_manager().get_provider("discord_bot")
    assert provider is not None
    _drain(buffer)
    # Right after the pickup was played the buffer counts as speaking...
    buffer.write(b"\x00\x01" * 2400)
    assert provider.is_agent_speaking(session) is True
    # ...and an unknown session has no buffer at all.
    other = SimpleNamespace(interface_path="discord_live_999")
    assert provider.is_agent_speaking(other) is False  # type: ignore[arg-type]


def test_wav_is_converted_to_the_buffer_format(tmp_path: Path) -> None:
    path = tmp_path / "in.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x10\x00\x10\x00" * 48000)  # 1 s stereo at 48 kHz
    pcm = _wav_to_pcm_24k_mono(str(path))
    assert abs(len(pcm) - 24000 * 2) < 200  # 1 s of 24 kHz mono 16-bit

    already = io.BytesIO()
    with wave.open(already, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 2400)
    (tmp_path / "ok.wav").write_bytes(already.getvalue())
    assert len(_wav_to_pcm_24k_mono(str(tmp_path / "ok.wav"))) == 4800


@pytest.mark.asyncio
async def test_stop_removes_the_provider_and_ends_its_calls() -> None:
    interface, _ = _interface_with_live_session()
    await interface._open_call(PATH)
    await interface.stop()
    assert get_call_manager().get_provider("discord_bot") is None
    assert get_call_manager().get_active_call(PATH) is None


# ---------------------------------------------------------------------------
# The default call: heard by speech-to-text, answered by a normal turn and voice
# ---------------------------------------------------------------------------


class _FakeVoiceClient:
    def __init__(self) -> None:
        self.playing = None
        self.sink = None
        self.stopped_listening = False

    def is_playing(self) -> bool:
        return self.playing is not None

    def stop(self) -> None:
        self.playing = None

    def play(self, source: object) -> None:
        self.playing = source

    def listen(self, sink: object) -> None:
        self.sink = sink

    def stop_listening(self) -> None:
        self.stopped_listening = True


def _guild_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DiscordInterface, _FakeVoiceClient]:
    voice_client = _FakeVoiceClient()
    guild = SimpleNamespace(id=7, name="Guild", voice_client=voice_client)
    channel = SimpleNamespace(id=9, name="General", guild=guild)
    interface = DiscordInterface(bot_token="")
    interface.client = SimpleNamespace(  # type: ignore[assignment]
        get_channel=lambda cid: channel,
        get_guild=lambda gid: guild,
        loop=asyncio.get_event_loop(),
        user=SimpleNamespace(id=1),
    )

    async def _joined(channel_id: object) -> dict[str, str]:
        return {"status": "success", "message": "Connected"}

    monkeypatch.setattr(interface, "_join_voice", _joined)
    interface._register_call_provider()
    return interface, voice_client


@pytest.mark.asyncio
async def test_a_voice_session_is_an_ordinary_call_unless_live_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interface, voice_client = _guild_setup(monkeypatch)
    live_calls: list[object] = []

    async def _no_live(*a: object, **k: object) -> dict[str, str]:  # pragma: no cover
        live_calls.append(a)
        return {"status": "failed", "message": "live must not run"}

    # Default: the Live route is off, so the voice session is a call.
    assert get_call_manager().live_enabled() is False
    result = await interface._start_live_voice(9, "Xargon")
    assert result["status"] == "success", result
    state = interface._call_voice_state[7]  # type: ignore[attr-defined]
    assert state["interface_path"] == "discord_bot/7/9"
    assert voice_client.playing is not None and voice_client.sink is state["sink"]
    session = get_call_manager().get_active_call("discord_bot/7/9")
    assert session is not None and state["call_id"] == session.call_id
    assert live_calls == []
    # The pickup went into the always-on buffer.
    assert _drain(state["audio_buffer"]) > 0.3 * 192000

    assert await interface._start_live_voice(9) == {
        "status": "success",
        "message": "Call already active",
    }

    # Stopping through the Live entry point stops the call.
    assert (await interface._stop_live_voice(7))["status"] == "success"
    assert get_call_manager().get_active_call("discord_bot/7/9") is None
    assert voice_client.stopped_listening and voice_client.playing is None
    assert 7 not in interface._call_voice_state  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_the_live_route_is_used_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interface, _ = _guild_setup(monkeypatch)
    monkeypatch.setattr(get_call_manager(), "live_enabled", lambda: True)
    started: list[object] = []

    async def _call_voice(*a: object, **k: object) -> dict[str, str]:
        started.append(a)
        return {"status": "success", "message": "generic"}

    monkeypatch.setattr(interface, "_start_call_voice", _call_voice)
    # With Live on, the generic path is not taken: the Live code runs (it needs a
    # real Discord client and engine, so here it fails cleanly instead).
    result = await interface._start_live_voice(9)
    assert started == [] and result["status"] == "failed"


@pytest.mark.asyncio
async def test_what_is_said_in_the_call_becomes_a_call_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.chat_context_manager as ccm
    from core import message_queue

    interface, _ = _guild_setup(monkeypatch)
    await interface._start_call_voice(9, "Xargon")
    session = get_call_manager().get_active_call("discord_bot/7/9")
    assert session is not None

    queued: list[tuple[object, dict[str, object]]] = []

    async def _enqueue(bot: object, message: object, **kwargs: object) -> None:
        queued.append((message, kwargs))

    async def _noop(*a: object, **k: object) -> None:
        return None

    monkeypatch.setattr(message_queue, "enqueue", _enqueue)
    monkeypatch.setattr(ccm, "add_message_to_context", _noop)
    import interface.discord_interface as discord_module

    monkeypatch.setattr(discord_module, "resolve_and_touch", _noop)

    provider = get_call_manager().get_provider("discord_bot")
    assert provider is not None
    await provider.deliver_turn(session, "ciao come va", "555", "Xargon")

    ((message, kwargs),) = queued
    assert (
        message.text == "ciao come va" and message.interface_path == "discord_bot/7/9"
    )  # type: ignore[attr-defined]
    # Ranked as a call, answered by voice, addressed to Synth without a mention.
    assert message._call is True and message.request_tts is True  # type: ignore[attr-defined]
    assert message.is_voice_input is True and message.is_explicit_trigger is True  # type: ignore[attr-defined]
    assert message.from_user.id == 555 and message.chat.id == 9  # type: ignore[attr-defined]
    assert (
        kwargs["interface_id"] == "discord_bot" and kwargs["skip_mention_check"] is True
    )


@pytest.mark.asyncio
async def test_the_reply_is_played_into_the_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    interface, _ = _guild_setup(monkeypatch)
    await interface._start_call_voice(9)
    buffer = interface._call_voice_state[7]["audio_buffer"]  # type: ignore[attr-defined]
    _drain(buffer)

    reply = tmp_path / "reply.wav"
    with wave.open(str(reply), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\x00\x10" * 22050)  # 1 s

    # Vox hands the clip to the call that owns the chat (a thread of it counts too).
    assert await get_call_manager().route_speech("discord_bot/7/9", reply) is True
    assert _drain(buffer) > 0.9 * 192000
    assert await get_call_manager().route_speech("discord_bot/7/9/123", reply) is True
    assert await get_call_manager().route_speech("discord_bot/7/10", reply) is False


def test_the_sink_feeds_16k_mono_to_the_pipeline() -> None:
    from interface.discord_interface import CallVoiceSink

    fed: list[tuple[str, bytes, str, str]] = []

    class _Pipeline:
        def feed(self, call_id: str, pcm: bytes, speaker: str, name: str) -> None:
            fed.append((call_id, pcm, speaker, name))

    sink = CallVoiceSink("call-1", _Pipeline())
    human = SimpleNamespace(id=55, display_name="Xargon", bot=False)
    bot = SimpleNamespace(id=1, display_name="Other", bot=True)
    stereo_20ms = b"\x00\x10\x00\x10" * 960  # 20 ms at 48 kHz stereo

    sink.write(human, SimpleNamespace(pcm=stereo_20ms))
    sink.write(bot, SimpleNamespace(pcm=stereo_20ms))  # other bots are not listened to
    sink.write(None, SimpleNamespace(pcm=stereo_20ms))  # unmapped SSRC
    assert sink.wants_opus() is False

    ((call_id, pcm, speaker, name),) = fed
    assert (call_id, speaker, name) == ("call-1", "55", "Xargon")
    assert abs(len(pcm) - 640) <= 4  # 20 ms at 16 kHz mono 16-bit


@pytest.mark.asyncio
async def test_a_call_is_found_by_the_voice_state_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interface, _ = _guild_setup(monkeypatch)
    assert interface._voice_session_state(7) is None
    await interface._start_call_voice(9)
    state = interface._voice_session_state(7)
    assert state is not None and state["channel_id"] == 9
