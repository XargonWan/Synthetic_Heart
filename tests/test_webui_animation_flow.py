import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.animation_handler import AnimationState, KaradaStateServer
from core.karada_ws_transport import WebSocketTransport


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


class FakeWebUI:
    def __init__(self, sid: str) -> None:
        self.connections = {sid: FakeWebSocket()}


@pytest.mark.asyncio
async def test_play_and_stop_with_outro(tmp_path: Path):
    # Setup fake animation with outro descriptor
    base = tmp_path / "webui_anim"
    (base / "think").mkdir(parents=True)
    (base / "think" / "think_long.fbx").write_text("FBX")
    (base / "think" / "think_long.fbx.json").write_text(
        json.dumps(
            {
                "intro": {"start_frame": 0, "end_frame": 10},
                "loop": {"start_frame": 11, "end_frame": 50},
                "outro": {"start_frame": 51, "end_frame": 70},
            }
        )
    )

    handler = KaradaStateServer()
    handler.set_animation_search_paths([base])
    # Force selection of our test animation regardless of active persona/skin content
    handler.register_state_animations("think", {"loop": ["think_long.fbx"]})

    session = "sess1"
    fake = FakeWebUI(session)
    handler.set_webui(cast(Any, fake))
    handler.add_transport(WebSocketTransport(cast(Any, fake.connections)))

    # Play animation with context
    await handler.play_animation(
        AnimationState.THINK,
        session_id=session,
        loop=True,
        context_id="ctx1",
        priority=5,
    )
    # Ensure we sent an animation payload (new type: vrm_animation)
    sent = fake.connections[session].sent
    assert any(p.get("type") == "vrm_animation" for p in sent)

    # Ensure rich animation_state is present when descriptor exists
    anim_payloads = [p for p in sent if p.get("type") == "vrm_animation"]
    assert anim_payloads
    first = anim_payloads[0]
    assert first.get("descriptor") is not None
    assert "animation_state" in first
    assert first["animation_state"].get("lipsync") is False

    # Now stop animation and expect an outro play_section event
    await handler.stop_animation(context_id="ctx1", session_id=session)
    # After stop, at least one payload with play_section 'outro' should be in sent
    assert any(p.get("play_section") == "outro" for p in sent)
