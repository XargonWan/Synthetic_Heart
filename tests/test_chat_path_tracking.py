"""Tests for the chat_id -> interface_path map that grillo outreach targeting
relies on (core.recent_chats.set_chat_path / get_chat_path).

The map is populated from chat_context_manager.add_message_to_context so that
recent_chats.get_last_active_chats results can be resolved back to a routable
interface path (grillo_outreach's primary target-selection path).
"""

import pytest
from unittest.mock import AsyncMock, patch

import core.recent_chats as recent_chats


def test_set_chat_path_saves_only_on_change(monkeypatch):
    saves = {"count": 0}

    monkeypatch.setattr(recent_chats, "chat_path_map", {})
    monkeypatch.setattr(
        recent_chats,
        "_save_chat_paths",
        lambda: saves.__setitem__("count", saves["count"] + 1),
    )

    recent_chats.set_chat_path(123, "telegram_bot/123")
    assert recent_chats.get_chat_path(123) == "telegram_bot/123"
    assert saves["count"] == 1

    # Same mapping again -> no redundant disk write
    recent_chats.set_chat_path(123, "telegram_bot/123")
    assert saves["count"] == 1

    # Changed mapping -> persisted
    recent_chats.set_chat_path(123, "telegram_bot/123/456")
    assert recent_chats.get_chat_path("123") == "telegram_bot/123/456"
    assert saves["count"] == 2


@pytest.mark.asyncio
async def test_add_message_to_context_records_chat_path(monkeypatch):
    from core.chat_context_manager import add_message_to_context

    monkeypatch.setattr(recent_chats, "chat_path_map", {})
    monkeypatch.setattr(recent_chats, "_save_chat_paths", lambda: None)

    with (
        patch(
            "plugins.recent_chats.update_chat_activity", AsyncMock(return_value=None)
        ),
        patch(
            "core.chat_history_cache.save_chat_message", AsyncMock(return_value=True)
        ),
    ):
        await add_message_to_context(
            interface_path="telegram_bot/5551234567",
            message_text="hello",
            sender_name="Alice",
            sender_id="5551234567",
        )

    assert recent_chats.get_chat_path("5551234567") == "telegram_bot/5551234567"
