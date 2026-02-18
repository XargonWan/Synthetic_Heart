import pytest
from types import SimpleNamespace

# Skip if telegram not installed
try:
    pass  # type: ignore
except Exception:
    pytest.skip(
        "python-telegram-bot not installed; skipping telegram integration tests",
        allow_module_level=True,
    )

import interface.telegram_bot as tbot
from core.chat_attention import get_attention, set_attention


@pytest.mark.asyncio
async def test_custom_sleep_trigger_puts_chat_to_sleep(monkeypatch):
    """When CHAT_SLEEP_COMMANDS contains a phrase, a message with that phrase should put chat to sleep."""
    chat_id = 999123

    # Make the configuration return 'bye 2b' as the sleep trigger
    monkeypatch.setattr(
        "core.chat_attention.config_registry.get_value",
        lambda k, d=None, **kwargs: "bye 2b" if k == "CHAT_SLEEP_COMMANDS" else "",
    )

    trainer_id = 555
    msg = SimpleNamespace(
        from_user=SimpleNamespace(
            id=trainer_id, username="Xargon", full_name="Xargon Test"
        ),
        chat=SimpleNamespace(id=chat_id, type="private"),
        text="bye 2b",
        message_id=1,
        message_thread_id=None,
        caption=None,
        photo=None,
        document=None,
        voice=None,
        video=None,
        video_note=None,
    )
    msg.chat_id = chat_id
    msg.reply_to_message = None
    update = SimpleNamespace(message=msg)

    # stub ensure_plugin_loaded
    async def ep(u):
        return True

    monkeypatch.setattr(tbot, "ensure_plugin_loaded", ep)

    called = {"enq": False}

    async def fake_enqueue(bot, message, interface_id=None, **kwargs):
        called["enq"] = True

    import core.message_queue as mq

    monkeypatch.setattr(mq, "enqueue", fake_enqueue)

    class Ctx:
        bot = SimpleNamespace()

    # Ensure chat is initially awake
    set_attention(chat_id, True)

    await tbot.handle_message(update, Ctx())

    # After processing, chat should be asleep
    assert get_attention(chat_id) is False


@pytest.mark.asyncio
async def test_empty_config_does_not_trigger_sleep(monkeypatch):
    """If CHAT_SLEEP_COMMANDS is empty, phrases like 'bye 2b' should NOT put chat to sleep."""
    chat_id = 999124

    # Ensure no configured triggers
    monkeypatch.setattr(
        "core.chat_attention.config_registry.get_value",
        lambda k, d=None, **kwargs: (
            "" if k in ("CHAT_SLEEP_COMMANDS", "CHAT_WAKE_COMMANDS") else ""
        ),
    )

    trainer_id = 555
    msg = SimpleNamespace(
        from_user=SimpleNamespace(
            id=trainer_id, username="Xargon", full_name="Xargon Test"
        ),
        chat=SimpleNamespace(id=chat_id, type="private"),
        text="bye 2b",
        message_id=1,
        message_thread_id=None,
        caption=None,
        photo=None,
        document=None,
        voice=None,
        video=None,
        video_note=None,
    )
    msg.chat_id = chat_id
    msg.reply_to_message = None
    update = SimpleNamespace(message=msg)

    async def ep(u):
        return True

    monkeypatch.setattr(tbot, "ensure_plugin_loaded", ep)

    async def fake_enqueue(bot, message, interface_id=None, **kwargs):
        pass

    import core.message_queue as mq

    monkeypatch.setattr(mq, "enqueue", fake_enqueue)

    class Ctx:
        bot = SimpleNamespace()

    # Initially awake
    set_attention(chat_id, True)

    await tbot.handle_message(update, Ctx())

    # Should remain awake if no configured triggers
    assert get_attention(chat_id) is True
