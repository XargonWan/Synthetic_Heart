import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.animation_handler import AnimationState, KaradaStateServer
from core.karada_ws_transport import WebSocketTransport


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class FakeWebUI:
    def __init__(self, sid):
        self.connections: dict[str, FakeWebSocket] = {sid: FakeWebSocket()}


@pytest.mark.asyncio
async def test_v2_payload_uses_descriptor_id_and_numeric_started_at(tmp_path: Path):
    # Setup fake animation with descriptor
    base = tmp_path / "webui_anim_state"
    (base / "think").mkdir(parents=True)
    anim_f = base / "think" / "think_short.fbx"
    anim_f.write_text("FBX")
    desc = {
        "loop": {"start_frame": 0, "end_frame": 30},
        "expressions": [
            {"start_frame": 0, "end_frame": 10, "targets": {"mouth.O": 0.5}}
        ],
        # note: no lipsync field -> default false
    }
    (base / "think" / "think_short.fbx.json").write_text(json.dumps(desc))

    handler = KaradaStateServer()
    handler.set_animation_search_paths([base])
    handler.register_state_animations("think", {"loop": ["think_short.fbx"]})

    session = "sess1"
    fake = FakeWebUI(session)
    handler.set_webui(cast(Any, fake))
    ws_transport = WebSocketTransport(cast(Any, fake.connections))
    handler.add_transport(ws_transport)

    # Play animation
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
    last = anim_payloads[-1]
    assert last["state"] == "think"
    assert last["descriptor"] == "local/think/think_short"
    assert isinstance(last["started_at"], float)

    current = handler.get_current_animation_state()
    assert current["state"] == "think"
    assert current["descriptor"] == "local/think/think_short"
    assert isinstance(current["started_at"], float)


@pytest.mark.asyncio
async def test_idle_rotation_loop_emits_v2_payload_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    base = tmp_path / "webui_idle_rotation"
    (base / "idle").mkdir(parents=True)

    for name in ("IdleA.fbx", "IdleB.fbx"):
        anim_f = base / "idle" / name
        anim_f.write_text("FBX")
        (base / "idle" / f"{name}.json").write_text(
            json.dumps({"loop": {"start_frame": 0, "end_frame": 30}})
        )

    handler = KaradaStateServer()
    handler.set_animation_search_paths([base])
    handler.register_state_animations(
        "idle", {"loop": ["IdleA.fbx", "IdleB.fbx"]}, sequential=True
    )

    session = "sess1"
    fake = FakeWebUI(session)
    handler.set_webui(cast(Any, fake))
    ws_transport = WebSocketTransport(cast(Any, fake.connections))
    handler.add_transport(ws_transport)

    handler.current_state = AnimationState.IDLE
    handler.current_animation = "IdleA.fbx"
    handler._current_animation_file = "IdleA.fbx"

    sleep_calls = 0

    async def fake_sleep(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr("core.animation_handler.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("core.animation_handler.random.randint", lambda _a, _b: 0)

    await handler._rotation_loop(session, AnimationState.IDLE, None)

    sent = fake.connections[session].sent
    assert any(payload.get("type") == "vrm_animation_v2" for payload in sent)
    assert not any(payload.get("type") == "vrm_animation" for payload in sent)

    payload = [p for p in sent if p.get("type") == "vrm_animation_v2"][-1]
    assert payload["state"] == "idle"
    assert payload["descriptor"] == "local/idle/idleb"
    assert isinstance(payload["started_at"], float)

    current = handler.get_current_animation_state()
    assert current["descriptor"] == payload["descriptor"]
    assert current["started_at"] == payload["started_at"]


def test_animation_manifest_entry_contains_resolver_data(tmp_path: Path):
    base = tmp_path / "webui_structured_manifest"
    (base / "think").mkdir(parents=True)
    anim_f = base / "think" / "Thinking.fbx"
    anim_f.write_text("FBX")
    desc = {
        "fps": 30,
        "intro": {"start_frame": 0, "end_frame": 70},
        "loop": {"start_frame": 71, "end_frame": 140},
        "outro": {"start_frame": 141, "end_frame": 180},
    }
    (base / "think" / "Thinking.fbx.json").write_text(json.dumps(desc))

    handler = KaradaStateServer()
    handler.set_animation_search_paths([base])
    handler.register_state_animations("think", {"loop": ["Thinking.fbx"]})

    entry = handler.get_animation_manifest_entry("think", "Thinking.fbx")

    assert entry is not None
    assert entry["id"] == "local/think/thinking"
    assert entry["animation_file"] == "Thinking.fbx"
    assert entry["animation_url"].endswith("/think/Thinking.fbx")
    assert entry["descriptor_url"].startswith(
        "/api/karada/animations/resolve?descriptor_id="
    )
    assert entry["descriptor_data"] == desc


@pytest.mark.asyncio
async def test_get_animation_manifest_entry_by_id_resolves_current_slice(
    tmp_path: Path,
):
    base = tmp_path / "webui_manifest_lookup"
    (base / "write").mkdir(parents=True)
    anim_f = base / "write" / "Texting.fbx"
    anim_f.write_text("FBX")
    (base / "write" / "Texting.fbx.json").write_text(
        json.dumps({"loop": {"start_frame": 0, "end_frame": 20}})
    )

    handler = KaradaStateServer()
    handler.set_animation_search_paths([base])
    handler.register_state_animations("write", {"loop": ["Texting.fbx"]})

    manifest = handler.get_animation_manifest()
    assert manifest["version"] == 2
    assert "local/write/texting" in manifest["animations"]
    assert (
        handler.get_animation_manifest_entry_by_id("local/write/texting")
        == manifest["animations"]["local/write/texting"]
    )
