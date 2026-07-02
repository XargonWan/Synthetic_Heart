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
    (base / "idle").mkdir(parents=True)
    (base / "think" / "think_long.fbx").write_text("FBX")
    (base / "idle" / "idle_loop.fbx").write_text("FBX")
    (base / "think" / "think_long.fbx.json").write_text(
        json.dumps(
            {
                "intro": {"start_frame": 0, "end_frame": 10},
                "loop": {"start_frame": 11, "end_frame": 50},
                "outro": {"start_frame": 51, "end_frame": 70},
            }
        )
    )
    (base / "idle" / "idle_loop.fbx.json").write_text(
        json.dumps({"loop": {"start_frame": 0, "end_frame": 40}})
    )

    handler = KaradaStateServer()
    handler.set_animation_search_paths([base])
    # Force selection of our test animation regardless of active persona/skin content
    handler.register_state_animations("think", {"loop": ["think_long.fbx"]})
    handler.register_state_animations("idle", {"loop": ["idle_loop.fbx"]})

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

    sent = fake.connections[session].sent
    assert any(p.get("type") == "vrm_animation_v2" for p in sent)

    anim_payloads = [p for p in sent if p.get("type") == "vrm_animation_v2"]
    assert anim_payloads
    first = anim_payloads[0]
    assert first["state"] == "think"
    assert first["descriptor"] == "local/think/think_long"
    assert isinstance(first["started_at"], float)

    await handler.stop_animation(context_id="ctx1", session_id=session)

    assert any(
        p.get("type") == "vrm_animation_v2"
        and p.get("state") == "idle"
        and p.get("descriptor") == "local/idle/idle_loop"
        for p in sent
    )
