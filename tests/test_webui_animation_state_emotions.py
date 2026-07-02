from datetime import datetime, timezone, timedelta
from typing import Any, cast

import pytest

from core.animation_handler import AnimationState
from core.webui import SynthWebUIInterface


class DummyWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class DummyEmotionManager:
    def get_emotion_state(self):
        # Return a simple dict (synchronous) with one strong emotion
        return {"happy": 0.8, "calm": 0.05}


@pytest.mark.asyncio
async def test_broadcast_animation_state_summary_includes_emotions(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)

    # Patch the plugins.emotion_manager.EmotionManager to our dummy
    monkeypatch.setitem(
        __import__("sys").modules,
        "plugins.emotion_manager",
        type("m", (), {"EmotionManager": DummyEmotionManager}),
    )

    # Attach a fake websocket
    ws = DummyWebSocket()
    webui.connections["sid"] = cast(Any, ws)

    # Set current animation in the animation handler
    ah = cast(Any, webui.animation_handler)
    ah.current_state = AnimationState.THINK
    ah._current_animation_file = "Thinking.fbx"
    ah._current_animation_descriptor = {
        "fps": 30,
        "loop": {"start_frame": 0, "end_frame": 30},
    }
    ah._current_animation_started_at = datetime.now(timezone.utc) - timedelta(
        seconds=1
    )

    await webui._broadcast_animation_state_summary(
        AnimationState.THINK, "Thinking.fbx", ah._current_animation_descriptor
    )

    assert len(ws.sent) == 1
    payload = ws.sent[0]
    assert payload.get("type") == "animation_state"
    assert payload.get("state") == "think"
    assert payload.get("descriptor") == "rei/think/thinking"
    assert isinstance(payload.get("started_at"), float)
    assert "animation_state" in payload
    anim_state = payload["animation_state"]
    assert anim_state.get("descriptor") == "rei/think/thinking"
    assert isinstance(anim_state.get("started_at"), float)
    assert isinstance(anim_state.get("emotions"), dict)
    assert anim_state["emotions"].get("dominant") == "happy"
    assert "happy" in anim_state["emotions"].get("values", {})
