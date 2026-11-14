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
        self.frames: list[tuple[int, Any, bytes, Any]] = []
        self.fail_frames = False

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

    async def send_frame(
        self, chat_id: int, device: Any, data: bytes, info: Any
    ) -> None:
        if self.fail_frames:
            raise RuntimeError("not in call")
        self.frames.append((chat_id, device, data, info))


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
    sent: list[dict[str, Any]] = []

    async def _send(payload: dict[str, Any]) -> bool:
        sent.append(payload)
        return True

    interface = SimpleNamespace(
        client=SimpleNamespace(), bot=SimpleNamespace(), send_message=_send, sent=sent
    )
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
async def test_a_stranger_is_not_answered_but_the_trainer_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    tg = importlib.import_module("interface.telegram.telegram")

    monkeypatch.setattr(tg, "get_trainer_id", lambda: TRAINER)
    calls, app = await _started()
    handler = app.handlers[("chat_update", _Status.INCOMING_CALL)]
    await handler(None, SimpleNamespace(chat_id=STRANGER, status=_Status.INCOMING_CALL))
    assert get_call_manager().get_active_call(f"telegram/{STRANGER}") is None
    assert app.played == []
    # The trainer is told who is calling, in their own chat.
    sent = calls._interface.sent
    assert len(sent) == 1 and sent[0]["interface_path"] == f"telegram/{TRAINER}"
    assert str(STRANGER) in sent[0]["text"]


@pytest.mark.asyncio
async def test_the_held_call_is_answered_when_the_trainer_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    tg = importlib.import_module("interface.telegram.telegram")

    monkeypatch.setattr(tg, "get_trainer_id", lambda: TRAINER)
    calls, app = await _started()
    handler = app.handlers[("chat_update", _Status.INCOMING_CALL)]
    await handler(None, SimpleNamespace(chat_id=STRANGER, status=_Status.INCOMING_CALL))

    result = await calls.answer_pending(STRANGER, True)
    assert result["status"] == "success"
    session = get_call_manager().get_active_call(f"telegram/{STRANGER}")
    assert session is not None and session.state is CallState.ACTIVE
    # It can only be answered once.
    assert (await calls.answer_pending(STRANGER, True))["status"] == "failed"


@pytest.mark.asyncio
async def test_a_declined_or_expired_call_is_not_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    tg = importlib.import_module("interface.telegram.telegram")

    monkeypatch.setattr(tg, "get_trainer_id", lambda: TRAINER)
    calls, app = await _started()
    handler = app.handlers[("chat_update", _Status.INCOMING_CALL)]
    await handler(None, SimpleNamespace(chat_id=STRANGER, status=_Status.INCOMING_CALL))
    assert (await calls.answer_pending(STRANGER, False))["status"] == "success"
    assert get_call_manager().get_active_call(f"telegram/{STRANGER}") is None

    await handler(None, SimpleNamespace(chat_id=STRANGER, status=_Status.INCOMING_CALL))
    calls._ringing[STRANGER] -= tc._RING_TIMEOUT_S + 1
    assert (await calls.answer_pending(STRANGER, True))["status"] == "failed"
    assert get_call_manager().get_active_call(f"telegram/{STRANGER}") is None
    # Nobody is asked about a call that is not ringing.
    assert (await calls.answer_pending(999, True))["status"] == "failed"


def test_the_answer_call_action_is_validated() -> None:
    from interface.telegram.telegram import ANSWER_CALL_ACTION, TelegramUserInterface

    ok = {"user_id": "42", "answer": True}
    assert TelegramUserInterface.validate_payload(ANSWER_CALL_ACTION, ok) == []
    assert TelegramUserInterface.validate_payload(
        ANSWER_CALL_ACTION, {"user_id": "x", "answer": "yes"}
    )
    assert ANSWER_CALL_ACTION in TelegramUserInterface.get_supported_actions()


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


# -- video: the SyntH's face, lent by a WebUI tab ---------------------------------


class _FakeTab:
    """A connected ``/api/call/camera`` WebSocket."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _video_lib() -> Any:
    lib = _fake_lib()
    lib.video_supported = True
    lib.Device = SimpleNamespace(MICROPHONE="mic", SCREEN="screen", CAMERA="camera")
    lib.video_stream = lambda path, video: SimpleNamespace(
        path=f"{path}+video{video.width}x{video.height}"
    )
    lib.frame_info = lambda video: ("info", video.width, video.height)
    return lib


def _jpeg() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buf, format="JPEG")
    return buf.getvalue()


async def _until(predicate: Any, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "not reached in time"
        await asyncio.sleep(0.01)


@pytest.fixture
def video_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.call_video as cv

    cv.reset_call_video_hub()
    # The browser source: the server renderer is exercised separately below.
    monkeypatch.setattr(cv.CallVideoHub, "_mode", staticmethod(lambda: "browser"))
    monkeypatch.setattr(tc, "_Lib", _video_lib)
    monkeypatch.setattr(
        tc.VideoSettings,
        "from_config",
        classmethod(lambda cls: cls(fps=30, width=64, height=48, quality=0.7)),
    )


async def _answer(app: _FakeApp) -> Any:
    await app.handlers[("chat_update", _Status.INCOMING_CALL)](
        None, SimpleNamespace(chat_id=TRAINER, status=_Status.INCOMING_CALL)
    )
    return get_call_manager().get_active_call(f"telegram/{TRAINER}")


@pytest.mark.asyncio
async def test_without_a_webui_tab_the_call_stays_audio_only(video_env: None) -> None:
    calls, app = await _started()
    provider = get_call_manager().get_provider("telegram")
    assert provider is not None and provider.capabilities.video is False

    session = await _answer(app)
    assert session is not None and session.kind.value == "audio"
    assert not session.degraded  # audio was all that was asked for
    assert "+video" not in app.played[0]
    assert calls._video == {}


@pytest.mark.asyncio
async def test_a_video_call_is_requested_when_a_tab_can_lend_the_avatar(
    video_env: None,
) -> None:
    from core.call_video import get_call_video_hub

    hub = get_call_video_hub()
    tab = _FakeTab()
    source = hub.register_source(tab)
    calls, app = await _started()
    provider = get_call_manager().get_provider("telegram")
    assert provider is not None and provider.capabilities.video is True

    session = await _answer(app)
    assert session is not None and session.kind.value == "video"
    assert not session.degraded
    # The camera is part of the very stream that answers the call.
    assert app.played[0].endswith("+video64x48")
    assert tab.sent[0]["type"] == "start" and tab.sent[0]["call_id"] == session.call_id

    # Frames from the tab reach py-tgcalls as I420 on the camera device.
    assert hub.submit_frame(source, _jpeg()) is True
    await _until(lambda: app.frames)
    chat_id, device, data, info = app.frames[0]
    assert (chat_id, device, info) == (TRAINER, "camera", ("info", 64, 48))
    assert len(data) == 64 * 48 * 3 // 2
    # A later stream (the SyntH's reply) keeps the camera.
    await calls.play_file(TRAINER, Path("reply.wav"))
    assert app.played[-1].endswith("+video64x48")


@pytest.mark.asyncio
async def test_hanging_up_stops_the_video(
    video_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.call_video import get_call_video_hub

    hub = get_call_video_hub()
    tab = _FakeTab()
    hub.register_source(tab)
    calls, app = await _started()
    session = await _answer(app)
    assert session is not None
    pump = calls._video[TRAINER]["task"]

    real_sleep = asyncio.sleep

    async def _no_wait(seconds: float) -> None:
        await real_sleep(0)  # still yields: the video pump sleeps through this too

    monkeypatch.setattr(tc.asyncio, "sleep", _no_wait)
    assert (await calls.hangup(TRAINER))["status"] == "success"
    assert pump.done()
    assert calls._video == {}
    assert tab.sent[-1] == {"type": "stop", "call_id": session.call_id}
    assert hub.has_source() is True  # the tab is free again


@pytest.mark.asyncio
async def test_the_caller_hanging_up_stops_the_video(video_env: None) -> None:
    from core.call_video import get_call_video_hub

    tab = _FakeTab()
    get_call_video_hub().register_source(tab)
    calls, app = await _started()
    session = await _answer(app)
    assert session is not None
    pump = calls._video[TRAINER]["task"]

    ended = app.handlers[("chat_update", _Status.DISCARDED_CALL | _Status.LEFT_CALL)]
    await ended(None, SimpleNamespace(chat_id=TRAINER, status=_Status.DISCARDED_CALL))
    assert pump.done() and calls._video == {}
    assert tab.sent[-1] == {"type": "stop", "call_id": session.call_id}


@pytest.mark.asyncio
async def test_video_can_be_switched_off(
    video_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.call_video import get_call_video_hub

    get_call_video_hub().register_source(_FakeTab())
    real = tc.config_registry.get_value
    monkeypatch.setattr(
        tc.config_registry,
        "get_value",
        lambda key, default=None, **kw: (
            False if key == "TELEGRAM_CALL_VIDEO_ENABLED" else real(key, default, **kw)
        ),
    )
    calls, app = await _started()
    provider = get_call_manager().get_provider("telegram")
    assert provider is not None and provider.capabilities.video is False
    session = await _answer(app)
    assert session is not None and session.kind.value == "audio"


@pytest.mark.asyncio
async def test_a_library_without_external_frames_stays_audio_only(
    video_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.call_video import get_call_video_hub

    tab = _FakeTab()
    get_call_video_hub().register_source(tab)
    monkeypatch.setattr(tc, "_Lib", _fake_lib)  # no ``video_supported``
    calls, app = await _started()
    session = await _answer(app)
    assert session is not None and session.kind.value == "audio"
    assert tab.sent == []  # the tab was never asked to capture


@pytest.mark.asyncio
async def test_losing_the_tab_degrades_the_call_to_audio(video_env: None) -> None:
    from core.call_video import get_call_video_hub

    hub = get_call_video_hub()
    tab = _FakeTab()
    source = hub.register_source(tab)
    calls, app = await _started()
    session = await _answer(app)
    assert session is not None and session.kind.value == "video"
    pump = calls._video[TRAINER]["task"]

    await hub.unregister_source(source)  # the browser tab was closed

    assert session.kind.value == "audio" and session.degraded
    assert pump.done() and calls._video == {}
    # The SyntH no longer claims the caller sees it, and later streams are audio-only.
    ctx = get_call_manager().turn_context(session.call_id)
    assert ctx is not None and ctx["caller_sees_you"] is False
    await calls.play_file(TRAINER, Path("reply.wav"))
    assert "+video" not in app.played[-1]


@pytest.mark.asyncio
async def test_a_library_that_keeps_refusing_frames_degrades_the_call(
    video_env: None,
) -> None:
    from core.call_video import get_call_video_hub

    hub = get_call_video_hub()
    source = hub.register_source(_FakeTab())
    calls, app = await _started()
    app.fail_frames = True
    session = await _answer(app)
    assert session is not None
    hub.submit_frame(source, _jpeg())

    await _until(lambda: session.kind.value == "audio")
    assert session.degraded and calls._video == {}
    assert app.frames == []


@pytest.mark.asyncio
async def test_a_call_can_show_the_face_drawn_by_the_server(
    video_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No tab at all: the server draws the avatar, frames go out as they are (I420)."""
    import core.avatar_render.source as src
    import core.call_video as cv

    made: list[Any] = []

    class _Producer:
        def __init__(self, path: Any, width: int, height: int, fps: int) -> None:
            self.spoken: list[Any] = []
            self.stopped = False
            made.append(self)

        async def start(self, sink: Any, on_failed: Any = None) -> None:
            sink(b"\x80" * (64 * 48 * 3 // 2))  # the renderer's first frame

        def speak(self, path: Any) -> None:
            self.spoken.append(path)

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(src, "AvatarVideoSource", _Producer)
    monkeypatch.setattr(src, "resolve_model_path", lambda configured="": Path("m.vrm"))
    monkeypatch.setattr(cv.CallVideoHub, "_mode", staticmethod(lambda: "server"))

    calls, app = await _started()
    provider = get_call_manager().get_provider("telegram")
    assert provider is not None
    session = await _answer(app)
    assert session is not None and session.kind.value == "video"
    assert app.played[0].endswith("+video64x48")

    await _until(lambda: app.frames)
    assert app.frames[0][2] == b"\x80" * (64 * 48 * 3 // 2)  # sent unconverted

    # The SyntH's spoken reply moves the mouth of the drawn face.
    await provider.play_speech(session, Path("reply.wav"))
    assert made[0].spoken == [Path("reply.wav")]

    await calls.hangup(TRAINER)
    assert made[0].stopped is True
