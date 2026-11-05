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
    record_failure = AsyncMock(return_value=None)

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
    # Keep the test hermetic: the delivery-failure recorder writes to the DB.
    monkeypatch.setattr(tbot, "_record_telegram_delivery_failure", record_failure)

    await iface.send_message({"text": "hello", "interface_path": "telegram_bot/123"})

    notify.assert_awaited_once()
    assert notify.await_args is not None
    payload_text = notify.await_args.args[0]
    assert "Telegram delivery failed" in payload_text
    # A rejected send must leave a failure record, not just a container log line.
    record_failure.assert_awaited_once()
    assert "Chat not found" in record_failure.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_send_message_records_failure_on_transport_error(monkeypatch) -> None:
    """A non-BadRequest exception loses the text just as silently as a rejection:
    it must be recorded, then re-raised for the dispatcher."""
    iface = tbot.TelegramInterface(bot=cast(Any, SimpleNamespace()))
    record_failure = AsyncMock(return_value=None)

    monkeypatch.setattr(tbot, "resolve_and_touch", AsyncMock(return_value=None))

    async def boom(*args, **kwargs):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(tbot, "send_with_thread_fallback", boom)
    monkeypatch.setattr(tbot, "_record_telegram_delivery_failure", record_failure)

    with pytest.raises(RuntimeError):
        await iface.send_message(
            {"text": "hello", "interface_path": "telegram_bot/5208932647"}
        )

    record_failure.assert_awaited_once()
    assert "connection reset" in record_failure.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_send_message_ignores_reply_to_holding_the_chat_id(monkeypatch) -> None:
    """The model fills ``reply_to`` with the chat id (live incident 2026-09-17).
    Telegram refuses to quote a message that does not exist and the reply was
    lost; the obviously-wrong value must be dropped and the send must still go
    out, replying to the incoming message instead."""
    iface = tbot.TelegramInterface(bot=cast(Any, SimpleNamespace()))

    monkeypatch.setattr(tbot, "resolve_and_touch", AsyncMock(return_value=None))
    sent = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(tbot, "send_with_thread_fallback", sent)

    await iface.send_message(
        {
            "text": "hello",
            "interface_path": "telegram_bot/5208932647",
            "reply_to": "5208932647",
        },
        original_message=SimpleNamespace(chat_id="5208932647", message_id=4350),
    )

    assert sent.await_count == 1
    kwargs = sent.await_args.kwargs
    # Not the chat id ... the incoming message's own id (the automatic reply).
    assert kwargs.get("reply_to_message_id") == 4350


@pytest.mark.asyncio
async def test_send_message_keeps_a_valid_reply_to(monkeypatch) -> None:
    """A genuine message id must still be honoured."""
    iface = tbot.TelegramInterface(bot=cast(Any, SimpleNamespace()))

    monkeypatch.setattr(tbot, "resolve_and_touch", AsyncMock(return_value=None))
    sent = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(tbot, "send_with_thread_fallback", sent)

    await iface.send_message(
        {
            "text": "hello",
            "interface_path": "telegram_bot/5208932647",
            "reply_to": 4349,
        }
    )

    assert sent.await_args.kwargs.get("reply_to_message_id") == 4349


@pytest.mark.asyncio
async def test_send_message_survives_a_non_numeric_reply_to(monkeypatch) -> None:
    """A non-numeric reply_to (a placeholder or a path) must never leave
    ``reply_message_id`` unbound — that used to crash the send with a NameError
    and lose the message."""
    iface = tbot.TelegramInterface(bot=cast(Any, SimpleNamespace()))

    monkeypatch.setattr(tbot, "resolve_and_touch", AsyncMock(return_value=None))
    sent = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(tbot, "send_with_thread_fallback", sent)

    await iface.send_message(
        {
            "text": "hello",
            "interface_path": "telegram_bot/5208932647",
            "reply_to": "dm_message_id_placeholder",
        }
    )

    assert sent.await_count == 1
    assert sent.await_args.kwargs.get("reply_to_message_id") is None


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
