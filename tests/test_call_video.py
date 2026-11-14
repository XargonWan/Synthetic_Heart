"""Tests for the SyntH's call camera (``core.call_video``): the /api/call/camera socket,
the hub, and the JPEG -> I420 conversion."""

from __future__ import annotations

import io
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.call_video as cv
import core.webui_call as wc


@pytest.fixture(autouse=True)
def _fresh_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    cv.reset_call_video_hub()
    # These tests are about the browser source; the server renderer has its own below.
    monkeypatch.setattr(cv.CallVideoHub, "_mode", staticmethod(lambda: "browser"))


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    wc.mount_call_api(app)
    with TestClient(app) as test_client:
        yield test_client


def _jpeg(size: tuple[int, int] = (64, 48), color: Any = "red") -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


class _FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(payload)


# -- settings ---------------------------------------------------------------


def test_settings_are_clamped_and_even(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "CALL_VIDEO_FPS": 500,
        "CALL_VIDEO_WIDTH": 855,
        "CALL_VIDEO_HEIGHT": 9,
        "CALL_VIDEO_JPEG_QUALITY": 5.0,
    }
    monkeypatch.setattr(
        cv.config_registry,
        "get_value",
        lambda key, default=None, **_: values.get(key, default),
    )
    settings = cv.VideoSettings.from_config()
    assert settings.fps == 30
    assert settings.width == 854 and settings.height == 64
    assert settings.quality == 1.0


def test_default_settings_are_480p() -> None:
    settings = cv.VideoSettings.from_config()
    assert (settings.width, settings.height) == (854, 480)
    assert 1 <= settings.fps <= 30


# -- the hub ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tab_means_no_camera() -> None:
    hub = cv.get_call_video_hub()
    assert hub.has_source() is False
    assert await hub.begin("c1") is None


@pytest.mark.asyncio
async def test_begin_tells_the_tab_to_start_and_end_to_stop() -> None:
    hub = cv.get_call_video_hub()
    socket = _FakeSocket()
    source = hub.register_source(socket)
    assert hub.has_source() is True

    feed = await hub.begin("c1")
    assert feed is not None and feed.source is source
    assert socket.sent[0]["type"] == "start" and socket.sent[0]["call_id"] == "c1"
    assert socket.sent[0]["width"] % 2 == 0 and socket.sent[0]["height"] % 2 == 0
    # The tab is busy now: a second call finds no free camera.
    assert hub.has_source() is False
    assert await hub.begin("c2") is None

    await hub.end("c1")
    assert socket.sent[-1] == {"type": "stop", "call_id": "c1"}
    assert hub.has_source() is True
    await hub.end("c1")  # idempotent
    assert len(socket.sent) == 2


@pytest.mark.asyncio
async def test_a_tab_that_cannot_take_the_start_is_skipped() -> None:
    hub = cv.get_call_video_hub()
    good = _FakeSocket()
    hub.register_source(good)
    hub.register_source(_FakeSocket(fail=True))  # newest, but broken
    feed = await hub.begin("c1")
    assert feed is not None and feed.source is not None
    assert feed.source.websocket is good


@pytest.mark.asyncio
async def test_frames_are_accepted_only_from_the_tab_serving_the_call() -> None:
    hub = cv.get_call_video_hub()
    serving = hub.register_source(_FakeSocket())
    idle = hub.register_source(_FakeSocket())
    # The newest tab (``idle``) is chosen first; make the serving one explicit.
    feed = await hub.begin("c1")
    assert feed is not None and feed.source is not None
    serving, idle = feed.source, (serving if feed.source is idle else idle)

    jpeg = _jpeg()
    assert hub.submit_frame(idle, jpeg) is False
    assert hub.submit_frame(serving, jpeg) is True
    assert feed.latest() == (jpeg, 1)
    # Only the latest frame is kept.
    newer = _jpeg(color="blue")
    assert hub.submit_frame(serving, newer) is True
    assert feed.latest() == (newer, 2)


@pytest.mark.asyncio
async def test_bad_frames_are_dropped() -> None:
    hub = cv.get_call_video_hub()
    source = hub.register_source(_FakeSocket())
    feed = await hub.begin("c1")
    assert feed is not None
    assert hub.submit_frame(source, b"") is False
    assert hub.submit_frame(source, b"not a jpeg at all") is False
    assert hub.submit_frame(source, b"\xff\xd8" + b"0" * cv.MAX_FRAME_BYTES) is False
    assert feed.latest() is None
    await hub.end("c1")
    assert hub.submit_frame(source, _jpeg()) is False  # the call is over


@pytest.mark.asyncio
async def test_losing_the_tab_reports_the_feed_as_lost() -> None:
    hub = cv.get_call_video_hub()
    source = hub.register_source(_FakeSocket())
    lost: list[str] = []

    async def _on_lost() -> None:
        lost.append("lost")

    feed = await hub.begin("c1", on_lost=_on_lost)
    assert feed is not None
    await hub.unregister_source(source)
    assert lost == ["lost"]
    assert hub.get_feed("c1") is None
    assert hub.has_source() is False


@pytest.mark.asyncio
async def test_an_idle_tab_leaving_reports_nothing() -> None:
    hub = cv.get_call_video_hub()
    source = hub.register_source(_FakeSocket())
    await hub.unregister_source(source)
    assert hub.has_source() is False


# -- the WebSocket --------------------------------------------------------------


def test_camera_socket_registers_a_tab_and_forgets_it_on_close(
    client: TestClient,
) -> None:
    hub = cv.get_call_video_hub()
    assert hub.has_source() is False
    with client.websocket_connect("/api/call/camera") as ws:
        _wait_for(hub.has_source)
        # A frame before any call asked for video goes nowhere (and does no harm).
        ws.send_bytes(_jpeg())
    _wait_for(lambda: not hub.has_source())


def test_camera_socket_start_message_and_frames_roundtrip(
    client: TestClient,
) -> None:
    """The server pushes ``start`` to the tab; the tab's JPEGs become the feed."""
    hub = cv.get_call_video_hub()
    portal = client.portal
    assert portal is not None  # the client runs the app on one loop (context manager)
    with client.websocket_connect("/api/call/camera") as ws:
        _wait_for(hub.has_source)
        feed = portal.call(hub.begin, "call-1")
        assert feed is not None
        message = ws.receive_json()
        assert message["type"] == "start" and message["call_id"] == "call-1"
        assert message["fps"] >= 1

        jpeg = _jpeg()
        ws.send_bytes(jpeg)
        ws.send_text("ignored text")
        ws.send_bytes(jpeg)
        _wait_for(lambda: feed.seq == 2)
        assert feed.latest() == (jpeg, 2)

        portal.call(hub.end, "call-1")
        assert ws.receive_json() == {"type": "stop", "call_id": "call-1"}


def _wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.01)


def test_camera_socket_requires_the_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wc.webui_auth, "websocket_authorized", lambda ws: False)
    with pytest.raises(Exception):
        with client.websocket_connect("/api/call/camera"):
            pass
    assert cv.get_call_video_hub().has_source() is False


# -- conversion -----------------------------------------------------------------


def test_jpeg_to_i420_has_the_planar_size_and_limited_range() -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (255, 255, 255)).save(buf, format="JPEG")
    data = cv.jpeg_to_i420(buf.getvalue(), 64, 48)
    assert len(data) == 64 * 48 * 3 // 2
    y, u, v = (
        data[: 64 * 48],
        data[64 * 48 : 64 * 48 * 5 // 4],
        data[64 * 48 * 5 // 4 :],
    )
    assert len(u) == len(v) == 32 * 24
    # White is Y=235, neutral chroma 128 in limited-range BT.601 (a little tolerance
    # for JPEG).
    assert 232 <= y[0] <= 235
    assert 126 <= u[0] <= 130 and 126 <= v[0] <= 130

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (0, 0, 0)).save(buf, format="JPEG")
    black = cv.jpeg_to_i420(buf.getvalue(), 64, 48)
    assert 16 <= black[0] <= 19


def test_jpeg_to_i420_fits_a_frame_of_another_size() -> None:
    data = cv.jpeg_to_i420(_jpeg((100, 100)), 64, 48)
    assert len(data) == 64 * 48 * 3 // 2


def test_jpeg_to_i420_rejects_odd_sizes_and_garbage() -> None:
    with pytest.raises(ValueError):
        cv.jpeg_to_i420(_jpeg(), 63, 48)
    with pytest.raises(ValueError):
        cv.jpeg_to_i420(b"\xff\xd8garbage", 64, 48)


# -- the server renderer as the source -------------------------------------------


class _FakeProducer:
    """Stands in for ``core.avatar_render.source.AvatarVideoSource``."""

    instances: list["_FakeProducer"] = []
    unavailable = False

    def __init__(self, path: Any, width: int, height: int, fps: int) -> None:
        self.size = (width, height, fps)
        self.spoken: list[Any] = []
        self.stopped = False
        self.sink: Any = None
        self.on_failed: Any = None
        _FakeProducer.instances.append(self)

    async def start(self, sink: Any, on_failed: Any = None) -> None:
        if _FakeProducer.unavailable:
            from core.avatar_render.renderer import RendererUnavailable

            raise RendererUnavailable("no GL")
        self.sink, self.on_failed = sink, on_failed

    def speak(self, path: Any) -> None:
        self.spoken.append(path)

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def server_source(monkeypatch: pytest.MonkeyPatch) -> type[_FakeProducer]:
    import core.avatar_render.source as src

    _FakeProducer.instances = []
    _FakeProducer.unavailable = False
    monkeypatch.setattr(src, "AvatarVideoSource", _FakeProducer)
    monkeypatch.setattr(
        src,
        "resolve_model_path",
        lambda configured="": __import__("pathlib").Path("m.vrm"),
    )
    monkeypatch.setattr(cv.CallVideoHub, "_mode", staticmethod(lambda: "server"))
    return _FakeProducer


@pytest.mark.asyncio
async def test_the_server_can_be_the_source_without_any_tab(
    server_source: type[_FakeProducer],
) -> None:
    hub = cv.get_call_video_hub()
    assert hub.has_source() is False
    assert hub.video_possible() is True  # drawn on the server: no tab needed

    feed = await hub.begin("c1")
    assert feed is not None and feed.fmt == "i420" and feed.source is None
    producer = server_source.instances[0]
    assert producer.size[:2] == (feed.settings.width, feed.settings.height)

    producer.sink(b"frame-1")  # what the renderer does for every frame
    assert feed.latest() == (b"frame-1", 1)
    # A browser frame can never be mixed into a server-drawn feed.
    assert hub.submit_frame(hub.register_source(_FakeSocket()), _jpeg()) is False

    hub.speak("c1", "voice.wav")
    assert producer.spoken == ["voice.wav"]
    await hub.end("c1")
    assert producer.stopped is True and hub.get_feed("c1") is None
    hub.speak("c1", "later.wav")  # a finished call is ignored
    assert producer.spoken == ["voice.wav"]


@pytest.mark.asyncio
async def test_server_mode_never_falls_back_to_a_tab(
    server_source: type[_FakeProducer],
) -> None:
    hub = cv.get_call_video_hub()
    hub.register_source(_FakeSocket())
    server_source.unavailable = True
    assert await hub.begin("c1") is None
    # It learnt the renderer does not work here.
    assert hub.video_possible() is False


@pytest.mark.asyncio
async def test_auto_prefers_the_server_and_falls_back_to_a_tab(
    server_source: type[_FakeProducer], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cv.CallVideoHub, "_mode", staticmethod(lambda: "auto"))
    hub = cv.get_call_video_hub()
    tab = _FakeSocket()
    hub.register_source(tab)

    feed = await hub.begin("c1")
    assert feed is not None and feed.fmt == "i420"
    assert tab.sent == []  # the tab was not bothered
    await hub.end("c1")

    server_source.unavailable = True
    feed = await hub.begin("c2")
    assert feed is not None and feed.fmt == "jpeg"
    assert tab.sent[0]["type"] == "start"


@pytest.mark.asyncio
async def test_browser_mode_ignores_the_server_renderer(
    server_source: type[_FakeProducer], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cv.CallVideoHub, "_mode", staticmethod(lambda: "browser"))
    hub = cv.get_call_video_hub()
    assert hub.video_possible() is False
    assert await hub.begin("c1") is None
    assert server_source.instances == []


@pytest.mark.asyncio
async def test_a_renderer_that_stops_reports_the_feed_as_lost(
    server_source: type[_FakeProducer],
) -> None:
    hub = cv.get_call_video_hub()
    lost: list[str] = []

    async def _on_lost() -> None:
        lost.append("lost")

    feed = await hub.begin("c1", on_lost=_on_lost)
    assert feed is not None
    producer = server_source.instances[0]
    await producer.on_failed()
    assert lost == ["lost"] and producer.stopped
    assert hub.get_feed("c1") is None


@pytest.mark.asyncio
async def test_the_renderer_is_probed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.avatar_render.renderer as rd
    import core.avatar_render.source as src

    monkeypatch.setattr(cv.CallVideoHub, "_mode", staticmethod(lambda: "server"))
    monkeypatch.setattr(
        src,
        "resolve_model_path",
        lambda configured="": __import__("pathlib").Path("m.vrm"),
    )
    calls: list[int] = []

    def _probe() -> str:
        calls.append(1)
        return "llvmpipe"

    monkeypatch.setattr(rd, "probe", _probe)
    hub = cv.get_call_video_hub()
    await hub.ensure_renderer_probe()
    await hub.ensure_renderer_probe()
    assert calls == [1] and hub.video_possible() is True

    cv.reset_call_video_hub()
    hub = cv.get_call_video_hub()

    def _broken() -> str:
        raise rd.RendererUnavailable("no EGL")

    monkeypatch.setattr(rd, "probe", _broken)
    await hub.ensure_renderer_probe()
    assert hub.video_possible() is False
