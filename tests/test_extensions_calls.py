"""The call API: an extension carries the sound and the picture, the Core runs the call."""

from __future__ import annotations

import asyncio
import audioop
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from synth_sdk.rpc import ErrorCode

from core.call_manager import CallKind, CallManager
from core.extensions.api import register_core_api
from core.extensions.calls import (
    CAP_CALLS,
    MAX_FRAME_BYTES,
    CallPorts,
    RemoteCallProvider,
    register_call_api,
)
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.interfaces import (
    InterfacePorts,
    RemoteInterface,
    register_interface_api,
)
from core.extensions.manifest import (
    InterfaceProvision,
    Manifest,
    Permissions,
    Provides,
)
from core.extensions.media import MediaBridge
from core.extensions.memory_ports import (
    MemoryInterfaceHost,
    MemoryMessaging,
    memory_ports,
)
from tests.test_extensions_gateway import Client, Env
from tests.test_extensions_interface_extras import answering

CAPS = ("interfaces.register", "interfaces.inbound", CAP_CALLS)
PATTERNS = ("chat/{chat}",)


class FakeFeed:
    def __init__(self, frame: bytes, fmt: str = "i420") -> None:
        self.fmt = fmt
        self.settings = SimpleNamespace(width=4, height=2, fps=10)
        self._frame = frame

    def latest(self) -> tuple[bytes, int] | None:
        return (self._frame, 7) if self._frame else None


class FakeVideo:
    def __init__(self) -> None:
        self.feed: FakeFeed | None = FakeFeed(b"\x10" * 12)
        self.begun: list[str] = []
        self.ended: list[str] = []
        self.spoken: list[str] = []
        self.on_lost: Any = None

    async def begin(
        self, call_id: str, on_lost: Any = None, settings: Any = None
    ) -> Any:
        self.begun.append(call_id)
        self.on_lost = on_lost
        return self.feed

    async def end(self, call_id: str) -> None:
        self.ended.append(call_id)

    def get_feed(self, call_id: str) -> Any:
        return self.feed

    def speak(self, call_id: str, path: Any) -> None:
        self.spoken.append(call_id)


class FakePipeline:
    def __init__(self) -> None:
        self.fed: list[tuple[str, bytes, str, str]] = []
        self.loop: Any = None

    def feed(
        self, call_id: str, pcm: bytes, speaker_id: str = "", speaker_name: str = ""
    ) -> None:
        self.fed.append((call_id, pcm, speaker_id, speaker_name))

    def bind_loop(self, loop: Any) -> None:
        self.loop = loop

    def end(self, call_id: str) -> None:
        return None


def manifest() -> Manifest:
    return Manifest(
        id="dev/chat",
        name="dev/chat",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=CAPS),
        provides=Provides(interfaces=(InterfaceProvision(id="chat", paths=PATTERNS),)),
    )


class Rig:
    settings: dict[str, Any]

    def __init__(
        self,
        env: Env,
        manager: CallManager,
        video: FakeVideo,
        pipeline: FakePipeline,
        messaging: MemoryMessaging,
        host: MemoryInterfaceHost,
        tmp: Path,
    ) -> None:
        self.env, self.manager, self.video, self.pipeline = (
            env,
            manager,
            video,
            pipeline,
        )
        self.messaging, self.host, self.tmp = messaging, host, tmp

    async def connect(self, *, video: bool = False) -> Client:
        client = await self.env.connect(manifest(), grant=CAPS)
        await client.call(
            "interfaces.register",
            interface_id="chat",
            display_name="Chat",
            capabilities=["call"],
        )
        await client.call("calls.provider", interface_id="chat", video=video)
        return client


@pytest_asyncio.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host, messaging = MemoryInterfaceHost(), MemoryMessaging()
    manager, video, pipeline = CallManager(), FakeVideo(), FakePipeline()

    async def no_signal(session: Any, kind: str) -> None:
        return None

    monkeypatch.setattr(manager, "_play_signal", no_signal)
    monkeypatch.setattr(manager, "is_enabled", lambda: True)
    monkeypatch.setattr(manager, "_spawn", lambda coro: coro.close())
    layout_data = tmp_path / "data"

    media = MediaBridge(
        data_dir=lambda ext: layout_data / ext.replace("/", "_"),
        uid=lambda ext: None,
        inbound_root=tmp_path / "media",
        resolve_outbound=lambda raw: (None, "no"),
    )
    register_interface_api(gateway, InterfacePorts(host, messaging, media))
    holder: dict[str, Any] = {"settings": SimpleNamespace(fps=10, width=4, height=2)}
    call_ports = CallPorts(
        manager=lambda: manager,
        pipeline=lambda: pipeline,
        video=lambda: video,
        jpeg_to_i420=lambda jpeg, w, h: b"\x20" * (w * h * 3 // 2),
        video_settings=lambda: holder["settings"],
    )
    register_call_api(gateway, call_ports, messaging, media)
    env = Env(gateway, grants, ports)
    try:
        made = Rig(env, manager, video, pipeline, messaging, host, tmp_path)
        made.settings = holder
        yield made
    finally:
        await env.close()


def pcm(seconds: float, rate: int = 48000, channels: int = 1) -> bytes:
    return b"\x01\x00" * int(seconds * rate) * channels


class TestOpening:
    @pytest.mark.asyncio
    async def test_the_provider_needs_a_registered_interface_with_the_call_capability(
        self, rig: Rig
    ) -> None:
        client = await rig.env.connect(manifest(), grant=CAPS)
        await client.expect(
            ErrorCode.PERMISSION_DENIED,
            "calls.provider",
            interface_id="chat",
            video=False,
        )
        await client.call(
            "interfaces.register", interface_id="chat", display_name="Chat"
        )
        await client.expect(
            ErrorCode.PERMISSION_DENIED,
            "calls.provider",
            interface_id="chat",
            video=False,
        )
        assert rig.manager.providers() == {}

    @pytest.mark.asyncio
    async def test_open_starts_a_session_the_core_owns(self, rig: Rig) -> None:
        client = await rig.connect()
        result = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        assert (
            result["kind"] == "audio"
            and result["video"] is None
            and result["degraded"] is False
        )
        session = rig.manager.get_session(result["call_id"])
        assert session is not None and session.interface_path == "chat/42"
        assert session.interface_name == "chat" and session.user_id == "42"
        assert rig.pipeline.loop is not None

    @pytest.mark.asyncio
    async def test_open_refuses_paths_of_another_interface_or_shape(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        for path in ("other/42", "chat/42/9", "chat"):
            await client.expect(
                ErrorCode.INVALID_PARAMS, "calls.open", path=path, user_id="42"
            )
        assert rig.manager._sessions == {}  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_calls_disabled_is_reported(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = await rig.connect()
        monkeypatch.setattr(rig.manager, "is_enabled", lambda: False)
        error = await client.expect(
            ErrorCode.INVALID_PARAMS, "calls.open", path="chat/42", user_id="42"
        )
        assert "disabled" in error.message

    @pytest.mark.asyncio
    async def test_the_answering_tone_is_played_by_the_extension_during_open(
        self, rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = await rig.connect()
        tone = tmp_path / "pickup.wav"
        tone.write_bytes(b"RIFFtone")
        played: list[bytes] = []

        async def signal(session: Any, kind: str) -> None:
            await rig.manager.get_provider("chat").play_audio(session, tone)  # type: ignore[union-attr]

        monkeypatch.setattr(rig.manager, "_play_signal", signal)
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "call.play":
                played.append(Path(params["path"]).read_bytes())
                assert params["user_id"] == "42" and params["speech"] is False
                return {"seconds": 1.5}
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        await client.call("calls.open", path="chat/42", user_id="42")
        assert played == [b"RIFFtone"]
        provider = rig.manager.get_provider("chat")
        assert isinstance(provider, RemoteCallProvider)
        assert provider.is_agent_speaking(SimpleNamespace()) is True  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_video_call_gets_the_face_or_degrades_to_audio(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(video=True)
        result = await client.call(
            "calls.open", path="chat/1", user_id="1", kind="video", announce=False
        )
        assert result["kind"] == "video" and result["video"] == {
            "fps": 10,
            "width": 4,
            "height": 2,
        }
        assert rig.video.begun == [result["call_id"]]
        rig.video.feed = None
        second = await client.call(
            "calls.open", path="chat/2", user_id="2", kind="video", announce=False
        )
        assert (
            second["kind"] == "audio"
            and second["video"] is None
            and second["degraded"] is True
        )

    @pytest.mark.asyncio
    async def test_the_answering_stream_is_told_about_the_camera(
        self, rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = await rig.connect(video=True)
        tone = tmp_path / "t.wav"
        tone.write_bytes(b"x")
        got: list[Any] = []

        async def signal(session: Any, kind: str) -> None:
            await rig.manager.get_provider("chat").play_audio(session, tone)  # type: ignore[union-attr]

        monkeypatch.setattr(rig.manager, "_play_signal", signal)
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "call.play":
                got.append(params["video"])
                return {"seconds": 0.1}
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        await client.call("calls.open", path="chat/1", user_id="1", kind="video")
        assert got == [{"fps": 10, "width": 4, "height": 2}]

    @pytest.mark.asyncio
    async def test_a_face_too_big_for_one_message_means_an_audio_call(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(video=True)
        rig.settings["settings"] = SimpleNamespace(fps=12, width=1920, height=1080)
        result = await client.call(
            "calls.open", path="chat/1", user_id="1", kind="video", announce=False
        )
        assert result["kind"] == "audio" and result["video"] is None

    @pytest.mark.asyncio
    async def test_an_audio_only_provider_never_gets_video(self, rig: Rig) -> None:
        client = await rig.connect(video=False)
        result = await client.call(
            "calls.open", path="chat/1", user_id="1", kind="video", announce=False
        )
        assert result["kind"] == "audio" and rig.video.begun == []


class TestAudio:
    async def _open(self, rig: Rig) -> tuple[Client, str]:
        client = await rig.connect()
        opened = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        return client, opened["call_id"]

    @pytest.mark.asyncio
    async def test_audio_is_converted_to_16k_mono_for_the_pipeline(
        self, rig: Rig
    ) -> None:
        client, call_id = await self._open(rig)
        raw = pcm(0.1, 48000, 2)
        await client.call(
            "calls.audio",
            call_id=call_id,
            pcm=base64.b64encode(raw).decode(),
            rate=48000,
            channels=2,
            speaker_id="42",
            speaker_name="Alice",
        )
        [(cid, converted, speaker, name)] = rig.pipeline.fed
        assert (cid, speaker, name) == (call_id, "42", "Alice")
        expected = audioop.ratecv(
            audioop.tomono(raw, 2, 0.5, 0.5), 2, 1, 48000, 16000, None
        )[0]
        assert converted == expected and len(converted) == 3200

    @pytest.mark.asyncio
    async def test_bad_audio_is_refused(self, rig: Rig) -> None:
        client, call_id = await self._open(rig)
        good = base64.b64encode(pcm(0.01)).decode()
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "calls.audio",
            call_id=call_id,
            pcm="***",
            rate=16000,
        )
        odd = base64.b64encode(b"\x00\x01\x02").decode()
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "calls.audio",
            call_id=call_id,
            pcm=odd,
            rate=16000,
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "calls.audio",
            call_id=call_id,
            pcm=good,
            rate=12345,
        )
        huge = base64.b64encode(b"\x00\x00" * 250_000).decode()
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "calls.audio",
            call_id=call_id,
            pcm=huge,
            rate=16000,
        )
        assert rig.pipeline.fed == []

    @pytest.mark.asyncio
    async def test_an_extension_cannot_drive_a_call_that_is_not_its_own(
        self, rig: Rig
    ) -> None:
        client, _ = await self._open(rig)
        good = base64.b64encode(pcm(0.01)).decode()
        for method, extra in (
            ("calls.audio", {"pcm": good}),
            ("calls.video_frame", {}),
            ("calls.end", {}),
        ):
            await client.expect(
                ErrorCode.PERMISSION_DENIED, method, call_id="not-mine", **extra
            )
        assert rig.pipeline.fed == []


class TestSpeechAndTurns:
    @pytest.mark.asyncio
    async def test_a_spoken_reply_is_staged_played_and_cleaned_up(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        client = await rig.connect()
        opened = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        session = rig.manager.get_session(opened["call_id"])
        assert session is not None
        voice = tmp_path / "reply.wav"
        voice.write_bytes(b"RIFFvoice")
        seen: list[dict[str, Any]] = []
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "call.play":
                seen.append({**params, "bytes": Path(params["path"]).read_bytes()})
                return {"seconds": 2}
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        provider = rig.manager.get_provider("chat")
        assert provider is not None
        await provider.play_speech(session, voice)
        assert seen[0]["bytes"] == b"RIFFvoice" and seen[0]["speech"] is True
        assert seen[0]["video"] is None  # an audio call
        assert seen[0]["video"] is None  # an audio call
        assert not Path(seen[0]["path"]).exists()  # the staged copy is gone
        assert voice.exists()
        assert rig.video.spoken == [opened["call_id"]]
        assert provider.is_agent_speaking(session) is True

    @pytest.mark.asyncio
    async def test_what_the_caller_said_becomes_a_call_turn(self, rig: Rig) -> None:
        client = await rig.connect()
        opened = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        session = rig.manager.get_session(opened["call_id"])
        assert session is not None
        provider = rig.manager.get_provider("chat")
        assert provider is not None
        await provider.deliver_turn(session, "hello there", "42", "Alice Smith")
        [queued] = rig.messaging.enqueued
        message = queued["message"]
        assert queued["interface_id"] == "chat" and queued["skip_mention_check"] is True
        assert (message.text, message.chat_id, message.interface_path) == (
            "hello there",
            "42",
            "chat/42",
        )
        assert message._call is True and message.call_id == opened["call_id"]  # noqa: SLF001
        assert message.is_voice_input is True and message.request_tts is True

    @pytest.mark.asyncio
    async def test_a_failed_play_does_not_break_the_call(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        from synth_sdk.rpc import RpcError

        client = await rig.connect()
        opened = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        session = rig.manager.get_session(opened["call_id"])
        assert session is not None
        answering(client, {"call.play": RpcError(ErrorCode.INTERNAL_ERROR, "boom")})
        voice = tmp_path / "r.wav"
        voice.write_bytes(b"x")
        provider = rig.manager.get_provider("chat")
        assert provider is not None
        await provider.play_audio(session, voice)  # no raise
        assert provider.is_agent_speaking(session) is False


class TestVideoFrames:
    async def _open(self, rig: Rig) -> tuple[Client, str]:
        client = await rig.connect(video=True)
        opened = await client.call(
            "calls.open", path="chat/1", user_id="1", kind="video", announce=False
        )
        return client, opened["call_id"]

    @pytest.mark.asyncio
    async def test_the_newest_frame_is_served_as_i420(self, rig: Rig) -> None:
        client, call_id = await self._open(rig)
        got = await client.call("calls.video_frame", call_id=call_id)
        assert got["available"] and (got["seq"], got["width"], got["height"]) == (
            7,
            4,
            2,
        )
        assert base64.b64decode(got["frame"]) == b"\x10" * 12

    @pytest.mark.asyncio
    async def test_jpeg_sources_are_converted_by_the_core(self, rig: Rig) -> None:
        client, call_id = await self._open(rig)
        rig.video.feed = FakeFeed(b"\xff\xd8jpeg", fmt="jpeg")
        got = await client.call("calls.video_frame", call_id=call_id)
        assert base64.b64decode(got["frame"]) == b"\x20" * 12

    @pytest.mark.asyncio
    async def test_no_frame_yet_and_oversized_frames(self, rig: Rig) -> None:
        client, call_id = await self._open(rig)
        rig.video.feed = FakeFeed(b"")
        assert await client.call("calls.video_frame", call_id=call_id) == {
            "available": False
        }
        rig.video.feed = FakeFeed(b"\x00" * (MAX_FRAME_BYTES + 1))
        assert await client.call("calls.video_frame", call_id=call_id) == {
            "available": False,
            "too_large": True,
        }

    @pytest.mark.asyncio
    async def test_losing_the_source_tells_the_extension_and_drops_to_audio(
        self, rig: Rig
    ) -> None:
        client, call_id = await self._open(rig)
        seen = answering(client, {"call.video_lost": {}})
        await rig.video.on_lost()
        session = rig.manager.get_session(call_id)
        assert session is not None and session.kind is CallKind.AUDIO
        await asyncio.sleep(0.05)
        assert [m for m, _ in seen] == ["call.video_lost"]


class TestEnding:
    @pytest.mark.asyncio
    async def test_end_closes_the_session_and_stops_the_camera(self, rig: Rig) -> None:
        client = await rig.connect()
        opened = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        seen = answering(client, {"call.ended": {}})
        assert await client.call("calls.end", call_id=opened["call_id"]) == {
            "ended": True
        }
        assert rig.manager.get_session(opened["call_id"]) is None
        assert rig.video.ended == [opened["call_id"]]
        await client.expect(
            ErrorCode.PERMISSION_DENIED, "calls.end", call_id=opened["call_id"]
        )
        await asyncio.sleep(0.05)
        assert seen and seen[0][1]["call_id"] == opened["call_id"]

    @pytest.mark.asyncio
    async def test_the_core_ending_a_call_tells_the_extension(self, rig: Rig) -> None:
        client = await rig.connect()
        opened = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        seen = answering(client, {"call.ended": {}})
        await rig.manager.end_call(opened["call_id"])
        await asyncio.sleep(0.05)
        assert seen[0][1] == {
            "interface_id": "chat",
            "call_id": opened["call_id"],
            "user_id": "42",
        }

    @pytest.mark.asyncio
    async def test_a_crash_ends_its_calls_and_removes_the_provider(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        opened = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        await client.peer.close()
        await asyncio.sleep(0.1)
        assert rig.video.ended == [opened["call_id"]]  # the camera stops too
        assert rig.manager.get_session(opened["call_id"]) is None
        assert rig.manager.get_provider("chat") is None

    @pytest.mark.asyncio
    async def test_the_capability_is_required(self, rig: Rig) -> None:
        client = await rig.env.connect(manifest(), grant=("interfaces.register",))
        await client.expect(
            ErrorCode.PERMISSION_DENIED, "calls.open", path="chat/1", user_id="1"
        )


def test_remote_interface_is_what_the_provider_serves() -> None:
    assert issubclass(RemoteInterface, object)
