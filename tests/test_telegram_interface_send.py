from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

try:
    import interface.telegram_bot as tbot
except Exception:
    pytest.skip(
        "python-telegram-bot not installed; skipping telegram interface send tests",
        allow_module_level=True,
    )


@pytest.mark.asyncio
async def test_send_message_chat_not_found_notifies_corrector(monkeypatch) -> None:
    iface = tbot.TelegramInterface(bot=cast(Any, SimpleNamespace()))
    notify = AsyncMock(return_value=None)

    monkeypatch.setattr(
        tbot,
        "resolve_and_touch",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        tbot,
        "send_with_thread_fallback",
        AsyncMock(side_effect=tbot.BadRequest("Chat not found")),
    )
    monkeypatch.setattr(
        "core.transport_layer.notify_corrector_of_system_message",
        notify,
    )

    await iface.send_message({"text": "hello", "interface_path": "telegram_bot/123"})

    notify.assert_awaited_once()
    assert notify.await_args is not None
    payload_text = notify.await_args.args[0]
    assert "Telegram delivery failed" in payload_text


@pytest.mark.asyncio
async def test_send_message_discards_non_numeric_thread_id(monkeypatch) -> None:
    iface = tbot.TelegramInterface(bot=cast(Any, SimpleNamespace()))

    monkeypatch.setattr(
        tbot,
        "resolve_and_touch",
        AsyncMock(return_value=None),
    )
    sent = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(tbot, "send_with_thread_fallback", sent)

    # A hallucinated placeholder thread id must not be passed to the Telegram
    # API nor persisted as a garbage interface_path segment.
    await iface.send_message(
        {
            "text": "hello",
            "interface_path": "telegram_bot/5208932647/no thread ID indicated in context",
        }
    )

    assert sent.await_count == 1
    assert sent.await_args is not None
    kwargs = sent.await_args.kwargs
    assert kwargs.get("thread_id") is None
