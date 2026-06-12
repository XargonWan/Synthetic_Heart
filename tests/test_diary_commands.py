import asyncio
import importlib
from types import SimpleNamespace
from typing import cast

import pytest

from core.abstract_context import AbstractContext


async def _mock_get_recent_entries_async(days: int = 2, max_chars=None):
    # Verify we are on the running event loop
    asyncio.get_running_loop()
    return []


@pytest.mark.asyncio
async def test_diary_command_uses_async_lookup(monkeypatch):
    from core.command_registry import diary_command

    monkeypatch.setattr("plugins.ai_diary.is_plugin_enabled", lambda: True)
    monkeypatch.setattr(
        "plugins.ai_diary.get_recent_entries_async", _mock_get_recent_entries_async
    )
    monkeypatch.setattr(
        "plugins.ai_diary.format_diary_for_injection", lambda entries: str(entries)
    )

    result = await diary_command("3")

    assert "No diary entries found" in result


@pytest.mark.asyncio
async def test_generic_diary_command_uses_async_lookup(monkeypatch):
    called = {"reply": None}

    async def fake_reply(text: str):
        called["reply"] = text

    generic_commands = importlib.import_module("core.generic_commands")

    monkeypatch.setattr("plugins.ai_diary.is_plugin_enabled", lambda: True)
    monkeypatch.setattr(
        "plugins.ai_diary.get_recent_entries_async", _mock_get_recent_entries_async
    )
    monkeypatch.setattr(
        "plugins.ai_diary.format_diary_for_injection", lambda entries: str(entries)
    )

    context = cast(AbstractContext, SimpleNamespace(is_trainer=lambda: True))
    await generic_commands.generic_diary_command(context, fake_reply, args="3")

    assert called["reply"] is not None
    assert "No diary entries found" in called["reply"]


def test_generic_commands_module_imports():
    module = importlib.import_module("core.generic_commands")
    assert hasattr(module, "generic_last_chats_command")
