import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from core.reaction_handler import get_reaction_emoji
import core.reaction_handler as rh

from interface import telegram_bot
from core.core_initializer import PLUGIN_REGISTRY


class DummyFile:
    async def download_to_drive(self, custom_path):
        # create an empty file to simulate download
        open(custom_path, "wb").close()


class FakeAuris:
    def __init__(self, ret):
        self._ret = ret

    async def transcribe_audio(self, path, hint):
        return self._ret


@pytest.mark.asyncio
async def test_handle_media_live_transcribes(tmp_path, monkeypatch):
    # prepare a fake voice message update
    voice = SimpleNamespace(
        file_id="file123",
        mime_type="audio/ogg",
        file_name="foo.ogg",
        file_unique_id="uniq",
    )
    msg = SimpleNamespace(
        voice=voice,
        video=None,
        video_note=None,
        chat_id=1,
        message_id=456,
        from_user=SimpleNamespace(id=99),
        set_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
        chat=SimpleNamespace(type="private", id=1),
    )
    update = SimpleNamespace(message=msg)

    # fake bot that returns our DummyFile
    bot = SimpleNamespace(
        get_file=AsyncMock(return_value=DummyFile()), send_chat_action=AsyncMock()
    )
    ctx = SimpleNamespace(bot=bot)

    # ensure Auris plugin returns a predictable transcription
    orig = PLUGIN_REGISTRY.get("auris_plugin")
    PLUGIN_REGISTRY["auris_plugin"] = FakeAuris("hello world")
    try:
        # capture calls to message_queue.enqueue
        recorded = []

        async def fake_enqueue(
            bot_arg, wrapped, interface_id, original_message, skip_mention_check
        ):
            recorded.append(
                (wrapped, interface_id, original_message, skip_mention_check)
            )

        monkeypatch.setattr(telegram_bot.message_queue, "enqueue", fake_enqueue)
        # run once with no config emoji to check fallback icon
        monkeypatch.setattr(rh, "get_reaction_emoji", lambda: None)
        msg.set_reaction.reset_mock()
        await telegram_bot.handle_media_live(update, ctx)
        msg.set_reaction.assert_awaited_once_with("👂")
        # remove the patch so later config test uses real function
        monkeypatch.setattr(rh, "get_reaction_emoji", get_reaction_emoji)

        # reset recorded for second phase
        recorded.clear()

        # also capture config-based reaction if set
        reacted = []

        def fake_react(interface, message_obj, emoji):
            reacted.append(emoji)
            return True

        monkeypatch.setattr(rh, "react_when_mentioned", fake_react)
        # make sure interface registry returns something truthy
        import core.core_initializer as ci

        ci.INTERFACE_REGISTRY["telegram_bot"] = object()

        with patch.dict("os.environ", {"REACT_WHEN_MENTIONED": "🔥"}):
            await telegram_bot.handle_media_live(update, ctx)

        # reaction should have been invoked
        assert reacted == ["🔥"]

        assert recorded, "enqueue should have been called"
        wrapped, iface, orig_msg, skip = recorded[0]
        assert wrapped.text == "hello world"
        assert getattr(wrapped, "is_voice_input", False)
        assert getattr(wrapped, "request_tts", False), "request_tts flag should be set"
        assert iface == "telegram_bot"
        assert orig_msg is msg
        assert skip is True
    finally:
        # restore registry
        if orig is None:
            PLUGIN_REGISTRY.pop("auris_plugin", None)
        else:
            PLUGIN_REGISTRY["auris_plugin"] = orig


@pytest.mark.asyncio
async def test_handle_media_live_no_file(monkeypatch):
    # message with no file_id should early return and log warning
    msg = SimpleNamespace(
        voice=None,
        video=None,
        video_note=None,
        chat_id=1,
        message_id=123,
        from_user=SimpleNamespace(id=1),
        set_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
    )
    update = SimpleNamespace(message=msg)
    bot = SimpleNamespace(get_file=AsyncMock())
    ctx = SimpleNamespace(bot=bot)

    # patch PLUGIN_REGISTRY.get just in case
    # ensure no auris plugin present
    orig = PLUGIN_REGISTRY.get("auris_plugin")
    if "auris_plugin" in PLUGIN_REGISTRY:
        del PLUGIN_REGISTRY["auris_plugin"]
    try:
        # no exception should be raised
        await telegram_bot.handle_media_live(update, ctx)
    finally:
        if orig is None:
            PLUGIN_REGISTRY.pop("auris_plugin", None)
        else:
            PLUGIN_REGISTRY["auris_plugin"] = orig


@pytest.mark.asyncio
async def test_handle_media_live_auris_empty(monkeypatch):
    # voice message with auris returning empty string should fall back to dispatch_media
    # which enqueues a placeholder text into the message queue (not reply_text).
    voice = SimpleNamespace(
        file_id="file123",
        mime_type="audio/ogg",
        file_name="foo.ogg",
        file_unique_id="uniq",
    )
    msg = SimpleNamespace(
        voice=voice,
        video=None,
        video_note=None,
        chat_id=1,
        message_id=456,
        from_user=SimpleNamespace(id=99),
        set_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(message=msg)
    bot = SimpleNamespace(
        get_file=AsyncMock(return_value=DummyFile()), send_chat_action=AsyncMock()
    )
    ctx = SimpleNamespace(bot=bot)

    # prepare Auris — returns empty string
    orig_auris = PLUGIN_REGISTRY.get("auris_plugin")
    PLUGIN_REGISTRY["auris_plugin"] = FakeAuris("")

    # patch dispatch_media to return a fallback transcription
    recorded = []

    async def fake_enqueue(
        bot_arg, wrapped, interface_id=None, original_message=None, **kw
    ):
        recorded.append((wrapped, original_message))

    monkeypatch.setattr(telegram_bot.message_queue, "enqueue", fake_enqueue)

    # patch dispatch_media so no real engine is called
    with patch(
        "core.media_dispatcher.dispatch_media",
        AsyncMock(return_value="dispatch fallback"),
    ):
        try:
            await telegram_bot.handle_media_live(update, ctx)
            assert recorded, "dispatch_media result should be enqueued"
            wrapped, orig_msg = recorded[0]
            assert wrapped.text == "dispatch fallback"
            assert getattr(wrapped, "is_voice_input", False)
            assert getattr(wrapped, "request_tts", False)
            # old reply_text path should NOT be called
            msg.reply_text.assert_not_awaited()
        finally:
            if orig_auris is None:
                PLUGIN_REGISTRY.pop("auris_plugin", None)
            else:
                PLUGIN_REGISTRY["auris_plugin"] = orig_auris


@pytest.mark.asyncio
async def test_handle_media_live_auris_empty_no_handler(monkeypatch):
    """If Auris returns an empty string and dispatch_media also returns None,
    a placeholder text is enqueued so the user gets a response.
    """
    voice = SimpleNamespace(
        file_id="file123",
        mime_type="audio/ogg",
        file_name="foo.ogg",
        file_unique_id="uniq",
    )

    msg = SimpleNamespace(
        voice=voice,
        video=None,
        video_note=None,
        chat_id=1,
        message_id=456,
        from_user=SimpleNamespace(id=99),
        set_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(message=msg)
    bot = SimpleNamespace(
        get_file=AsyncMock(return_value=DummyFile()), send_chat_action=AsyncMock()
    )
    ctx = SimpleNamespace(bot=bot)

    # make Auris plugin return empty string
    orig_auris = PLUGIN_REGISTRY.get("auris_plugin")
    PLUGIN_REGISTRY["auris_plugin"] = FakeAuris("")

    # capture enqueue
    recorded = []

    async def fake_enqueue(
        bot_arg, wrapped, interface_id=None, original_message=None, **kw
    ):
        recorded.append((wrapped, original_message))

    monkeypatch.setattr(telegram_bot.message_queue, "enqueue", fake_enqueue)

    # dispatch_media returns None — placeholder text should be used
    with patch("core.media_dispatcher.dispatch_media", AsyncMock(return_value=None)):
        try:
            await telegram_bot.handle_media_live(update, ctx)
            assert recorded, (
                "placeholder should be enqueued when dispatch_media returns None"
            )
            wrapped, orig_msg = recorded[0]
            assert "voice" in wrapped.text.lower() or "media" in wrapped.text.lower()
            assert getattr(wrapped, "request_tts", False), "wrapper should request tts"
            assert orig_msg is msg
        finally:
            if orig_auris is None:
                PLUGIN_REGISTRY.pop("auris_plugin", None)
            else:
                PLUGIN_REGISTRY["auris_plugin"] = orig_auris


@pytest.mark.asyncio
async def test_reply_to_media_with_alias_triggers_transcription(monkeypatch):
    """Replying to a media message while tagging the bot should transcribe it."""
    voice = SimpleNamespace(
        file_id="file123",
        mime_type="audio/ogg",
        file_name="foo.ogg",
        file_unique_id="uniq",
    )
    original = SimpleNamespace(
        voice=voice,
        chat_id=1,
        message_id=100,
        from_user=SimpleNamespace(id=2),
        set_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
        reply_text=AsyncMock(),
    )
    msg = SimpleNamespace(
        text="@synth please transcribe",
        chat_id=1,
        message_id=101,
        chat=SimpleNamespace(type="group", id=1),
        from_user=SimpleNamespace(id=99, full_name="Tester", username="tester"),
        reply_to_message=original,
        voice=None,
        video=None,
        video_note=None,
        photo=None,
        document=None,
        sticker=None,
        animation=None,
        audio=None,
        set_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(message=msg)
    bot = SimpleNamespace(
        get_file=AsyncMock(return_value=DummyFile()), send_chat_action=AsyncMock()
    )
    ctx = SimpleNamespace(bot=bot)

    # force mention util to treat message as directed
    monkeypatch.setattr(
        "core.mention_utils.is_message_for_bot", AsyncMock(return_value=(True, None))
    )
    # prevent plugin loading logic from running (not needed for this test)
    monkeypatch.setattr(
        telegram_bot, "ensure_plugin_loaded", AsyncMock(return_value=True)
    )

    orig_auris = PLUGIN_REGISTRY.get("auris_plugin")
    PLUGIN_REGISTRY["auris_plugin"] = FakeAuris("replied transcription")
    # intercept message_queue.enqueue to capture wrapped message
    from core import message_queue

    recorded = []

    async def fake_enqueue(
        bot_arg, wrapped, interface_id=None, original_message=None, **kwargs
    ):
        recorded.append((wrapped, original_message))
        return None

    monkeypatch.setattr(message_queue, "enqueue", fake_enqueue)

    try:
        await telegram_bot.handle_message(update, ctx)
        # ensure transcription text was sent into the queue with original media
        assert recorded, "expected transcription to be enqueued"
        wrapped_msg, orig_msg = recorded[0]
        assert getattr(wrapped_msg, "text", None) == "replied transcription"
        assert orig_msg is original
    finally:
        if orig_auris is None:
            PLUGIN_REGISTRY.pop("auris_plugin", None)
        else:
            PLUGIN_REGISTRY["auris_plugin"] = orig_auris


@pytest.mark.asyncio
async def test_handle_media_live_auris_disabled_no_handler(monkeypatch):
    """When Auris is disabled and dispatch_media returns None, a placeholder
    is still enqueued so the user receives a response."""
    voice = SimpleNamespace(
        file_id="file123",
        mime_type="audio/ogg",
        file_name="foo.ogg",
        file_unique_id="uniq",
    )
    msg = SimpleNamespace(
        voice=voice,
        video=None,
        video_note=None,
        chat_id=1,
        message_id=456,
        from_user=SimpleNamespace(id=99),
        set_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(message=msg)
    bot = SimpleNamespace(
        get_file=AsyncMock(return_value=DummyFile()), send_chat_action=AsyncMock()
    )
    ctx = SimpleNamespace(bot=bot)

    # make Auris plugin exist but return empty so primary path doesn't handle
    orig_auris = PLUGIN_REGISTRY.get("auris_plugin")
    PLUGIN_REGISTRY["auris_plugin"] = FakeAuris("")

    # capture enqueue
    recorded = []

    async def fake_enqueue(
        bot_arg, wrapped, interface_id=None, original_message=None, **kw
    ):
        recorded.append((wrapped, original_message))

    monkeypatch.setattr(telegram_bot.message_queue, "enqueue", fake_enqueue)

    with patch("core.media_dispatcher.dispatch_media", AsyncMock(return_value=None)):
        try:
            await telegram_bot.handle_media_live(update, ctx)
            assert recorded, "transcription should be enqueued"
            wrapped, orig_msg = recorded[0]
            assert "voice" in wrapped.text.lower() or "media" in wrapped.text.lower()
            assert getattr(wrapped, "request_tts", False), "wrapper should request tts"
            assert orig_msg is msg
        finally:
            if orig_auris is None:
                PLUGIN_REGISTRY.pop("auris_plugin", None)
            else:
                PLUGIN_REGISTRY["auris_plugin"] = orig_auris


@pytest.mark.asyncio
async def test_handle_media_live_reacts_when_directed(monkeypatch):
    """When the bot is considered directed and an emoji is configured we call
    the interface's `add_reaction` through `react_when_mentioned`.

    This primarily exercises the new path added to handle_media_live that
    evaluates `get_reaction_emoji` and looks up the interface in
    INTERFACE_REGISTRY.  We stub out Auris and dispatch_media to keep the
    remainder of the handler simple.
    """
    voice = SimpleNamespace(
        file_id="file123",
        mime_type="audio/ogg",
        file_name="foo.ogg",
        file_unique_id="uniq",
    )

    msg = SimpleNamespace(
        voice=voice,
        video=None,
        video_note=None,
        chat_id=1,
        message_id=456,
        from_user=SimpleNamespace(id=99),
        set_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(message=msg)
    bot = SimpleNamespace(
        get_file=AsyncMock(return_value=DummyFile()), send_chat_action=AsyncMock()
    )
    ctx = SimpleNamespace(bot=bot)

    # force emoji to be present and patch the config helper just in case
    monkeypatch.setattr("core.reaction_handler.REACT_WHEN_MENTIONED", "💡")
    monkeypatch.setattr("core.reaction_handler.get_reaction_emoji", lambda: "💡")

    # avoid needing a full message.chat object by short-circuiting mention_utils
    monkeypatch.setattr(
        "core.mention_utils.is_message_for_bot", AsyncMock(return_value=(True, None))
    )

    # put a dummy interface into the registry so handle_media_live can look it up
    from core.core_initializer import INTERFACE_REGISTRY

    dummy_iface = SimpleNamespace(add_reaction=AsyncMock(return_value=True))
    INTERFACE_REGISTRY["telegram_bot"] = dummy_iface

    # stub out auris plugin to return transcription directly
    orig_auris = PLUGIN_REGISTRY.get("auris_plugin")
    PLUGIN_REGISTRY["auris_plugin"] = FakeAuris("ok transcription")

    recorded = []

    async def fake_enqueue(
        bot_arg, wrapped, interface_id=None, original_message=None, **kw
    ):
        recorded.append(wrapped)

    monkeypatch.setattr(telegram_bot.message_queue, "enqueue", fake_enqueue)

    try:
        await telegram_bot.handle_media_live(update, ctx)
        dummy_iface.add_reaction.assert_awaited_once_with(msg, "💡")
    finally:
        if orig_auris is None:
            PLUGIN_REGISTRY.pop("auris_plugin", None)
        else:
            PLUGIN_REGISTRY["auris_plugin"] = orig_auris
        INTERFACE_REGISTRY.pop("telegram_bot", None)


@pytest.mark.asyncio
async def test_handle_media_live_no_reaction_when_not_directed(monkeypatch):
    """Reactions should not be attempted if `is_message_for_bot` returns False."""
    voice = SimpleNamespace(
        file_id="file123",
        mime_type="audio/ogg",
        file_name="foo.ogg",
        file_unique_id="uniq",
    )

    msg = SimpleNamespace(
        voice=voice,
        video=None,
        video_note=None,
        chat_id=1,
        message_id=456,
        from_user=SimpleNamespace(id=99),
        set_reaction=AsyncMock(),
        send_chat_action=AsyncMock(),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(message=msg)
    bot = SimpleNamespace(
        get_file=AsyncMock(return_value=DummyFile()), send_chat_action=AsyncMock()
    )
    ctx = SimpleNamespace(bot=bot)

    monkeypatch.setattr("core.reaction_handler.REACT_WHEN_MENTIONED", "💡")
    monkeypatch.setattr("core.reaction_handler.get_reaction_emoji", lambda: "💡")
    monkeypatch.setattr(
        "core.mention_utils.is_message_for_bot", AsyncMock(return_value=(False, None))
    )

    from core.core_initializer import INTERFACE_REGISTRY

    dummy_iface = SimpleNamespace(add_reaction=AsyncMock(return_value=True))
    INTERFACE_REGISTRY["telegram_bot"] = dummy_iface

    # stub out auris plugin to return a transcription (so handler exits cleanly)
    orig_auris = PLUGIN_REGISTRY.get("auris_plugin")
    PLUGIN_REGISTRY["auris_plugin"] = FakeAuris("transcribed")

    recorded = []

    async def fake_enqueue(
        bot_arg, wrapped, interface_id=None, original_message=None, **kw
    ):
        recorded.append(wrapped)

    monkeypatch.setattr(telegram_bot.message_queue, "enqueue", fake_enqueue)

    try:
        await telegram_bot.handle_media_live(update, ctx)
        assert not dummy_iface.add_reaction.called
    finally:
        if orig_auris is None:
            PLUGIN_REGISTRY.pop("auris_plugin", None)
        else:
            PLUGIN_REGISTRY["auris_plugin"] = orig_auris
        INTERFACE_REGISTRY.pop("telegram_bot", None)
