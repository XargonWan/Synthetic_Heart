from unittest.mock import AsyncMock
from typing import Any, cast

from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


def create_client():
    ui = SynthWebUIInterface(autostart=False)
    return TestClient(ui.app)


def test_set_animation_state_calls_handler():
    webui = SynthWebUIInterface(autostart=False)
    handler = cast(Any, type("AH", (), {})())
    handler.play_animation = AsyncMock()
    webui.animation_handler = handler

    test_client = TestClient(webui.app)
    r = test_client.post(
        "/api/animation_state",
        json={"state": "think", "session_id": "s123", "loop": True},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"
    # Ensure handler was called
    handler.play_animation.assert_awaited()


def test_get_animation_state_returns_minimal_v2_tuple():
    webui = SynthWebUIInterface(autostart=False)
    webui.animation_handler = type(
        "AH",
        (),
        {
            "get_current_animation_state": staticmethod(
                lambda: {
                    "state": "think",
                    "descriptor": "rei/think/thinking",
                    "started_at": 1712345678.123,
                }
            )
        },
    )()

    test_client = TestClient(webui.app)
    r = test_client.get("/api/animation_state")

    assert r.status_code == 200
    assert r.json() == {
        "state": "think",
        "descriptor": "rei/think/thinking",
        "started_at": 1712345678.123,
    }
