import asyncio
from types import SimpleNamespace

import pytest

import core.plugin_instance as pi


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text=None, reply_to_message_id=None):
        self.sent.append((chat_id, text, reply_to_message_id))


@pytest.mark.asyncio
async def test_fallback_ack_sent_when_no_actions(monkeypatch):
    fake_bot = FakeBot()

    # Fake plugin to return a bare string result (no actions)
    class FakePlugin:
        async def handle_incoming_message(self, bot, message, prompt):
            return "(internal notes only)"

    monkeypatch.setattr(pi, "plugin", FakePlugin())

    class Msg:
        chat_id = 123
        message_id = 1
        from_user = SimpleNamespace(id=42)
        text = "hello"

    # Call handler
    res = await pi.handle_incoming_message(fake_bot, Msg(), {})

    # Ensure fallback ACK was sent
    assert fake_bot.sent, "Fallback ACK was not sent"
    chat_id, text, reply_to = fake_bot.sent[0]
    assert chat_id == 123
    assert "Ricevuto" in text
