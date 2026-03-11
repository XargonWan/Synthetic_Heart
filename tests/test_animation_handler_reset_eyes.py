import pytest
from core.webui import SynthWebUIInterface


class DummyWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_send_animation_includes_reset_eyes(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)
    ws = DummyWS()
    # Put into connections to simulate broadcast
    webui.connections["s"] = ws

    ah = webui.animation_handler

    await ah._send_animation_command(
        session_id="s",
        animation_file="Thinking.fbx",
        loop=True,
        state="think",
        descriptor={"fps": 30},
        play_section=None,
        priority=None,
        source=None,
    )

    assert len(ws.sent) >= 1
    found = any((p.get("reset_eyes") is True) for p in ws.sent)
    assert found, "reset_eyes flag not found in any sent animation payload"
