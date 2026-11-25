"""Chat commands and wake/sleep triggers work from the message, for any interface."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from core import chat_attention, command_registry
from core.extensions.core_interfaces import CoreMessaging


@pytest.fixture(autouse=True)
def clean_state() -> Iterator[None]:
    chat_attention.CHAT_ATTENTION_STATE.clear()
    yield
    chat_attention.CHAT_ATTENTION_STATE.clear()


def context(
    interface_id: str = "chat", chat_id: str = "-100", thread: str | None = "7"
) -> dict[str, Any]:
    wrapped = SimpleNamespace(
        chat_id=chat_id,
        thread_id=thread,
        interface_path=f"{interface_id}/{chat_id}" + (f"/{thread}" if thread else ""),
    )
    return {"interface_id": interface_id, "wrapped": wrapped, "bot": object()}


class TestCommandsFromTheMessage:
    @pytest.mark.asyncio
    async def test_sleep_wake_and_status_use_the_chat_the_message_came_from(
        self,
    ) -> None:
        ctx = context()
        assert "Asleep" in await command_registry.sleep_command(ctx)
        assert chat_attention.get_attention("-100", True) is False
        assert "Asleep" in await command_registry.status_command(ctx)
        assert "Awake" in await command_registry.wake_command(ctx)
        assert chat_attention.get_attention("-100", False) is True
        assert "Awake" in await command_registry.status_command(ctx)

    @pytest.mark.asyncio
    async def test_another_chat_is_not_affected(self) -> None:
        await command_registry.sleep_command(context(chat_id="-100"))
        assert chat_attention.get_attention("-200", True) is True

    @pytest.mark.asyncio
    async def test_the_path_command_reports_the_messages_own_path(self) -> None:
        text = await command_registry.get_interface_path_command(
            context("chat", "5", "9")
        )
        assert "`chat/5/9`" in text

    @pytest.mark.asyncio
    async def test_without_any_chat_the_commands_say_so_and_change_nothing(
        self,
    ) -> None:
        assert (
            await command_registry.sleep_command({"interface_id": "chat"})
            == "💤 Asleep."
        )
        assert await command_registry.wake_command(None) == "👀 Awake."
        assert chat_attention.CHAT_ATTENTION_STATE == {}

    @pytest.mark.asyncio
    async def test_a_platform_update_object_is_read_the_same_way(self) -> None:
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-55),
            effective_message=SimpleNamespace(message_thread_id=3),
        )
        ctx = {"interface_id": "chat", "update": update}
        await command_registry.sleep_command(ctx)
        assert chat_attention.get_attention(-55, True) is False
        assert "`chat/-55/3`" in await command_registry.get_interface_path_command(ctx)


class TestTextTriggers:
    @pytest.fixture(autouse=True)
    def triggers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            chat_attention, "get_sleep_triggers", lambda: ["good night"]
        )
        monkeypatch.setattr(chat_attention, "get_wake_triggers", lambda: ["wake up"])

    def test_a_sleep_trigger_puts_an_awake_chat_to_sleep_once(self) -> None:
        assert chat_attention.apply_text_triggers("c", "Good night, all") == "sleep"
        assert chat_attention.get_attention("c", True) is False
        assert chat_attention.apply_text_triggers("c", "good night again") is None

    def test_a_wake_trigger_or_a_mention_wakes_a_sleeping_chat(self) -> None:
        chat_attention.set_attention("c", False)
        assert chat_attention.apply_text_triggers("c", "please wake up") == "wake"
        chat_attention.set_attention("c", False)
        assert (
            chat_attention.apply_text_triggers("c", "hey @SynthBot", "synthbot")
            == "wake"
        )
        assert chat_attention.get_attention("c", False) is True

    def test_nothing_happens_without_a_trigger_or_to_an_awake_chat(self) -> None:
        assert chat_attention.apply_text_triggers("c", "hello") is None
        assert chat_attention.apply_text_triggers("c", "wake up") is None
        assert chat_attention.apply_text_triggers(None, "good night") is None
        assert chat_attention.apply_text_triggers("c", "") is None
        assert chat_attention.CHAT_ATTENTION_STATE == {}

    def test_sleep_wins_when_both_match(self) -> None:
        assert chat_attention.apply_text_triggers("c", "wake up, good night") == "sleep"


class TestEnqueueAppliesTriggers:
    @pytest.mark.asyncio
    async def test_a_remote_message_with_a_trigger_changes_the_chat_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core import message_queue

        queued: list[Any] = []

        async def fake_enqueue(bot: Any, message: Any, **kw: Any) -> None:
            queued.append(message)

        monkeypatch.setattr(message_queue, "enqueue", fake_enqueue)
        monkeypatch.setattr(
            chat_attention, "get_sleep_triggers", lambda: ["good night"]
        )

        class Bot:
            async def get_me(self) -> Any:
                return SimpleNamespace(username="synthbot")

        message = SimpleNamespace(chat_id="42", text="good night")
        await CoreMessaging().enqueue(Bot(), message, "chat", False)
        assert chat_attention.get_attention("42", True) is False
        assert queued == [message]  # the goodbye is still processed
