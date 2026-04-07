import types
from types import SimpleNamespace


import sys

# Create lightweight stubs for the `telegram` package so tests run in isolation
if "telegram" not in sys.modules:
    telegram = types.ModuleType("telegram")
    # Mark as package so submodule imports (telegram.constants) work during tests
    telegram.__path__ = []
    telegram.Update = object
    telegram.Bot = object
    # error submodule
    err = types.ModuleType("telegram.error")

    class TelegramError(Exception):
        pass

    class RetryAfter(Exception):
        pass

    class BadRequest(Exception):
        pass

    class TimedOut(Exception):
        pass

    err.TelegramError = TelegramError
    err.RetryAfter = RetryAfter
    err.BadRequest = BadRequest
    err.TimedOut = TimedOut
    # constants submodule
    const = types.ModuleType("telegram.constants")
    const.ParseMode = types.SimpleNamespace(MARKDOWN="MARKDOWN")

    sys.modules["telegram"] = telegram
    sys.modules["telegram.error"] = err
    sys.modules["telegram.constants"] = const

from interface import telegram_bot


def test_resolve_from_plugin_tuple(monkeypatch):
    reply = SimpleNamespace(message_id=10, reply_to_message=None, text=None)

    def fake_get_target(mid):
        if mid == 10:
            return (12345, 678)
        return None

    monkeypatch.setattr(telegram_bot.plugin_instance, "get_target", fake_get_target)

    import asyncio

    chat_id, msg_id = asyncio.run(telegram_bot._resolve_original_from_reply(reply))
    assert chat_id == 12345 and msg_id == 678


def test_resolve_from_plugin_dict(monkeypatch):
    reply = SimpleNamespace(message_id=11, reply_to_message=None, text=None)

    def fake_get_target(mid):
        if mid == 11:
            return {"chat_id": -200, "message_id": 42}
        return None

    monkeypatch.setattr(telegram_bot.plugin_instance, "get_target", fake_get_target)

    import asyncio

    chat_id, msg_id = asyncio.run(telegram_bot._resolve_original_from_reply(reply))
    assert chat_id == -200 and msg_id == 42


def test_resolve_from_forward_metadata():
    fchat = SimpleNamespace(id=-3000)
    reply = SimpleNamespace(
        message_id=12,
        reply_to_message=None,
        text=None,
        forward_from_chat=fchat,
        forward_from_message_id=555,
    )
    import asyncio

    chat_id, msg_id = asyncio.run(telegram_bot._resolve_original_from_reply(reply))
    assert chat_id == -3000 and msg_id == 555


def test_resolve_from_textual_fallback():
    reply = SimpleNamespace(
        message_id=13,
        reply_to_message=None,
        text="(original message from chat -4000 id 777)",
    )
    import asyncio

    chat_id, msg_id = asyncio.run(telegram_bot._resolve_original_from_reply(reply))
    assert chat_id == -4000 and msg_id == 777


def test_manual_tracks_last_sent_chunk(monkeypatch):
    from cortex.llm_provider.dev.manual import ManualAIPlugin

    plugin = ManualAIPlugin()

    # Create prompt JSON long enough to produce two chunks
    prompt_json = "x" * 5000

    # Make safe_send return two chunk messages, last one with message_id
    sent_objs = [None, types.SimpleNamespace(message_id=9002)]

    async def fake_safe_send(bot, chat_id, text, **kwargs):
        return sent_objs.pop(0) if sent_objs else None

    monkeypatch.setattr(telegram_bot, "safe_send", fake_safe_send)

    import core.config as config

    monkeypatch.setattr(config, "get_trainer_id", lambda iface: 9999)
    assert config.get_trainer_id("telegram_bot") == 9999

    # Ensure trainer id is present by monkeypatching core.config.get_trainer_id
    import core.config as config

    monkeypatch.setattr(config, "get_trainer_id", lambda iface: 9999)
    assert config.get_trainer_id("telegram_bot") == 9999

    # Make bot.forward_message raise so forwarded is not available
    fake_bot = types.SimpleNamespace()

    async def fake_forward_message(**kwargs):
        raise Exception("forward failed")

    fake_bot.forward_message = fake_forward_message

    # Track calls to track_message
    calls = []

    async def fake_track(trainer_id, original_chat, original_msg):
        calls.append((trainer_id, original_chat, original_msg))

    monkeypatch.setattr(plugin, "track_message", fake_track)

    message = SimpleNamespace(
        chat_id=123, message_id=456, from_user=SimpleNamespace(id=1, username="u")
    )
    # Call handle_incoming_message (it will schedule init_message_map_table; keep simple)
    import asyncio

    asyncio.run(plugin.handle_incoming_message(fake_bot, message, {}))

    assert calls, "track_message should have been called"


def test_manual_prefers_forwarded_message(monkeypatch):
    from cortex.llm_provider.dev.manual import ManualAIPlugin

    plugin = ManualAIPlugin()

    # safe_send returns a chunk message
    async def fake_safe_send(bot, chat_id, text, **kwargs):
        return types.SimpleNamespace(message_id=9003)

    monkeypatch.setattr(telegram_bot, "safe_send", fake_safe_send)

    # bot.forward_message returns a forwarded message
    fake_bot = types.SimpleNamespace()

    async def fake_forward_message(chat_id, from_chat_id, message_id):
        return types.SimpleNamespace(message_id=7777)

    fake_bot.forward_message = fake_forward_message

    calls = []

    async def fake_track(trainer_id, original_chat, original_msg):
        calls.append((trainer_id, original_chat, original_msg))

    monkeypatch.setattr(plugin, "track_message", fake_track)

    message = SimpleNamespace(
        chat_id=123, message_id=456, from_user=SimpleNamespace(id=1, username="u")
    )
    import asyncio

    asyncio.run(plugin.handle_incoming_message(fake_bot, message, {}))

    # Ensure track_message used forwarded message id (7777) via internal call
    assert calls, "track_message should have been called"
