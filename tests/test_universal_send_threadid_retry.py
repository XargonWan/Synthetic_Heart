import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.transport_layer import universal_send
from core.message_chain import send_llm_fallback_message


class BadBot:
    def __init__(self):
        self.messages = []

    # note: does NOT accept message_thread_id kwarg
    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_universal_send_retries_without_message_thread_id(monkeypatch):
    bot = BadBot()
    add_message = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context",
        add_message,
    )
    await universal_send(
        bot.send_message, 42, text="fallback", interface_path="fake", thread_id=99
    )
    assert bot.messages == [(42, "fallback")]
    add_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_llm_fallback_with_bot_without_thread_kw(monkeypatch):
    bot = BadBot()
    add_message = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context",
        add_message,
    )
    msg = SimpleNamespace(chat_id=7, thread_id=123, interface_path="fake")
    # Should not raise and should send message
    res = await send_llm_fallback_message(bot, msg, "test reason", context=None)
    assert bot.messages
    assert bot.messages[0][0] == 7
    assert isinstance(res, str)
    add_message.assert_not_awaited()
