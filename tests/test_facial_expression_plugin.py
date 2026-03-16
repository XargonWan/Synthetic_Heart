import asyncio
import pytest

from plugins.facial_expression_plugin import (
    FacialExpressionPlugin,
    FacialExpressionEvent,
)
from core.animation_handler import KaradaStateServer


class DummyKarada(KaradaStateServer):
    def __init__(self):
        super().__init__(webui=None)
        self.sent = []

    def has_connected_clients(self) -> bool:  # always pretend a client is connected
        return True

    async def push_face_expression(self, name, intensity, targets=None):
        self.sent.append((name, intensity))


@pytest.mark.asyncio
async def test_timeline_basic(monkeypatch):
    plugin = FacialExpressionPlugin()
    # patch get_karada_state_server
    dummy = DummyKarada()
    monkeypatch.setattr(
        "plugins.facial_expression_plugin.get_karada_state_server", lambda: dummy
    )
    events = [
        FacialExpressionEvent(position=0, name="smile", intensity=0.5),
        FacialExpressionEvent(position=5, name="sad", intensity=0.2),
    ]
    # total_chars such that first event at 0 sec, second at >0
    await plugin._play_expression_timeline(
        events, total_chars=10, session_id="x", cooldown_s=0.1, chars_per_sec=10
    )
    # after timeline and cooldown, we should see three pushes (two events + clear)
    assert dummy.sent[0] == ("smile", 0.5)
    assert dummy.sent[1] == ("sad", 0.2)
    assert dummy.sent[2] == (None, 0)


@pytest.mark.asyncio
async def test_process_message_text(monkeypatch):
    plugin = FacialExpressionPlugin()
    dummy = DummyKarada()
    monkeypatch.setattr(
        "plugins.facial_expression_plugin.get_karada_state_server", lambda: dummy
    )

    # monkeypatch persona manager to give default settings
    class PM:
        _current_persona = type("X", (), {"name": "Rei"})

        def _load_persona_json(self, name):
            return {
                "facial_expression_cooldown_s": 0.05,
                "facial_expression_chars_per_sec": 1000,
            }

    monkeypatch.setattr(
        "plugins.facial_expression_plugin.get_persona_manager", lambda: PM()
    )

    # Capture background tasks so we can await them deterministically
    created_tasks: list = []
    _real_create_task = asyncio.create_task

    def _capturing_create_task(coro, **kw):
        t = _real_create_task(coro, **kw)
        created_tasks.append(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", _capturing_create_task)

    text = "Hello [em_grin:0.9] world"
    clean = await plugin.process_message_text(text, session_id="s")
    assert clean == "Hello  world"
    # await all background tasks to completion (avoids timing-dependent sleep)
    if created_tasks:
        await asyncio.gather(*created_tasks)
    assert ("grin", 0.9) in dummy.sent
