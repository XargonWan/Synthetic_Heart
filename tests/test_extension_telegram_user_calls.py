"""The Telegram call engine in isolation: a fake py-tgcalls, a fake ``ctx``, no network."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "telegram"
sys.path.insert(0, str(SOURCE))
_spec = importlib.util.spec_from_file_location("tgu_calls", SOURCE / "calls.py")
assert _spec and _spec.loader
calls = importlib.util.module_from_spec(_spec)
sys.modules["tgu_calls"] = calls
_spec.loader.exec_module(calls)

TRAINER = "777"


def make_wav(path: Path, seconds: float = 0.5) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


class FakeApp:
    def __init__(self) -> None:
        self.played: list[tuple[int, Any]] = []
        self.recorded: list[int] = []
        self.left: list[int] = []
        self.frames: list[tuple[int, Any, bytes]] = []
        self.handlers: dict[str, Any] = {}
        self.fail_frames = False
        self.fail_record = 0

    def on_update(self, flt: Any) -> Any:
        def register(handler: Any) -> Any:
            self.handlers[str(flt)] = handler
            return handler

        return register

    async def start(self) -> None:
        return None

    async def play(self, chat_id: int, stream: Any) -> None:
        self.played.append((chat_id, stream))

    async def record(self, chat_id: int, stream: Any) -> None:
        if self.fail_record:
            self.fail_record -= 1
            raise RuntimeError("not yet")
        self.recorded.append(chat_id)

    async def leave_call(self, chat_id: int) -> None:
        self.left.append(chat_id)

    async def send_frame(
        self, chat_id: int, device: Any, frame: bytes, info: Any
    ) -> None:
        if self.fail_frames:
            raise RuntimeError("refused")
        self.frames.append((chat_id, device, frame))


class FakeLib:
    video_supported = True
    can_record = True
    Direction = SimpleNamespace(INCOMING="in", OUTGOING="out")
    Device = SimpleNamespace(MICROPHONE="mic", SPEAKER="spk", CAMERA="cam")

    def __init__(self, app: FakeApp, video: bool = True) -> None:
        self.app = app
        self.video_supported = video
        self.filters = SimpleNamespace(
            chat_update=lambda status: f"chat:{status}",
            stream_frame=lambda **kw: "frames",
        )
        self.ChatUpdate = SimpleNamespace(
            Status=SimpleNamespace(INCOMING_CALL="incoming")
        )

    def PyTgCalls(self, client: Any) -> FakeApp:  # noqa: N802
        return self.app

    def ended_flags(self) -> str:
        return "ended"

    def frame_filter(self) -> str:
        return "frames"

    def record_stream(self, rate: int, channels: int) -> tuple[str, int, int]:
        return ("record", rate, channels)

    def audio_stream(self, path: Path) -> tuple[str, str]:
        return ("audio", path.name)

    def video_stream(self, path: Path, video: dict[str, int]) -> tuple[str, str, int]:
        return ("video", path.name, video["width"])

    def frame_info(self, video: dict[str, int]) -> str:
        return "info"


class FakeCalls:
    """``ctx.calls``: records what the engine tells the Core."""

    def __init__(self, tmp: Path) -> None:
        self.opened: list[dict[str, Any]] = []
        self.audio_sent: list[tuple[str, int, int, str]] = []
        self.ended: list[str] = []
        self.handlers: dict[str, Any] = {}
        self.video: dict[str, int] | None = None
        self.frames: list[dict[str, Any] | None] = []
        self.play_on_open: Path | None = None
        self.engine: Any = None

    def on_play(self, handler: Any) -> None:
        self.handlers["play"] = handler

    def on_ended(self, handler: Any) -> None:
        self.handlers["ended"] = handler

    def on_video_lost(self, handler: Any) -> None:
        self.handlers["lost"] = handler

    async def open(
        self,
        path: str,
        user_id: str,
        *,
        kind: str = "audio",
        announce: bool = True,
        **_: Any,
    ) -> dict[str, Any]:
        self.opened.append({"path": path, "user_id": user_id, "kind": kind})
        if (
            self.play_on_open is not None
        ):  # the Core's answering tone arrives during open
            await self.handlers["play"](
                "telegram", "", user_id, self.play_on_open, False, self.video
            )
        return {
            "call_id": f"call-{user_id}",
            "kind": kind,
            "degraded": False,
            "video": self.video,
        }

    async def audio(
        self,
        call_id: str,
        pcm: bytes,
        *,
        rate: int,
        channels: int,
        speaker_id: str = "",
        speaker_name: str = "",
    ) -> None:
        self.audio_sent.append((call_id, len(pcm), rate, speaker_name))

    async def video_frame(self, call_id: str) -> dict[str, Any] | None:
        return self.frames.pop(0) if self.frames else None

    async def end(self, call_id: str) -> bool:
        self.ended.append(call_id)
        return True


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.app = FakeApp()
        self.lib = FakeLib(self.app)
        self.core_calls = FakeCalls(tmp)
        self.sent_to_trainer: list[tuple[int, str]] = []
        self.logs: list[str] = []
        self.config: dict[str, Any] = {
            "TELEGRAM_CALL_ALLOWED_IDS": "55, 56",
            "TELEGRAM_CALL_VIDEO_ENABLED": True,
            "TELEGRAM_CALL_RX_RATE": 48000,
            "TELEGRAM_CALL_RX_CHANNELS": 1,
        }
        self.n = 0
        rig = self

        class Config:
            async def get(self, key: str) -> Any:
                return rig.config.get(key)

        class Interfaces:
            async def trainer(self, interface_id: str) -> str | None:
                return TRAINER

        class Log:
            def _w(self, m: str) -> None:
                rig.logs.append(m)

            info = warning = error = debug = _w

        class Client:
            async def get_entity(self, chat_id: int) -> Any:
                return SimpleNamespace(first_name="Dana", last_name="Lee")

            async def send_message(self, chat_id: int, text: str) -> None:
                rig.sent_to_trainer.append((chat_id, text))

        def inbox_path(name: str = "file") -> Path:
            rig.n += 1
            return tmp / f"{rig.n}_{name}"

        self.ctx = SimpleNamespace(
            log=Log(),
            config=Config(),
            interfaces=Interfaces(),
            calls=self.core_calls,
            inbox_path=inbox_path,
        )
        self.client = Client()
        self.engine = calls.TelegramCalls(self.ctx, "telegram", lib=self.lib)
        self.core_calls.engine = self.engine

    async def start(self) -> bool:
        return await self.engine.start(self.client)


def update(chat_id: int, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(chat_id=chat_id, **kw)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


class TestStartup:
    @pytest.mark.asyncio
    async def test_it_hooks_py_tgcalls_and_the_cores_callbacks(self, rig: Rig) -> None:
        assert await rig.start() is True
        assert set(rig.app.handlers) == {"chat:incoming", "chat:ended", "frames"}
        assert set(rig.core_calls.handlers) == {"play", "ended", "lost"}
        assert rig.engine.video_available() is True

    @pytest.mark.asyncio
    async def test_a_library_that_cannot_start_means_no_calls_and_no_crash(
        self, rig: Rig
    ) -> None:
        def boom(client: Any) -> Any:
            raise RuntimeError("no native library")

        rig.lib.PyTgCalls = boom  # type: ignore[method-assign]
        assert await rig.start() is False
        assert rig.engine.video_available() is False
        assert any("could not start" in line for line in rig.logs)

    @pytest.mark.asyncio
    async def test_video_needs_a_library_that_can_take_frames(self, rig: Rig) -> None:
        rig.lib.video_supported = False
        await rig.start()
        assert rig.engine.video_available() is False


class TestWhoMayCall:
    @pytest.mark.asyncio
    async def test_the_trainer_and_the_allowed_ids_are_answered_at_once(
        self, rig: Rig
    ) -> None:
        await rig.start()
        for caller in (int(TRAINER), 55, 56):
            await rig.app.handlers["chat:incoming"](None, update(caller))
        assert [o["user_id"] for o in rig.core_calls.opened] == [TRAINER, "55", "56"]
        assert rig.sent_to_trainer == []

    @pytest.mark.asyncio
    async def test_a_stranger_is_not_answered_the_trainer_is_asked(
        self, rig: Rig
    ) -> None:
        await rig.start()
        await rig.app.handlers["chat:incoming"](None, update(999))
        assert rig.core_calls.opened == [] and rig.app.played == []
        [(to, text)] = rig.sent_to_trainer
        assert to == int(TRAINER) and "Dana Lee" in text and "telegram/999" in text

    @pytest.mark.asyncio
    async def test_the_trainer_can_say_yes_once_while_it_rings(self, rig: Rig) -> None:
        await rig.start()
        await rig.app.handlers["chat:incoming"](None, update(999))
        assert (await rig.engine.answer_pending(999, True))["status"] == "success"
        assert [o["user_id"] for o in rig.core_calls.opened] == ["999"]
        again = await rig.engine.answer_pending(999, True)
        assert again["status"] == "failed"  # it is not ringing any more

    @pytest.mark.asyncio
    async def test_declining_leaves_the_call(self, rig: Rig) -> None:
        await rig.start()
        await rig.app.handlers["chat:incoming"](None, update(999))
        assert (await rig.engine.answer_pending(999, False))[
            "message"
        ] == "Call declined"
        assert rig.app.left == [999] and rig.core_calls.opened == []

    @pytest.mark.asyncio
    async def test_a_call_that_rang_too_long_cannot_be_answered(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await rig.start()
        await rig.app.handlers["chat:incoming"](None, update(999))
        monkeypatch.setattr(calls, "RING_TIMEOUT", -1.0)
        assert (await rig.engine.answer_pending(999, True))["status"] == "failed"
        assert rig.core_calls.opened == []


class TestOpening:
    @pytest.mark.asyncio
    async def test_the_cores_tone_is_the_stream_that_answers_and_the_call_listens(
        self, rig: Rig
    ) -> None:
        await rig.start()
        rig.core_calls.play_on_open = make_wav(rig.tmp / "pickup.wav")
        await rig.app.handlers["chat:incoming"](None, update(55))
        assert [chat for chat, _ in rig.app.played] == [55]
        assert rig.app.played[0][1][0] == "audio"
        assert rig.app.recorded == [55]  # without record() a >= 3 call is deaf
        assert rig.engine._calls[55]["call_id"] == "call-55"  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_without_a_tone_the_call_is_still_answered_with_silence(
        self, rig: Rig
    ) -> None:
        await rig.start()
        await rig.app.handlers["chat:incoming"](None, update(55))
        assert len(rig.app.played) == 1 and rig.app.played[0][1][0] == "audio"

    @pytest.mark.asyncio
    async def test_listening_is_retried_once(self, rig: Rig) -> None:
        await rig.start()
        rig.app.fail_record = 1
        await rig.app.handlers["chat:incoming"](None, update(55))
        assert rig.app.recorded == [55]

    @pytest.mark.asyncio
    async def test_a_core_that_refuses_the_call_leaves_it(self, rig: Rig) -> None:
        await rig.start()

        async def refuse(*a: Any, **k: Any) -> Any:
            raise RuntimeError("calls are disabled")

        rig.core_calls.open = refuse  # type: ignore[method-assign]
        await rig.app.handlers["chat:incoming"](None, update(55))
        assert rig.app.left == [55] and 55 not in rig.engine._calls  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_the_playback_survives_the_cores_cleanup_of_its_copy(
        self, rig: Rig
    ) -> None:
        await rig.start()
        await rig.app.handlers["chat:incoming"](None, update(55))
        reply = make_wav(rig.tmp / "reply.wav", 1.0)
        seconds = await rig.core_calls.handlers["play"](
            "telegram", "call-55", "55", reply, True, None
        )
        reply.unlink()  # the Core deletes its copy when the handler returns
        assert seconds == pytest.approx(1.0)
        stream = rig.app.played[-1][1]
        assert (rig.tmp / stream[1]).exists()  # the engine kept its own copy

    @pytest.mark.asyncio
    async def test_nothing_is_played_into_a_call_that_is_not_there(
        self, rig: Rig
    ) -> None:
        await rig.start()
        tone = make_wav(rig.tmp / "t.wav")
        assert (
            await rig.core_calls.handlers["play"](
                "telegram", "gone", "55", tone, False, None
            )
            == 0.0
        )
        assert rig.app.played == []  # py-tgcalls would ring the person back


class TestAudio:
    async def _call(self, rig: Rig) -> None:
        await rig.start()
        await rig.app.handlers["chat:incoming"](None, update(55))

    @pytest.mark.asyncio
    async def test_frames_are_sent_to_the_core_in_chunks_of_about_100_ms(
        self, rig: Rig
    ) -> None:
        await self._call(rig)
        frame = b"\x01\x00" * 480  # 10 ms at 48 kHz mono
        for _ in range(25):
            await rig.app.handlers["frames"](
                None,
                update(
                    55,
                    frames=[SimpleNamespace(frame=frame)],
                    direction="in",
                    device="mic",
                ),
            )
        sent = rig.core_calls.audio_sent
        assert len(sent) == 2  # 250 ms -> two full 100 ms chunks, 50 ms still buffered
        assert all(s[1] == 9600 and s[2] == 48000 and s[3] == "Dana Lee" for s in sent)

    @pytest.mark.asyncio
    async def test_our_own_stream_and_other_devices_are_not_the_caller(
        self, rig: Rig
    ) -> None:
        await self._call(rig)
        big = SimpleNamespace(frame=b"\x01\x00" * 4800)
        await rig.app.handlers["frames"](
            None, update(55, frames=[big], direction="out", device="mic")
        )
        await rig.app.handlers["frames"](
            None, update(55, frames=[big], direction="in", device="cam")
        )
        await rig.app.handlers["frames"](
            None, update(999, frames=[big], direction="in", device="mic")
        )
        assert rig.core_calls.audio_sent == []

    @pytest.mark.asyncio
    async def test_a_core_that_drops_a_chunk_does_not_end_the_call(
        self, rig: Rig
    ) -> None:
        await self._call(rig)

        async def fail(*a: Any, **k: Any) -> None:
            raise RuntimeError("rate limited")

        rig.core_calls.audio = fail  # type: ignore[method-assign]
        big = SimpleNamespace(frame=b"\x01\x00" * 4800)
        await rig.app.handlers["frames"](
            None, update(55, frames=[big], direction="in", device="mic")
        )
        assert 55 in rig.engine._calls  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_the_incoming_format_is_configurable(self, rig: Rig) -> None:
        rig.config["TELEGRAM_CALL_RX_RATE"] = 16000
        rig.config["TELEGRAM_CALL_RX_CHANNELS"] = 2
        await self._call(rig)
        big = SimpleNamespace(frame=b"\x01\x00" * 3200)  # 100 ms of 16 kHz stereo
        await rig.app.handlers["frames"](
            None, update(55, frames=[big], direction="in", device="mic")
        )
        assert rig.core_calls.audio_sent[0][1:3] == (6400, 16000)


class TestEnding:
    @pytest.mark.asyncio
    async def test_the_platform_ending_the_call_tells_the_core(self, rig: Rig) -> None:
        await rig.start()
        await rig.app.handlers["chat:incoming"](None, update(55))
        await rig.app.handlers["chat:ended"](None, update(55, status="closed"))
        assert rig.core_calls.ended == ["call-55"] and rig.app.left == [55]
        assert 55 not in rig.engine._calls  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_an_end_for_an_unknown_call_is_ignored(self, rig: Rig) -> None:
        await rig.start()
        await rig.app.handlers["chat:ended"](None, update(55))
        assert rig.core_calls.ended == [] and rig.app.left == []

    @pytest.mark.asyncio
    async def test_the_core_ending_the_call_leaves_it(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await rig.start()
        await rig.app.handlers["chat:incoming"](None, update(55))

        async def instant(_: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", instant)
        await rig.core_calls.handlers["ended"]("telegram", "call-55", "55")
        assert rig.app.left == [55] and 55 not in rig.engine._calls  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_hanging_up_by_request_and_dialling_out(self, rig: Rig) -> None:
        await rig.start()
        assert (await rig.engine.hangup(55))["message"] == "No call with that user"
        assert (await rig.engine.dial(55))["status"] == "success"
        assert (await rig.engine.hangup(55))["message"] == "Call ended"
        assert rig.core_calls.ended == ["call-55"]
        assert (await rig.engine.dial(999))["status"] == "failed"  # not allowed to call

    @pytest.mark.asyncio
    async def test_stopping_leaves_every_call(self, rig: Rig) -> None:
        await rig.start()
        await rig.app.handlers["chat:incoming"](None, update(55))
        await rig.app.handlers["chat:incoming"](None, update(56))
        await rig.engine.stop()
        assert sorted(rig.app.left) == [55, 56]


class TestVideo:
    VIDEO = {"fps": 50, "width": 4, "height": 2}

    @pytest.mark.asyncio
    async def test_the_face_is_pushed_into_the_call_and_each_frame_once(
        self, rig: Rig
    ) -> None:
        await rig.start()
        rig.core_calls.video = self.VIDEO
        rig.core_calls.play_on_open = make_wav(rig.tmp / "pickup.wav")
        rig.core_calls.frames = [
            {"seq": 1, "frame": b"A" * 12},
            {"seq": 1, "frame": b"A" * 12},
            {"seq": 2, "frame": b"B" * 12},
        ]
        await rig.app.handlers["chat:incoming"](None, update(55))
        assert (
            rig.app.played[0][1][0] == "video"
        )  # the camera is part of the answering stream
        await asyncio.sleep(0.3)
        assert [f[2] for f in rig.app.frames] == [b"A" * 12, b"B" * 12]
        assert rig.app.frames[0][1] == "cam"

    @pytest.mark.asyncio
    async def test_a_core_without_the_face_means_an_audio_call(self, rig: Rig) -> None:
        await rig.start()
        rig.core_calls.video = None
        await rig.app.handlers["chat:incoming"](None, update(55))
        await asyncio.sleep(0.1)
        assert rig.app.frames == [] and rig.engine._calls[55]["video"] is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_a_library_that_keeps_refusing_frames_drops_the_video(
        self, rig: Rig
    ) -> None:
        await rig.start()
        rig.core_calls.video = self.VIDEO
        rig.app.fail_frames = True
        rig.core_calls.frames = [{"seq": i, "frame": b"x" * 12} for i in range(20)]
        await rig.app.handlers["chat:incoming"](None, update(55))
        await asyncio.sleep(0.4)
        assert rig.engine._calls[55]["video"] is None  # noqa: SLF001
        assert any("audio-only" in line for line in rig.logs)

    @pytest.mark.asyncio
    async def test_losing_the_face_source_stops_the_video_only(self, rig: Rig) -> None:
        await rig.start()
        rig.core_calls.video = self.VIDEO
        await rig.app.handlers["chat:incoming"](None, update(55))
        await rig.core_calls.handlers["lost"]("call-55")
        assert rig.engine._calls[55]["video"] is None and 55 in rig.engine._calls  # noqa: SLF001
        assert 55 not in rig.engine._video  # noqa: SLF001
