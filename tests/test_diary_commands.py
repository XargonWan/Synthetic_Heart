import asyncio
import importlib
from types import SimpleNamespace
from typing import cast

import pytest

from core.abstract_context import AbstractContext


def _assert_not_running_on_event_loop(days: int = 2, max_chars=None):
    del days, max_chars
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return []
    raise AssertionError("get_recent_entries ran on the active event loop thread")


@pytest.mark.asyncio
async def test_diary_command_offloads_sync_lookup(monkeypatch):
    from core.command_registry import diary_command

    monkeypatch.setattr("plugins.ai_diary.is_plugin_enabled", lambda: True)
    monkeypatch.setattr(
        "plugins.ai_diary.get_recent_entries", _assert_not_running_on_event_loop
    )
    monkeypatch.setattr(
        "plugins.ai_diary.format_diary_for_injection", lambda entries: str(entries)
    )

    result = await diary_command("3")

    assert "No diary entries found" in result


@pytest.mark.asyncio
async def test_generic_diary_command_offloads_sync_lookup(monkeypatch):
    called = {"reply": None}

    async def fake_reply(text: str):
        called["reply"] = text

    generic_commands = importlib.import_module("core.generic_commands")

    monkeypatch.setattr("plugins.ai_diary.is_plugin_enabled", lambda: True)
    monkeypatch.setattr(
        "plugins.ai_diary.get_recent_entries", _assert_not_running_on_event_loop
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
