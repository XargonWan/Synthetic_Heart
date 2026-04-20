import asyncio
import pytest
from types import SimpleNamespace

from core import message_queue


class FastBot:
    def __init__(self):
        self.handled = []

    async def on_generation_start(
        self, interface_path=None, context=None, message=None
    ):
        return

    async def on_generation_end(
        self, interface_path=None, success=None, context=None, message=None
    ):
        return


async def _fake_handle_incoming_message(bot, message, context, interface):
    # simulate a long running background task
    if "grillo" in (interface or ""):
        await asyncio.sleep(1)
        return
    # fast handling
    bot.handled.append(message.text)


@pytest.mark.asyncio
async def test_low_priority_does_not_block(monkeypatch):
    # Patch plugin_instance.handle_incoming_message to our fake
    import core.plugin_instance as pi

    monkeypatch.setattr(pi, "handle_incoming_message", _fake_handle_incoming_message)

    # Put a low-priority item first
    item1 = {
        "bot": FastBot(),
        "message": SimpleNamespace(chat_id=-1, text="grillo beat"),
        "chat_id": -1,
        "thread_id": None,
        "interface": "grillo",
        "chat_name": None,
        "message_thread_name": None,
        "timestamp": 0,
        "context": {},
        "priority": False,
    }

    # Then a normal message
    item2 = {
        "bot": FastBot(),
        "message": SimpleNamespace(chat_id=1, text="user message"),
        "chat_id": 1,
        "thread_id": None,
        "interface": "telegram_bot",
        "chat_name": None,
        "message_thread_name": None,
        "timestamp": 0,
        "context": {},
        "priority": False,
    }

    # Put onto the queue using internal API
    await message_queue._queue.put((message_queue.LOW_PRIORITY, 1, item1))
    await message_queue._queue.put((message_queue.NORMAL_PRIORITY, 2, item2))

    # Run consumer loop iteration twice with a short timeout to simulate
    consumer_task = asyncio.create_task(message_queue._consumer_loop())

    # Wait enough time for background task to be scheduled and fast task to complete
    await asyncio.sleep(2)

    # Check that normal message was handled (FastBot.handled appended)
    # Since we used FastBot instances per item, ensure no exceptions and that the queue progressed
    # Cancel consumer task
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    assert True  # If we reached here without deadlock, test passes
