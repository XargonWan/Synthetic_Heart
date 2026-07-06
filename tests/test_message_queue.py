import pytest
from types import SimpleNamespace

from core import message_queue, plugin_instance, rate_limit


@pytest.mark.asyncio
async def test_enqueue_clears_voice_and_tts_flags(monkeypatch):
    """The context dict should not retain ``is_voice_input``/``request_tts``
    after a non-voice message is enqueued.

    Regression: previously enqueue() only set those flags when the incoming
    message carried them, but never removed them.  If the context had been
    reused from a previous voice message, the stale ``is_voice_input`` value
    would persist and later cause message_chain to auto-inject a TTS reply
    even though the *current* input was plain text.  This test exercises the
    behaviour by passing in a pre-populated context and then verifying it is
    cleaned up.
    """

    # fake "previous" context that still believes the last input was voice
    context = {"is_voice_input": True, "request_tts": True}

    # avoid touching the database during this unit test
    from unittest.mock import AsyncMock

    monkeypatch.setattr(message_queue, "is_user_blocked", AsyncMock(return_value=False))

    # ensure a plugin is available so enqueue() doesn't bail out early
    from core import plugin_instance

    class DummyPlugin:
        def get_rate_limit(self):
            return 1000, 1, 1.0

    monkeypatch.setattr(plugin_instance, "get_plugin", lambda: DummyPlugin())
    monkeypatch.setattr(rate_limit, "is_allowed", lambda *args, **kwargs: True)

    # intercept consumer invocation so we can inspect the final context
    recorded = []

    async def fake_handle(bot, message, context_memory_or_prompt, interface=None, **kw):
        # context_memory_or_prompt may either be a dict or other object; for our
        # purposes it should be a dict containing the context that reaches the
        # plugin.
        if isinstance(context_memory_or_prompt, dict):
            recorded.append(context_memory_or_prompt.copy())
        else:
            recorded.append(context_memory_or_prompt)

    monkeypatch.setattr(plugin_instance, "handle_incoming_message", fake_handle)

    # construct a simple text-only message; minimal attributes used by enqueue
    msg = SimpleNamespace(
        chat_id=1,
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(
            type="private", id=1, title=None, username=None, first_name=None
        ),
        text="hello world",
    )

    # ensure the queue consumer is running so our fake_handle will be invoked
    await message_queue.run()

    # call enqueue with skip_mention_check to bypass the mention logic
    await message_queue.enqueue(
        bot=None,
        message=msg,
        context_memory=context,
        interface_id="telegram_bot",
        skip_mention_check=True,
    )

    # give the consumer loop a moment to process the item
    import asyncio

    await asyncio.sleep(0.1)

    assert recorded, "consumer should have been invoked"
    final_context = recorded[0]
    assert not final_context.get("is_voice_input", False), (
        "voice flag should be cleared by consumer"
    )
    assert not final_context.get("request_tts", False), (
        "request_tts flag should be cleared by consumer"
    )


@pytest.mark.asyncio
async def test_enqueue_and_wait_returns_plugin_response(monkeypatch):
    """enqueue_and_wait should return the plugin processing result."""

    from unittest.mock import AsyncMock

    monkeypatch.setattr(message_queue, "is_user_blocked", AsyncMock(return_value=False))

    class DummyPlugin:
        def get_rate_limit(self):
            return 1000, 1, 1.0

    monkeypatch.setattr(plugin_instance, "get_plugin", lambda: DummyPlugin())
    monkeypatch.setattr(
        message_queue.rate_limit, "is_allowed", lambda *args, **kwargs: True
    )

    async def fake_handle(bot, message, context_memory_or_prompt, interface=None, **kw):
        return "llm-ok"

    monkeypatch.setattr(plugin_instance, "handle_incoming_message", fake_handle)

    msg = SimpleNamespace(
        chat_id=1,
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(
            type="private", id=1, title=None, username=None, first_name=None
        ),
        text="hello world",
    )

    await message_queue.run()
    result = await message_queue.enqueue_and_wait(
        bot=None,
        message=msg,
        context_memory={},
        interface_id="telegram_bot",
        skip_mention_check=True,
        timeout=2.0,
    )

    assert result == "llm-ok"


@pytest.mark.asyncio
async def test_enqueue_preserves_explicit_interface_path(monkeypatch):
    """Explicit message.interface_path should be preserved through the queue."""

    from unittest.mock import AsyncMock

    monkeypatch.setattr(message_queue, "is_user_blocked", AsyncMock(return_value=False))

    class DummyPlugin:
        def get_rate_limit(self):
            return 1000, 1, 1.0

    monkeypatch.setattr(plugin_instance, "get_plugin", lambda: DummyPlugin())
    monkeypatch.setattr(
        message_queue.rate_limit, "is_allowed", lambda *args, **kwargs: True
    )

    recorded = []

    async def fake_handle(bot, message, context_memory_or_prompt, interface=None, **kw):
        if isinstance(context_memory_or_prompt, dict):
            recorded.append(context_memory_or_prompt.copy())
        return "ok"

    monkeypatch.setattr(plugin_instance, "handle_incoming_message", fake_handle)

    msg = SimpleNamespace(
        chat_id=1,
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(
            type="private", id=1, title=None, username=None, first_name=None
        ),
        text="hello world",
    )
    msg.interface_path = "telegram_bot/12345"

    await message_queue.run()
    result = await message_queue.enqueue_and_wait(
        bot=None,
        message=msg,
        context_memory={},
        interface_id="telegram_bot",
        skip_mention_check=True,
        timeout=2.0,
    )

    assert result == "ok"
    assert recorded
    assert recorded[0].get("interface_path") == "telegram_bot/12345"


@pytest.mark.asyncio
async def test_enqueue_recovers_grillo_activity_id_from_synthetic_message_id(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(message_queue, "is_user_blocked", AsyncMock(return_value=False))

    class DummyPlugin:
        def get_rate_limit(self):
            return 1000, 1, 1.0

    monkeypatch.setattr(plugin_instance, "get_plugin", lambda: DummyPlugin())
    monkeypatch.setattr(
        message_queue.rate_limit, "is_allowed", lambda *args, **kwargs: True
    )

    recorded = []

    async def fake_handle(bot, message, context_memory_or_prompt, interface=None, **kw):
        if isinstance(context_memory_or_prompt, dict):
            recorded.append(context_memory_or_prompt.copy())
        return "ok"

    monkeypatch.setattr(plugin_instance, "handle_incoming_message", fake_handle)

    msg = SimpleNamespace(
        chat_id=5551234567,
        message_id="grillo_observer_6364",
        from_user=SimpleNamespace(id=-1),
        chat=SimpleNamespace(
            type="private",
            id=5551234567,
            title=None,
            username=None,
            first_name=None,
        ),
        text="background observer",
    )

    await message_queue.run()
    result = await message_queue.enqueue_and_wait(
        bot=None,
        message=msg,
        context_memory={},
        interface_id="telegram_bot",
        skip_mention_check=True,
        timeout=2.0,
    )

    assert result == "ok"
    assert recorded
    assert recorded[0].get("activity_log_id") == 6364
    assert recorded[0].get("grillo_activity_log_id") == 6364


@pytest.mark.asyncio
async def test_supervisor_restarts_dead_consumer(monkeypatch):
    """The supervisor watchdog must restart the consumer if it dies unexpectedly.

    Regression: a per-message LLM timeout cancellation could propagate up and
    kill the consumer loop permanently. Without a supervisor the consumer never
    restarted, so every subsequent message queued silently and the VRM avatar
    stayed frozen on the ``think`` animation. The consumer must survive an
    accidental cancellation.
    """
    import asyncio

    # Speed the watchdog up so the test doesn't wait 5s.
    monkeypatch.setattr(message_queue, "_SUPERVISOR_INTERVAL_SECONDS", 0.05)

    # Make sure we start from a clean, non-shutdown state.
    await message_queue.stop()

    try:
        await message_queue.run()

        first_task = message_queue._consumer_task
        assert first_task is not None and not first_task.done()

        # Simulate an *accidental* cancellation (as a per-message timeout cancel
        # propagating up would cause). This must NOT be treated as a shutdown.
        first_task.cancel()

        # Wait for the supervisor to notice and restart the consumer.
        for _ in range(40):
            await asyncio.sleep(0.05)
            new_task = message_queue._consumer_task
            if (
                new_task is not None
                and new_task is not first_task
                and not new_task.done()
            ):
                break

        new_task = message_queue._consumer_task
        assert new_task is not None, "supervisor should have recreated the consumer"
        assert new_task is not first_task, "consumer task should be a fresh instance"
        assert not new_task.done(), "restarted consumer should be running"
    finally:
        await message_queue.stop()


@pytest.mark.asyncio
async def test_stop_prevents_consumer_restart(monkeypatch):
    """After a deliberate stop() the supervisor must not resurrect the consumer."""
    import asyncio

    monkeypatch.setattr(message_queue, "_SUPERVISOR_INTERVAL_SECONDS", 0.05)

    await message_queue.run()
    await message_queue.stop()

    # Give any lingering supervisor time to (wrongly) restart the consumer.
    await asyncio.sleep(0.2)

    assert message_queue._consumer_task is None
    assert message_queue._supervisor_task is None
    assert message_queue._shutdown_requested is True

    # Clean up for other tests: allow the queue to run again.
    message_queue._shutdown_requested = False
