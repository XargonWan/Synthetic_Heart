"""Tests for the WebUI call provider and /api/call API (``core.webui_call``)."""

from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.call_ack_cache as cac
import core.call_manager as cm
import core.webui_call as wc
from core.call_manager import CallKind, get_call_manager


@pytest.fixture(autouse=True)
def _signal_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import core.media_url_utils as mu

    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: tmp_path / "tts")


@pytest.fixture(autouse=True)
def _no_ack_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _prepare(self: Any, languages: Any = None) -> dict[str, Path]:
        return {}

    monkeypatch.setattr(cac.CallAckCache, "prepare", _prepare)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    wc.mount_call_api(app)
    return TestClient(app)


def _write_wav(path: Path, seconds: float = 0.5, rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(buf, format="JPEG")
    return buf.getvalue()


def test_start_status_end_roundtrip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = client.post("/api/call/start", json={"session_id": "s1", "kind": "video"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "video" and body["degraded"] is False

    status = client.get("/api/call/status", params={"session_id": "s1"}).json()
    assert status["call"]["call_id"] == body["call_id"]
    assert client.get("/api/call/status", params={"session_id": "s2"}).json() == {
        "call": None
    }

    assert body["ack_ready"] is True and body["last_ack"] == ""
    assert client.post("/api/call/utterance_end", json={}).status_code in (404, 405)

    assert (
        client.post("/api/call/end", json={"call_id": body["call_id"]}).status_code
        == 200
    )
    assert (
        client.post("/api/call/end", json={"call_id": body["call_id"]}).status_code
        == 404
    )
    assert client.get("/api/call/status", params={"session_id": "s1"}).json() == {
        "call": None
    }


def test_start_rejects_unknown_kind_and_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        client.post(
            "/api/call/start", json={"session_id": "s1", "kind": "hologram"}
        ).status_code
        == 400
    )
    monkeypatch.setattr(get_call_manager(), "is_enabled", lambda: False)
    assert client.post("/api/call/start", json={"session_id": "s1"}).status_code == 503


@pytest.mark.asyncio
async def test_provider_plays_ack_as_a_sound_effect_not_as_speech(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    played: list[tuple[str, float | None]] = []
    spoken: list[str] = []

    class _FakeKarada:
        async def broadcast_sfx(
            self, audio_path: str, audio_duration_s: float | None = None
        ) -> None:
            played.append((audio_path, audio_duration_s))

        async def broadcast_audio(self, audio_path: str, **_: Any) -> None:
            spoken.append(audio_path)  # would lip-sync and play TALK

        def get_current_audio(self) -> None:
            return None

    import core.animation_handler as ah

    monkeypatch.setattr(ah, "get_karada_state_server", lambda: _FakeKarada())

    mgr = get_call_manager()
    mgr.register_provider(wc.WebUICallProvider())
    session = await mgr.request_call(wc.WEBUI_INTERFACE_NAME, wc.webui_call_path("s1"))
    # Starting the call already played the pickup sound, as an sfx too.
    assert [Path(p).name for p, _ in played] == ["pickup-v1.wav"]
    played.clear()
    clip = _write_wav(tmp_path / "chime.wav", seconds=0.5)
    cac.get_call_ack_cache()._path = clip

    assert await mgr.on_message_transcribed(session.call_id) is True
    assert played and played[0][0] == str(clip)
    assert played[0][1] == pytest.approx(0.5, abs=0.01)
    assert spoken == []  # the avatar does not talk for the signal


@pytest.mark.asyncio
async def test_notify_transcribed_plays_the_signal_once_per_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []

    async def _heard(
        self: Any, call_id: str | None, language: str | None = None
    ) -> bool:
        calls.append(call_id)
        return True

    monkeypatch.setattr(cm.CallManager, "on_message_transcribed", _heard)

    wc.notify_transcribed(None)
    wc.notify_transcribed("c1")
    await asyncio.sleep(0)
    await asyncio.gather(*list(wc._background))

    assert calls == ["c1"]


@pytest.mark.asyncio
async def test_call_is_bound_to_its_session() -> None:
    mgr = get_call_manager()
    mgr.register_provider(wc.WebUICallProvider())
    session = await mgr.request_call(wc.WEBUI_INTERFACE_NAME, wc.webui_call_path("s1"))

    assert wc.resolve_call_for_session(session.call_id, "s1") is session
    # Another WebUI session cannot piggy-back on this call id.
    assert wc.resolve_call_for_session(session.call_id, "s2") is None
    assert wc.resolve_call_for_session(None, "s1") is None

    await wc.end_calls_for_session("s1")
    assert mgr.get_active_call(wc.webui_call_path("s1")) is None


def test_screen_websocket_feeds_frames(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cm.config_registry,
        "get_value",
        lambda key, default=None, **_: (
            0.0 if key == "CALL_SCREEN_FRAME_MIN_INTERVAL_SEC" else default
        ),
    )
    call_id = client.post("/api/call/start", json={"session_id": "s1"}).json()[
        "call_id"
    ]

    with client.websocket_connect(f"/api/call/screen?call_id={call_id}") as ws:
        ws.send_bytes(_jpeg())

    session = get_call_manager().get_session(call_id)
    assert session is not None and session.latest_screen_frame

    with pytest.raises(Exception):
        with client.websocket_connect("/api/call/screen?call_id=nope"):
            pass


@pytest.mark.asyncio
async def test_webui_video_call_is_supported_natively() -> None:
    mgr = get_call_manager()
    mgr.register_provider(wc.WebUICallProvider())
    session = await mgr.request_call(
        wc.WEBUI_INTERFACE_NAME, wc.webui_call_path("s1"), kind=CallKind.VIDEO
    )
    assert session.kind is CallKind.VIDEO and not session.degraded


def test_start_reports_readiness_warnings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import core.core_initializer as ci

    # Everything off: both Auris and Vox are named in the warnings.
    monkeypatch.setattr(ci, "PLUGIN_REGISTRY", {})
    warnings = client.post("/api/call/start", json={"session_id": "s1"}).json()[
        "warnings"
    ]
    assert len(warnings) == 2
    assert "Auris" in warnings[0] and "Vox" in warnings[1]

    # Both usable: no warnings.
    monkeypatch.setattr(
        ci,
        "PLUGIN_REGISTRY",
        {
            "auris_plugin": SimpleNamespace(_active_engine_name="whisper"),
            "vox_plugin": SimpleNamespace(_active_engine_name="kitten"),
        },
    )
    assert wc.readiness_warnings() == []


@pytest.mark.asyncio
async def test_webui_provider_knows_when_the_synth_is_speaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.animation_handler as ah

    state = {"audio": None}

    class _Karada:
        def get_current_audio(self) -> Any:
            return state["audio"]

    monkeypatch.setattr(ah, "get_karada_state_server", lambda: _Karada())
    provider = wc.WebUICallProvider()
    session = None  # the check does not depend on the session
    assert provider.is_agent_speaking(session) is False  # type: ignore[arg-type]
    state["audio"] = {"type": "tts-play"}
    assert provider.is_agent_speaking(session) is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_karada_sfx_is_not_speech() -> None:
    """``broadcast_sfx`` sends ``sfx-play`` only: no talk animation, no current audio."""
    from core.animation_handler import KaradaStateServer

    sent: list[dict[str, Any]] = []

    class _Transport:
        async def broadcast_audio(self, payload: dict[str, Any]) -> None:
            sent.append(payload)

    server = KaradaStateServer.__new__(KaradaStateServer)
    server._transports = [_Transport()]  # type: ignore[attr-defined]
    server._derive_audio_url = lambda path: "/media/tts/call_ack/chime-v1.wav"  # type: ignore[method-assign]
    talked: list[Any] = []
    server._start_talk_animation = lambda *a, **k: talked.append(a)  # type: ignore[method-assign]

    await server.broadcast_sfx("/x/chime-v1.wav", audio_duration_s=0.6)

    assert sent == [
        {
            "type": "sfx-play",
            "url": "/media/tts/call_ack/chime-v1.wav",
            "audio_duration_s": 0.6,
        }
    ]
    assert talked == []


class _FakeAuris:
    _active_engine_name = "fake"

    def __init__(self, text: str | None) -> None:
        self._text = text

    async def transcribe_audio(self, path: str, **_: Any) -> Any:
        from types import SimpleNamespace

        return None if self._text is None else SimpleNamespace(text=self._text)


async def _upload(
    monkeypatch: pytest.MonkeyPatch, text: str | None, call_id: str | None
) -> list[str]:
    """Run the real upload endpoint with a fake STT; return the call ids notified."""
    from starlette.datastructures import UploadFile

    import core.core_initializer as ci
    from core.webui import SynthWebUIInterface

    notified: list[str] = []
    monkeypatch.setattr(ci, "PLUGIN_REGISTRY", {"auris_plugin": _FakeAuris(text)})
    monkeypatch.setattr(wc, "notify_transcribed", lambda cid: notified.append(cid))
    upload = UploadFile(file=io.BytesIO(b"audio"), filename="clip.webm")
    await SynthWebUIInterface.audio_upload_endpoint(
        None,
        file=upload,
        engine=None,
        call_id=call_id,  # type: ignore[arg-type]
    )
    return notified


@pytest.mark.asyncio
async def test_signal_only_when_text_was_transcribed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _upload(monkeypatch, "ciao come va", "c1") == ["c1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, "", "   "])
async def test_no_signal_for_an_empty_transcription(
    monkeypatch: pytest.MonkeyPatch, text: str | None
) -> None:
    # Typing, a cough, an echo: nothing transcribed -> no "message received".
    assert await _upload(monkeypatch, text, "c1") == []


@pytest.mark.asyncio
async def test_no_signal_outside_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _upload(monkeypatch, "ciao", None) == []
