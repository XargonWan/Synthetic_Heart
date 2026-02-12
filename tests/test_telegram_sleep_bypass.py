import asyncio
import pytest
from types import SimpleNamespace

# Skip tests if python-telegram-bot is not installed in the environment
try:
    import telegram  # type: ignore
except Exception:
    pytest.skip("python-telegram-bot not installed; skipping telegram integration tests", allow_module_level=True)

import interface.telegram_bot as tbot
from core.chat_attention import set_attention


@pytest.mark.asyncio
async def test_trainer_bypasses_private(monkeypatch):
    """Trainer direct (private) messages should bypass sleep and be processed."""
    trainer_id = 31321637
    chat_id = 123456789  # arbitrary private chat id

    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=trainer_id, username="Xargon", full_name="Jay Cheshire"),
        chat=SimpleNamespace(id=chat_id, type="private"),
        text="Private Rekku test",
        message_id=1000,
        message_thread_id=None,
        caption=None,
        photo=None,
        document=None,
        voice=None,
        video=None,
    )
    update = SimpleNamespace(message=msg)

    monkeypatch.setattr(tbot, "ensure_plugin_loaded", lambda update: True)
    monkeypatch.setattr(tbot, "is_trainer", lambda uid: uid == trainer_id)

    # Force chat to be asleep
    set_attention(chat_id, False)

    called = {}

    async def fake_enqueue(bot, message, interface_id=None, **kwargs):
        called['enqueued'] = True

    import core.message_queue as mq
    monkeypatch.setattr(mq, "enqueue", fake_enqueue)

    class Ctx:
        bot = SimpleNamespace()

    await tbot.handle_message(update, Ctx())
    assert called.get('enqueued', False), "Trainer private message should be enqueued despite sleep"


@pytest.mark.asyncio
async def test_trainer_does_not_bypass_supergroup(monkeypatch):
    """Trainer messages in supergroup should NOT bypass sleep by default."""
    trainer_id = 31321637
    chat_id = -1003098886330

    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=trainer_id, username="Xargon", full_name="Jay Cheshire"),
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        text="Group Rekku test",
        message_id=1001,
        message_thread_id=2,
        caption=None,
        photo=None,
        document=None,
        voice=None,
        video=None,
    )
    update = SimpleNamespace(message=msg)

    monkeypatch.setattr(tbot, "ensure_plugin_loaded", lambda update: True)
    monkeypatch.setattr(tbot, "is_trainer", lambda uid: uid == trainer_id)

    # Force group to be asleep
    set_attention(chat_id, False)

    called = {}

    async def fake_enqueue(bot, message, interface_id=None, **kwargs):
        called['enqueued'] = True

    import core.message_queue as mq
    monkeypatch.setattr(mq, "enqueue", fake_enqueue)

    class Ctx:
        bot = SimpleNamespace()

    await tbot.handle_message(update, Ctx())
    assert not called.get('enqueued', False), "Trainer group message should NOT bypass sleep"
