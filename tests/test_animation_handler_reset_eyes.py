import asyncio
from core.webui import SynthWebUIInterface


class DummyWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def test_send_animation_includes_reset_eyes(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)
    ws = DummyWS()
    # Put into connections to simulate broadcast
    webui.connections["s"] = ws

    ah = webui.animation_handler
    # Call the internal send command (broadcast path)

    async def run():
        await ah._send_animation_command(
            session_id=None,
            animation_file="Thinking.fbx",
            loop=True,
            state="think",
            descriptor={"fps": 30},
            play_section=None,
            priority=None,
            source=None,
        )

    asyncio.get_event_loop().run_until_complete(run())

    assert len(ws.sent) >= 1
    found = any((p.get("reset_eyes") is True) for p in ws.sent)
    assert found, "reset_eyes flag not found in any sent animation payload"
