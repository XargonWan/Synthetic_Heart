import pytest
from types import SimpleNamespace

from core import message_queue


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
    from core import plugin_instance, rate_limit

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
