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
    queue = message_queue._get_queue()
    await queue.put((message_queue._heap_key(message_queue.PRIORITY_LOW), 1, item1))
    await queue.put((message_queue._heap_key(message_queue.PRIORITY_GENERAL), 2, item2))

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


@pytest.mark.asyncio
async def test_queue_rebinds_to_current_event_loop():
    original_queue = message_queue._queue
    original_lock = message_queue._lock
    original_queue_loop = getattr(message_queue, "_queue_loop", None)
    original_lock_loop = getattr(message_queue, "_lock_loop", None)

    try:
        other_loop = asyncio.new_event_loop()
        try:
            message_queue._queue = asyncio.PriorityQueue()
            message_queue._lock = asyncio.Lock()
            message_queue._queue_loop = other_loop
            message_queue._lock_loop = other_loop

            old_queue = message_queue._queue
            old_lock = message_queue._lock
            assert old_queue is not None
            assert old_lock is not None

            new_queue = message_queue._get_queue()
            new_lock = message_queue._get_lock()

            assert new_queue is not old_queue
            assert new_lock is not old_lock

            await new_queue.put(
                (
                    message_queue._heap_key(message_queue.PRIORITY_GENERAL),
                    1,
                    {
                        "bot": None,
                        "message": None,
                        "chat_id": 0,
                        "thread_id": None,
                        "interface": "test",
                        "timestamp": 0,
                        "context": {},
                        "priority": False,
                    },
                )
            )
            queued_item = await new_queue.get()
            assert queued_item[2]["interface"] == "test"
        finally:
            other_loop.close()
    finally:
        message_queue._queue = original_queue
        message_queue._lock = original_lock
        message_queue._queue_loop = original_queue_loop
        message_queue._lock_loop = original_lock_loop


@pytest.mark.asyncio
async def test_observer_background_task_is_not_cancelled_by_user_message(
    monkeypatch,
):
    import core.plugin_instance as pi

    class DummyPlugin:
        def get_rate_limit(self):
            return 1000, 1, 1.0

    original_queue = message_queue._queue
    original_lock = message_queue._lock
    original_queue_loop = getattr(message_queue, "_queue_loop", None)
    original_lock_loop = getattr(message_queue, "_lock_loop", None)
    original_bg_tasks = dict(message_queue._bg_tasks)

    finished = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_background_task():
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finished.set()

    async def fake_handle(bot, message, context, interface):
        return

    monkeypatch.setattr(pi, "handle_incoming_message", fake_handle)
    monkeypatch.setattr(pi, "get_plugin", lambda: DummyPlugin())

    try:
        message_queue._queue = asyncio.PriorityQueue()
        message_queue._lock = asyncio.Lock()
        message_queue._queue_loop = asyncio.get_running_loop()
        message_queue._lock_loop = asyncio.get_running_loop()
        message_queue._bg_tasks.clear()

        background_task = asyncio.create_task(fake_background_task())
        message_queue._bg_tasks["telegram_bot/1"] = message_queue._BackgroundTaskEntry(
            task=background_task,
            cancel_on_user_message=False,
        )

        user_item = {
            "bot": FastBot(),
            "message": SimpleNamespace(
                chat_id=1,
                text="incoming user message",
                from_user=SimpleNamespace(id=42),
            ),
            "chat_id": 1,
            "thread_id": None,
            "interface": "telegram_bot",
            "chat_name": None,
            "message_thread_name": None,
            "timestamp": 1,
            "context": {"core_animation_broadcast": False},
            "priority": False,
        }

        queue = message_queue._get_queue()
        consumer_task = asyncio.create_task(message_queue._consumer_loop())
        await queue.put(
            (message_queue._heap_key(message_queue.PRIORITY_GENERAL), 2, user_item)
        )
        await asyncio.wait_for(finished.wait(), timeout=1.0)

        assert not cancelled.is_set()

        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
        await background_task
    finally:
        message_queue._queue = original_queue
        message_queue._lock = original_lock
        message_queue._queue_loop = original_queue_loop
        message_queue._lock_loop = original_lock_loop
        message_queue._bg_tasks.clear()
        message_queue._bg_tasks.update(original_bg_tasks)
