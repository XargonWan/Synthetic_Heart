"""Group replies must be sent without Telegram Markdown parsing.

Telegram consumes ``*action*`` markers as bold markup whenever a parse_mode is
set, and the plain text a peer SyntH instance receives then carries no markers
at all: measured 2026-09-27, a 1,579-character line containing 6 asterisks
arrived at the other instance as 1,573 characters with zero. Each side then
remembered the other's physical actions as plain speech while keeping its own.
Private chats keep Markdown so ``*action*`` still renders as bold there.
"""

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

from interface.message_send_utils import telegram_parse_mode_for


def test_group_ids_get_no_parse_mode() -> None:
    # Group and supergroup ids are negative; that is the structural signal.
    assert telegram_parse_mode_for(-5293915984) is None
    assert telegram_parse_mode_for("-5293915984") is None
    assert telegram_parse_mode_for(-1001234567890) is None


def test_private_ids_keep_markdown() -> None:
    assert telegram_parse_mode_for(5208932647) == "Markdown"
    assert telegram_parse_mode_for("5208932647") == "Markdown"


@pytest.mark.asyncio
async def test_group_reply_is_sent_without_parse_mode(monkeypatch) -> None:
    iface = tbot.TelegramInterface(bot=cast(Any, SimpleNamespace()))
    sent = AsyncMock(return_value=SimpleNamespace(message_id=7))

    monkeypatch.setattr(tbot, "resolve_and_touch", AsyncMock(return_value=None))
    monkeypatch.setattr(tbot, "send_with_thread_fallback", sent)

    await iface.send_message(
        {
            "text": "*I put the plate down*",
            "interface_path": "telegram_bot/-5293915984",
            "skip_history": True,
        }
    )

    sent.assert_awaited_once()
    assert sent.await_args is not None
    assert sent.await_args.kwargs["parse_mode"] is None


@pytest.mark.asyncio
async def test_private_reply_keeps_markdown(monkeypatch) -> None:
    iface = tbot.TelegramInterface(bot=cast(Any, SimpleNamespace()))
    sent = AsyncMock(return_value=SimpleNamespace(message_id=8))

    monkeypatch.setattr(tbot, "resolve_and_touch", AsyncMock(return_value=None))
    monkeypatch.setattr(tbot, "send_with_thread_fallback", sent)

    await iface.send_message(
        {
            "text": "*I put the plate down*",
            "interface_path": "telegram_bot/5208932647",
            "skip_history": True,
        }
    )

    sent.assert_awaited_once()
    assert sent.await_args is not None
    assert sent.await_args.kwargs["parse_mode"] == "Markdown"
