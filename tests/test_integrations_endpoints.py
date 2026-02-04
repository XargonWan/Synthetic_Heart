import json
import pytest

from core.webui import SynthWebUIInterface


@pytest.mark.asyncio
async def test_post_chat_integration_message_calls_message_chain(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)

    async def fake_handle_incoming_message(
        bot, message, text, source="interface", context=None, **kwargs
    ):
        # Simulate successful processing
        return "ACTIONS_EXECUTED"

    monkeypatch.setattr(
        "core.message_chain.handle_incoming_message", fake_handle_incoming_message
    )

    payload = {
        "source": "test_integration",
        "type": "chat",
        "payload": {"text": "Hello from test", "conversation_id": "conv-1"},
    }

    class DummyReq:
        async def json(self):
            return payload

        @property
        def client(self):
            class C:
                host = "127.0.0.1"

            return C()

    res = await webui.post_integration_message(DummyReq())
    assert res.status_code == 200
    body = json.loads(res.body)
    assert body.get("status") == "ok"
    assert body.get("result") == "ACTIONS_EXECUTED"


@pytest.mark.asyncio
async def test_integration_outbox_store_and_retrieve():
    webui = SynthWebUIInterface(autostart=False)

    payload = {
        "source": "test_integration",
        "type": "event",
        "payload": {"text": "Event 1", "target": "unit"},
    }

    class DummyReq:
        async def json(self):
            return payload

    res = await webui.post_integration_message(DummyReq())
    assert res.status_code == 200
    assert json.loads(res.body).get("stored") is True

    class DummyGetReq:
        def __init__(self, params):
            self.query_params = params

    res2 = await webui.get_integration_outbox(
        DummyGetReq({"source": "test_integration"})
    )
    assert res2.status_code == 200
    messages = json.loads(res2.body).get("messages", [])
    assert len(messages) == 1
    assert messages[0]["text"] == "Event 1"

    # Outbox should be cleared now
    res3 = await webui.get_integration_outbox(
        DummyGetReq({"source": "test_integration"})
    )
    assert json.loads(res3.body).get("messages", []) == []
