"""Telegram user-account calls (``interface.telegram.telegram_call``) with a fake py-tgcalls."""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import core.call_audio as ca
import core.media_url_utils as mu
from core.call_manager import CallState, get_call_manager
from interface.telegram import telegram_call as tc

TRAINER = 1001
STRANGER = 2002


class _Status:
    INCOMING_CALL = 1
    DISCARDED_CALL = 2
    LEFT_CALL = 4  # IntFlag-like: members can be OR-ed


class _Filters:
    @staticmethod
    def chat_update(flags: int) -> tuple[str, int]:
        return ("chat_update", flags)

    @staticmethod
    def stream_frame(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        return ("stream_frame", kwargs)


class _FakeApp:
    """What PyTgCalls does that this module uses: decorators, play, leave."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.handlers: dict[Any, Any] = {}
        self.played: list[str] = []
        self.left: list[int] = []
        self.started = False

    def on_update(self, flt: Any) -> Any:
        key = flt if not isinstance(flt[1], dict) else flt[0]

        def _register(fn: Any) -> Any:
            self.handlers[key] = fn
            return fn

        return _register

    async def start(self) -> None:
        self.started = True

    async def play(self, chat_id: int, stream: Any) -> None:
        self.played.append(f"{chat_id}:{stream.path}")

    async def leave_call(self, chat_id: int) -> None:
        self.left.append(chat_id)


def _fake_lib() -> Any:
    lib = SimpleNamespace()
    lib.PyTgCalls = _FakeApp
    lib.filters = _Filters
    lib.ChatUpdate = SimpleNamespace(Status=_Status)
    lib.MediaStream = lambda path: SimpleNamespace(path=path)
    lib.Direction = SimpleNamespace(INCOMING="in", OUTGOING="out")
    lib.Device = SimpleNamespace(MICROPHONE="mic", SCREEN="screen")
    lib.ended_flags = lambda: _Status.DISCARDED_CALL | _Status.LEFT_CALL
    lib.frame_filter = lambda: _Filters.stream_frame(directions="in", devices="mic")
    return lib


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: tmp_path / "tts")
    monkeypatch.setattr(tc, "_Lib", _fake_lib)
    monkeypatch.setattr(
        tc.TelegramCalls, "_allowed", staticmethod(lambda uid: uid == TRAINER)
    )

    async def _name(self: Any, chat_id: int) -> str:
        return "Xargon Wan"

    monkeypatch.setattr(tc.TelegramCalls, "_display_name", _name)


async def _started() -> tuple[tc.TelegramCalls, _FakeApp]:
    interface = SimpleNamespace(client=SimpleNamespace(), bot=SimpleNamespace())
    calls = tc.TelegramCalls(interface)
    assert await calls.start(interface.client) is True
    app = calls._app
    assert isinstance(app, _FakeApp) and app.started
    return calls, app


@pytest.mark.asyncio
async def test_start_registers_the_provider_and_the_handlers() -> None:
    calls, app = await _started()
    assert get_call_manager().get_provider("telegram") is not None
    assert ("chat_update", _Status.INCOMING_CALL) in app.handlers
    assert ("chat_update", _Status.DISCARDED_CALL | _Status.LEFT_CALL) in app.handlers
    assert "stream_frame" in app.handlers
    caps = get_call_manager().get_provider("telegram").capabilities  # type: ignore[union-attr]
    assert caps.audio and not caps.video


@pytest.mark.asyncio
async def test_start_is_a_noop_when_disabled_or_the_library_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken() -> Any:
        raise RuntimeError("py-tgcalls is not installed")

    monkeypatch.setattr(tc, "_Lib", _broken)
    interface = SimpleNamespace(client=SimpleNamespace(), bot=None)
    calls = tc.TelegramCalls(interface)
    assert await calls.start(interface.client) is False
    assert get_call_manager().get_provider("telegram") is None


@pytest.mark.asyncio
async def test_the_trainer_is_answered_with_the_pickup_tone() -> None:
    calls, app = await _started()
    handler = app.handlers[("chat_update", _Status.INCOMING_CALL)]
    await handler(None, SimpleNamespace(chat_id=TRAINER, status=_Status.INCOMING_CALL))

    session = get_call_manager().get_active_call(f"telegram/{TRAINER}")
    assert session is not None and session.state is CallState.ACTIVE
    # One stream: the pickup answers the call, and the manager did not add a second one.
    assert [p.split(":")[1].rsplit("/", 1)[-1] for p in app.played] == ["pickup-v1.wav"]
    assert calls.is_speaking(TRAINER) is True  # the tone is playing


@pytest.mark.asyncio
async def test_a_stranger_is_not_answered() -> None:
    _, app = await _started()
    handler = app.handlers[("chat_update", _Status.INCOMING_CALL)]
    await handler(None, SimpleNamespace(chat_id=STRANGER, status=_Status.INCOMING_CALL))
    assert get_call_manager().get_active_call(f"telegram/{STRANGER}") is None
    assert app.played == []


@pytest.mark.asyncio
async def test_incoming_frames_reach_the_pipeline_as_16k_mono(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, app = await _started()
    await app.handlers[("chat_update", _Status.INCOMING_CALL)](
        None, SimpleNamespace(chat_id=TRAINER, status=_Status.INCOMING_CALL)
    )
    fed: list[tuple[str, bytes, str, str]] = []
    monkeypatch.setattr(
        ca.get_call_audio_pipeline(),
        "feed",
        lambda call_id, pcm, speaker="", name="": fed.append(
            (call_id, pcm, speaker, name)
        ),
    )
    frame = SimpleNamespace(frame=b"\x00\x10" * 480)  # 10 ms at 48 kHz mono
    handler = app.handlers["stream_frame"]
    update = SimpleNamespace(
        chat_id=TRAINER, frames=[frame], direction="in", device="mic"
    )
    await handler(None, update)
    # Our own stream and a shared screen are not the caller's voice.
    await handler(
        None,
        SimpleNamespace(chat_id=TRAINER, frames=[frame], direction="out", device="mic"),
    )
    await handler(
        None,
        SimpleNamespace(
            chat_id=TRAINER, frames=[frame], direction="in", device="screen"
        ),
    )
    # Frames of a chat with no call are dropped.
    await handler(
        None,
        SimpleNamespace(chat_id=STRANGER, frames=[frame], direction="in", device="mic"),
    )

    session = get_call_manager().get_active_call(f"telegram/{TRAINER}")
    assert session is not None
    ((call_id, pcm, speaker, name),) = fed
    assert (
        call_id == session.call_id and speaker == str(TRAINER) and name == "Xargon Wan"
    )
    assert abs(len(pcm) - 320) <= 4  # 10 ms at 16 kHz mono 16-bit


@pytest.mark.asyncio
async def test_the_reply_is_played_into_the_call_and_tracked_as_speech(
    tmp_path: Path,
) -> None:
    calls, app = await _started()
    await app.handlers[("chat_update", _Status.INCOMING_CALL)](
        None, SimpleNamespace(chat_id=TRAINER, status=_Status.INCOMING_CALL)
    )
    app.played.clear()
    reply = tmp_path / "reply.wav"
    with wave.open(str(reply), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)

    assert await get_call_manager().route_speech(f"telegram/{TRAINER}", reply) is True
    assert app.played == [f"{TRAINER}:{reply}"]
    assert calls.is_speaking(TRAINER) is True


@pytest.mark.asyncio
async def test_the_caller_hanging_up_ends_the_call() -> None:
    calls, app = await _started()
    await app.handlers[("chat_update", _Status.INCOMING_CALL)](
        None, SimpleNamespace(chat_id=TRAINER, status=_Status.INCOMING_CALL)
    )
    ended = app.handlers[("chat_update", _Status.DISCARDED_CALL | _Status.LEFT_CALL)]
    await ended(None, SimpleNamespace(chat_id=TRAINER, status=_Status.DISCARDED_CALL))
    assert get_call_manager().get_active_call(f"telegram/{TRAINER}") is None
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_we_hanging_up_leaves_the_call_after_the_end_tone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, app = await _started()
    await app.handlers[("chat_update", _Status.INCOMING_CALL)](
        None, SimpleNamespace(chat_id=TRAINER, status=_Status.INCOMING_CALL)
    )

    async def _no_wait(seconds: float) -> None:
        return None

    monkeypatch.setattr(tc.asyncio, "sleep", _no_wait)
    assert (await calls.hangup(TRAINER))["status"] == "success"
    assert get_call_manager().get_active_call(f"telegram/{TRAINER}") is None
    assert app.left == [TRAINER]
    assert any("hangup-v1.wav" in p for p in app.played)  # the end tone went out first


@pytest.mark.asyncio
async def test_what_the_caller_says_becomes_a_call_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    # ``interface.telegram`` is rebound to the module (see its __init__), so the
    # ``import a.b.c as d`` form cannot walk the attributes.
    tg = importlib.import_module("interface.telegram.telegram")

    interface = SimpleNamespace(client=SimpleNamespace(), bot=SimpleNamespace())
    calls = tc.TelegramCalls(interface)
    assert await calls.start(interface.client)
    app = calls._app
    await app.handlers[("chat_update", _Status.INCOMING_CALL)](
        None, SimpleNamespace(chat_id=TRAINER, status=_Status.INCOMING_CALL)
    )
    session = get_call_manager().get_active_call(f"telegram/{TRAINER}")
    assert session is not None

    queued: list[tuple[Any, Any, bool]] = []

    async def _forward(
        bot: Any, message: Any, wrapped: Any, *, directed: bool, **_: Any
    ) -> None:
        queued.append((message, wrapped, directed))

    monkeypatch.setattr(tg, "_forward_to_queue", _forward)
    await (
        get_call_manager()
        .get_provider("telegram")
        .deliver_turn(  # type: ignore[union-attr]
            session, "ciao come va", str(TRAINER), "Xargon Wan"
        )
    )

    ((message, wrapped, directed),) = queued
    assert directed is True and message.chat_id == TRAINER
    assert (
        wrapped.text == "ciao come va"
        and wrapped.interface_path == f"telegram/{TRAINER}"
    )
    # Ranked as a call, answered by voice.
    assert (
        wrapped._call is True
        and wrapped.request_tts is True
        and wrapped.is_voice_input is True
    )
    assert message.from_user.full_name == "Xargon Wan"


def test_the_interface_advertises_calls() -> None:
    from core.interface_capabilities import interface_capabilities
    from interface.telegram.telegram import TelegramUserInterface

    assert "call" in interface_capabilities(TelegramUserInterface)
