import pytest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

# Skip if telegram not installed
try:
    pass
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

    # Make the configuration return 'bye synth' as the sleep trigger
    monkeypatch.setattr(
        "core.chat_attention.config_registry.get_value",
        lambda k, d=None, **kwargs: "bye synth" if k == "CHAT_SLEEP_COMMANDS" else "",
    )

    trainer_id = 555
    msg = SimpleNamespace(
        from_user=SimpleNamespace(
            id=trainer_id, username="Xargon", full_name="Xargon Test"
        ),
        chat=SimpleNamespace(id=chat_id, type="private"),
        text="bye synth",
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
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context",
        AsyncMock(return_value=None),
    )

    called = {"enq": False}

    async def fake_enqueue(bot, message, interface_id=None, **kwargs):
        called["enq"] = True

    import core.message_queue as mq

    monkeypatch.setattr(mq, "enqueue", fake_enqueue)

    class Ctx:
        bot = SimpleNamespace()

    # Ensure chat is initially awake
    set_attention(chat_id, True)

    await tbot.handle_message(cast(Any, update), cast(Any, Ctx()))

    # After processing, chat should be asleep
    assert get_attention(chat_id) is False


@pytest.mark.asyncio
async def test_empty_config_does_not_trigger_sleep(monkeypatch):
    """If CHAT_SLEEP_COMMANDS is empty, phrases like 'bye synth' should NOT put chat to sleep."""
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
        text="bye synth",
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
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context",
        AsyncMock(return_value=None),
    )

    async def fake_enqueue(bot, message, interface_id=None, **kwargs):
        pass

    import core.message_queue as mq

    monkeypatch.setattr(mq, "enqueue", fake_enqueue)

    class Ctx:
        bot = SimpleNamespace()

    # Initially awake
    set_attention(chat_id, True)

    await tbot.handle_message(cast(Any, update), cast(Any, Ctx()))

    # Should remain awake if no configured triggers
    assert get_attention(chat_id) is True


@pytest.mark.asyncio
async def test_start_bot_failure_resets_state_and_schedules_retry(monkeypatch):
    scheduled = {"called": False}

    class FakeFilter:
        def __and__(self, _other):
            return self

        def __or__(self, _other):
            return self

        def __invert__(self):
            return self

    fake_filter = FakeFilter()

    class FailingApp:
        def __init__(self):
            self.updater = None
            self.bot = SimpleNamespace()

        def add_handler(self, _handler):
            return None

        def add_error_handler(self, _handler):
            return None

        async def start(self):
            return None

        async def initialize(self):
            raise TimeoutError("Timed out")

    class FakeBuilder:
        def token(self, _token):
            return self

        def post_init(self, _callback):
            return self

        def connect_timeout(self, _timeout):
            return self

        def read_timeout(self, _timeout):
            return self

        def write_timeout(self, _timeout):
            return self

        def pool_timeout(self, _timeout):
            return self

        def get_updates_connection_pool_size(self, _size):
            return self

        def get_updates_pool_timeout(self, _timeout):
            return self

        def get_updates_connect_timeout(self, _timeout):
            return self

        def get_updates_read_timeout(self, _timeout):
            return self

        def get_updates_write_timeout(self, _timeout):
            return self

        def build(self):
            return FailingApp()

    fake_interface = SimpleNamespace(bot="stale", is_enabled=True, disabled_reason=None)

    monkeypatch.setattr(tbot, "BOTFATHER_TOKEN", "token")
    monkeypatch.setattr(tbot, "telegram_interface", fake_interface)
    monkeypatch.setattr(tbot, "_bot_started", False)
    monkeypatch.setattr(tbot, "_bot_starting", False)
    monkeypatch.setattr(tbot, "_bot_retry_task", None)
    monkeypatch.setattr(tbot.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(tbot, "_parse_trainer_id_from_config", lambda: "123")
    monkeypatch.setattr(tbot, "get_trainer_id", lambda: "123")
    monkeypatch.setattr(tbot, "ApplicationBuilder", lambda: FakeBuilder())
    monkeypatch.setattr(tbot, "MessageHandler", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        tbot,
        "filters",
        SimpleNamespace(
            COMMAND=fake_filter,
            TEXT=fake_filter,
            PHOTO=fake_filter,
            AUDIO=fake_filter,
            VOICE=fake_filter,
            VIDEO=fake_filter,
            Document=SimpleNamespace(ALL=fake_filter),
            Sticker=SimpleNamespace(ALL=fake_filter),
        ),
    )
    monkeypatch.setattr(
        tbot._interface_registry,
        "set_trainer_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        tbot,
        "_schedule_start_bot_retry",
        lambda delay_seconds=30.0: scheduled.__setitem__("called", True),
    )

    started = await tbot.start_bot()

    assert started is False
    assert tbot._bot_started is False
    assert tbot._bot_starting is False
    assert fake_interface.bot is None
    assert fake_interface.is_enabled is False
    assert "Timed out" in str(fake_interface.disabled_reason)
    assert scheduled["called"] is True


@pytest.mark.asyncio
async def test_start_bot_retries_transient_timeout_inline(monkeypatch):
    scheduled = {"called": False}
    refresh_actions = AsyncMock(return_value=None)
    app_builds = {"count": 0}

    class FakeFilter:
        def __and__(self, _other):
            return self

        def __or__(self, _other):
            return self

        def __invert__(self):
            return self

    fake_filter = FakeFilter()

    class FakeUpdater:
        def __init__(self):
            self.running = False

        async def stop(self):
            return None

        async def start_polling(self):
            return None

    class RetryApp:
        def __init__(self, *, fail_initialize: bool):
            self.updater = FakeUpdater()
            self.bot = SimpleNamespace(name="bot")
            self._fail_initialize = fail_initialize

        def add_handler(self, _handler):
            return None

        def add_error_handler(self, _handler):
            return None

        async def initialize(self):
            if self._fail_initialize:
                raise TimeoutError("Timed out")

        async def start(self):
            return None

        async def stop(self):
            return None

        async def shutdown(self):
            return None

    class FakeTask:
        def __init__(self):
            self.name = None

        def set_name(self, name: str) -> None:
            self.name = name

    class FakeBuilder:
        def token(self, _token):
            return self

        def post_init(self, _callback):
            return self

        def connect_timeout(self, _timeout):
            return self

        def read_timeout(self, _timeout):
            return self

        def write_timeout(self, _timeout):
            return self

        def pool_timeout(self, _timeout):
            return self

        def get_updates_connection_pool_size(self, _size):
            return self

        def get_updates_pool_timeout(self, _timeout):
            return self

        def get_updates_connect_timeout(self, _timeout):
            return self

        def get_updates_read_timeout(self, _timeout):
            return self

        def get_updates_write_timeout(self, _timeout):
            return self

        def build(self):
            app_builds["count"] += 1
            return RetryApp(fail_initialize=app_builds["count"] == 1)

    fake_interface = SimpleNamespace(
        bot=None, is_enabled=False, disabled_reason="stale"
    )

    monkeypatch.setattr(tbot, "BOTFATHER_TOKEN", "token")
    monkeypatch.setattr(tbot, "telegram_interface", fake_interface)
    monkeypatch.setattr(tbot, "_bot_started", False)
    monkeypatch.setattr(tbot, "_bot_starting", False)
    monkeypatch.setattr(tbot, "_bot_retry_task", None)
    monkeypatch.setattr(tbot, "_parse_trainer_id_from_config", lambda: "123")
    monkeypatch.setattr(tbot, "get_trainer_id", lambda: "123")
    monkeypatch.setattr(tbot, "ApplicationBuilder", lambda: FakeBuilder())
    monkeypatch.setattr(tbot, "MessageHandler", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        tbot,
        "filters",
        SimpleNamespace(
            COMMAND=fake_filter,
            TEXT=fake_filter,
            PHOTO=fake_filter,
            AUDIO=fake_filter,
            VOICE=fake_filter,
            VIDEO=fake_filter,
            Document=SimpleNamespace(ALL=fake_filter),
            Sticker=SimpleNamespace(ALL=fake_filter),
        ),
    )
    monkeypatch.setattr(
        tbot._interface_registry,
        "set_trainer_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        tbot,
        "_schedule_start_bot_retry",
        lambda delay_seconds=30.0: scheduled.__setitem__("called", True),
    )
    monkeypatch.setattr(tbot.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(tbot.asyncio, "create_task", lambda _coro: FakeTask())
    monkeypatch.setattr(
        "core.core_initializer.core_initializer.refresh_actions_block",
        refresh_actions,
    )

    started = await tbot.start_bot()

    assert started is True
    assert app_builds["count"] == 2
    assert tbot._bot_started is True
    assert fake_interface.bot is not None
    assert fake_interface.is_enabled is True
    assert fake_interface.disabled_reason is None
    assert scheduled["called"] is False
    refresh_actions.assert_awaited_once()
