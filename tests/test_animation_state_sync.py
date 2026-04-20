import json
import pytest

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
        called["source"] = source

    webui.animation_handler = type("H", (), {"play_animation": fake_play_animation})()

    class DummyReq:
        async def json(self):
            return {
                "state": "think",
                "session_id": "s1",
                "loop": False,
                "context_id": "ctx",
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
    assert called["source"] == "test"


@pytest.mark.asyncio
async def test_get_animation_state_route_returns_payload():
    webui = SynthWebUIInterface(autostart=False)

    # Provide a fake animation handler with deterministic current state
    def fake_current_state():
        return {"state": "idle", "animation_file": None, "descriptor": None}

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
