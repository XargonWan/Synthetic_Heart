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
        tbot.chat_link_store,
        "update_names_from_resolver",
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
