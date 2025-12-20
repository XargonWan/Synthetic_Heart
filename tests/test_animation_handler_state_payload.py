import json
from pathlib import Path

import pytest

from core.animation_handler import get_animation_handler, AnimationState


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class FakeWebUI:
    def __init__(self, sid):
        self.connections = {sid: FakeWebSocket()}


@pytest.mark.asyncio
async def test_animation_state_included_and_lipsync_default(tmp_path: Path, monkeypatch):
    # Setup fake animation with descriptor
    base = tmp_path / "webui_anim_state"
    (base / "think").mkdir(parents=True)
    anim_f = (base / "think" / "think_short.fbx")
    anim_f.write_text("FBX")
    desc = {
        "loop": {"start_frame": 0, "end_frame": 30},
        "expressions": [{"start_frame": 0, "end_frame": 10, "targets": {"mouth.O": 0.5}}]
        # note: no lipsync field -> default false
    }
    (base / "think" / "think_short.fbx.json").write_text(json.dumps(desc))

    handler = get_animation_handler()
    handler.set_animation_search_paths([base])
    handler.register_state_animations('think', {'loop': ['think_short.fbx']})

    session = "sess1"
    fake = FakeWebUI(session)
    handler.set_webui(fake)

    # Monkeypatch EmotionManager to return predictable emotions
    async def fake_get_emotion_state(self, include_raw: bool = False):
        return {"happy": 7.5, "calm": 5.2}

    monkeypatch.setattr('plugins.emotion_manager.EmotionManager.get_emotion_state', fake_get_emotion_state, raising=False)

    # Play animation
    await handler.play_animation(AnimationState.THINK, session_id=session, loop=True, context_id="ctx1", priority=5)

    # Ensure we sent an animation payload with animation_state
    sent = fake.connections[session].sent
    assert any(p.get('type') == 'animation' for p in sent)

    # Find last animation payload
    anim_payloads = [p for p in sent if p.get('type') == 'animation']
    assert anim_payloads
    last = anim_payloads[-1]

    assert 'animation_state' in last
    st = last['animation_state']
    # lipsync default false (not defined in descriptor)
    assert st.get('lipsync') is False
    # emotions should be present and formatted
    assert isinstance(st.get('emotions'), dict)
    assert st['emotions']['dominant'] == 'happy'
    assert 'happy' in st['emotions']['values']

    # Clip/timing should be present when a descriptor exists
    assert isinstance(st.get('timing'), dict)
    assert st['timing'].get('started_at')
    assert isinstance(st.get('clip'), dict)
    assert st['clip'].get('fps')
