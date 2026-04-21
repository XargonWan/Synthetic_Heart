import json
import pytest
from fastapi.websockets import WebSocketDisconnect

from core.webui import SynthWebUIInterface
from core.animation_handler import AnimationState


@pytest.mark.asyncio
async def test_post_animation_state_calls_play_animation(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)

    called = {}

    async def fake_play_animation(
        self, state, session_id, loop=True, context_id=None, priority=None, source=None
    ):
        called["state"] = state
        called["session_id"] = session_id
        called["loop"] = loop
        called["context_id"] = context_id
        called["priority"] = priority
        called["source"] = source

    webui.animation_handler = type("H", (), {"play_animation": fake_play_animation})()

    class DummyReq:
        async def json(self):
            return {
                "state": "think",
                "session_id": "s1",
                "loop": False,
                "context_id": "ctx",
                "priority": 9,
                "source": "test",
            }

    res = await webui.set_animation_state(DummyReq())
    assert res.status_code == 200
    body = json.loads(res.body)
    assert body["status"] == "ok"
    assert called["state"] == AnimationState.THINK
    assert called["session_id"] == "s1"
    assert called["loop"] is False
    assert called["context_id"] == "ctx"
    assert called["priority"] == 9
    assert called["source"] == "test"


@pytest.mark.asyncio
async def test_post_touch_animation_state_uses_overlay_defaults():
    webui = SynthWebUIInterface(autostart=False)

    called = {}

    async def fake_play_animation(
        self, state, session_id, loop=True, context_id=None, priority=None, source=None
    ):
        called["state"] = state
        called["session_id"] = session_id
        called["loop"] = loop
        called["context_id"] = context_id
        called["priority"] = priority
        called["source"] = source

    webui.animation_handler = type("H", (), {"play_animation": fake_play_animation})()

    class DummyReq:
        async def json(self):
            return {"state": "touch"}

    res = await webui.set_animation_state(DummyReq())
    assert res.status_code == 200
    assert called["state"] == AnimationState.TOUCH
    assert called["session_id"] is None
    assert called["loop"] is False
    assert called["context_id"] == "__webui_touch_overlay"
    assert called["priority"] == 11
    assert called["source"] == "webui.touch"


@pytest.mark.asyncio
async def test_websocket_touch_message_triggers_authoritative_touch_animation():
    webui = SynthWebUIInterface(autostart=False)
    webui.persona_manager = None

    called = {}

    async def fake_play_animation(
        self, state, session_id, loop=True, context_id=None, priority=None, source=None
    ):
        called["state"] = state
        called["session_id"] = session_id
        called["loop"] = loop
        called["context_id"] = context_id
        called["priority"] = priority
        called["source"] = source

    async def fake_get_full_state(self):
        return {}

    async def fake_ensure_idle_preloaded(self):
        return None

    webui.animation_handler = type(
        "H",
        (),
        {
            "play_animation": fake_play_animation,
            "get_full_state": fake_get_full_state,
            "ensure_idle_preloaded": fake_ensure_idle_preloaded,
        },
    )()

    class TouchWS:
        def __init__(self):
            self.sent = []
            self.client = None
            self._messages = [
                json.dumps({"type": "hello", "client_type": "webui"}),
                json.dumps({"type": "touch", "part": "Head", "mapped_part": "head"}),
            ]

        async def accept(self):
            pass

        async def send_json(self, payload):
            self.sent.append(payload)

        async def receive_text(self):
            if self._messages:
                return self._messages.pop(0)
            raise WebSocketDisconnect()

    ws = TouchWS()
    await webui.websocket_endpoint(ws)  # type: ignore[arg-type]

    assert called["state"] == AnimationState.TOUCH
    assert called["session_id"] is None
    assert called["loop"] is False
    assert called["context_id"] == "__webui_touch_overlay"
    assert called["priority"] == 11
    assert called["source"] == "webui.touch"


@pytest.mark.asyncio
async def test_get_animation_state_route_returns_payload():
    webui = SynthWebUIInterface(autostart=False)

    # Provide a fake animation handler with deterministic current state
    def fake_current_state():
        return {
            "state": "idle",
            "animation_file": None,
            "descriptor": None,
            "play_section": "loop",
            "frame_range": {"start_frame": 0, "end_frame": 30},
            "phase_authoritative": False,
            "animation_state": {"phase": "loop", "phase_authoritative": False},
        }

    webui.animation_handler = type(
        "H", (), {"get_current_animation_state": staticmethod(fake_current_state)}
    )()

    # Find the registered route endpoint for /api/animation_state
    handler = None
    for route in webui.app.routes:
        if getattr(route, "path", None) == "/api/animation_state":
            handler = route.endpoint
            break

    assert handler is not None
    res = await handler(None)
    assert res.status_code == 200
    body = json.loads(res.body)
    assert body.get("state") == "idle"
    assert "animation" in body and "descriptor" in body
    assert body.get("play_section") == "loop"
    assert body.get("frame_range") == {"start_frame": 0, "end_frame": 30}
    assert body.get("phase_authoritative") is False
    assert body.get("animation_state") == {
        "phase": "loop",
        "phase_authoritative": False,
    }
