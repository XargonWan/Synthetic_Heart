from fastapi.testclient import TestClient
from core.webui import SynthWebUIInterface
from unittest.mock import AsyncMock


def create_client():
    ui = SynthWebUIInterface(autostart=False)
    return TestClient(ui.app)


def test_set_animation_state_calls_handler():
    client = create_client()
    # Patch the server's animation_handler to ensure play_animation is called
    ui = (
        client.app.state._app
    )  # FastAPI TestClient wiring: access underlying app via client
    # Above approach may not show our instance; instead create another instance directly
    webui = SynthWebUIInterface(autostart=False)
    webui.animation_handler = type("AH", (), {})()
    webui.animation_handler.play_animation = AsyncMock()

    test_client = TestClient(webui.app)
    r = test_client.post(
        "/api/animation_state",
        json={"state": "think", "session_id": "s123", "loop": True},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"
    # Ensure handler was called
    webui.animation_handler.play_animation.assert_awaited()
