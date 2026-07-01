import pytest
from types import SimpleNamespace

from core.mention_utils import is_message_for_bot
import core.chat_attention as chat_attention


class DummyChat:
    def __init__(
        self, type="group", human_count=None, title=None, username=None, id=123
    ):
        self.type = type
        self.human_count = human_count
        self.title = title
        self.username = username
        self.id = id


class DummyUser:
    def __init__(self, username=None, id=1, is_bot=False):
        self.username = username
        self.id = id
        self.is_bot = is_bot


class DummyMessage:
    def __init__(
        self, text=None, chat=None, from_user=None, reply_to_message=None, caption=None
    ):
        self.text = text
        self.caption = caption
        self.chat = chat or DummyChat()
        self.from_user = from_user or DummyUser()
        self.reply_to_message = reply_to_message


class DummyBot:
    def __init__(self, username="botname", id=999):
        self._username = username
        self.id = id

    async def get_me(self):
        class Me:
            def __init__(self, username, id):
                self.username = username
                self.id = id

        return Me(self._username, self.id)


@pytest.mark.asyncio
async def test_private_message_always_for_bot():
    msg = DummyMessage(text="hello", chat=DummyChat(type="private"))
    bot = DummyBot()
    directed, reason = await is_message_for_bot(msg, bot)
    assert directed is True


@pytest.mark.asyncio
async def test_explicit_alias_in_text():
    msg = DummyMessage(text="Hey Synth, are you there?", chat=DummyChat(type="group"))
    bot = DummyBot()
    directed, reason = await is_message_for_bot(msg, bot)
    assert directed is True


@pytest.mark.asyncio
async def test_explicit_at_mention():
    msg = DummyMessage(text="@synth what's up", chat=DummyChat(type="group"))
    bot = DummyBot()
    directed, reason = await is_message_for_bot(msg, bot)
    assert directed is True


@pytest.mark.asyncio
async def test_reply_to_bot_by_username():
    reply_from = DummyUser(username="botname", id=999)
    reply_msg = DummyMessage(text="original", from_user=reply_from)
    msg = DummyMessage(
        text="replying", reply_to_message=reply_msg, chat=DummyChat(type="group")
    )
    bot = DummyBot(username="botname", id=999)
    directed, reason = await is_message_for_bot(msg, bot, bot_username="botname")
    assert directed is True


@pytest.mark.asyncio
async def test_missing_human_count_returns_reason():
    msg = DummyMessage(
        text="just chatting", chat=DummyChat(type="group", human_count=None)
    )
    bot = DummyBot()
    directed, reason = await is_message_for_bot(msg, bot, human_count=None)
    assert directed is False
    # terminology changed to 'unknown_human_count'
    assert reason in ("missing_human_count", "unknown_human_count")


@pytest.mark.asyncio
async def test_media_only_message_not_directed_in_group():
    # no text but has voice attachment in a group chat – should not trigger
    # because the bot wasn't mentioned.
    msg = DummyMessage(chat=DummyChat(type="group"))
    msg.voice = SimpleNamespace()
    bot = DummyBot()
    directed, reason = await is_message_for_bot(msg, bot)
    assert directed is False
    # no explicit mention; reason may be human-count related
    assert reason in (None, "unknown_human_count")


@pytest.mark.asyncio
async def test_media_in_private_always_directed():
    msg = DummyMessage(chat=DummyChat(type="private"))
    msg.photo = SimpleNamespace()
    bot = DummyBot()
    directed, reason = await is_message_for_bot(msg, bot)
    assert directed is True


@pytest.mark.asyncio
async def test_chat_asleep_non_wake_message():
    msg = DummyMessage(text="hello", chat=DummyChat(type="group", id=9999))
    bot = DummyBot()
    # Set chat to asleep
    chat_attention.set_attention(9999, False)
    directed, reason = await is_message_for_bot(msg, bot)
    assert directed is False
    assert reason == "chat_asleep"
    # Restore attention for isolation
    chat_attention.set_attention(9999, True)


@pytest.mark.asyncio
async def test_chat_asleep_wake_message(monkeypatch):
    msg = DummyMessage(text="please wake", chat=DummyChat(type="group", id=9998))
    bot = DummyBot()
    chat_attention.set_attention(9998, False)
    # Ensure 'wake' is a configured wake trigger
    monkeypatch.setattr("core.chat_attention.get_wake_triggers", lambda: ["wake"])
    directed, reason = await is_message_for_bot(msg, bot)
    assert directed is True
    # Restore attention for isolation
    chat_attention.set_attention(9998, True)


@pytest.mark.asyncio
async def test_attention_window_grants_directed_without_alias(monkeypatch):
    """A non-bot sender in an engaged chat gets a reply even with no alias/mention."""
    monkeypatch.setattr("core.chat_attention.is_engaged", lambda scope_id: True)
    msg = DummyMessage(
        text="just a plain follow-up, no alias here",
        chat=DummyChat(type="group", id=7001),
    )
    bot = DummyBot()
    directed, reason = await is_message_for_bot(msg, bot)
    assert directed is True


@pytest.mark.asyncio
async def test_attention_window_disabled_falls_through_to_normal_gating(monkeypatch):
    """When the window isn't engaged, a plain follow-up is still not directed."""
    monkeypatch.setattr("core.chat_attention.is_engaged", lambda scope_id: False)
    msg = DummyMessage(
        text="just a plain follow-up, no alias here",
        chat=DummyChat(type="group", id=7002),
    )
    bot = DummyBot()
    directed, reason = await is_message_for_bot(msg, bot)
    assert directed is False


@pytest.mark.asyncio
async def test_attention_window_does_not_bypass_peer_suppression(monkeypatch):
    """Even with an active attention window, a peer SyntH message under a
    'silent' policy must still be suppressed -- peer suppression runs first
    and unconditionally, before the attention window is ever consulted."""
    monkeypatch.setattr("core.chat_attention.is_engaged", lambda scope_id: True)
    monkeypatch.setattr("core.peer_policy.is_peer_mode_enabled", lambda: True)
    monkeypatch.setattr(
        "core.peer_policy.is_peer_synth", lambda user_id: user_id == 555
    )
    monkeypatch.setattr("core.peer_policy.get_peer_policy", lambda: "silent")

    msg = DummyMessage(
        text="just chattering along, not addressing anyone in particular",
        chat=DummyChat(type="group", id=7004),
        from_user=DummyUser(username="peer_bot", id=555, is_bot=True),
    )
    bot = DummyBot()
    directed, reason = await is_message_for_bot(msg, bot)
    assert directed is False
    assert reason == "peer_synth"


@pytest.mark.asyncio
async def test_attention_window_never_applies_to_bot_sender(monkeypatch):
    """Cascade safety: engagement must never grant a free pass to a bot sender,
    even if the attention window is active for that chat."""
    monkeypatch.setattr("core.chat_attention.is_engaged", lambda scope_id: True)
    msg = DummyMessage(
        text="just chattering along, not addressing anyone in particular",
        chat=DummyChat(type="group", id=7003),
        from_user=DummyUser(username="peer_bot", id=555, is_bot=True),
    )
    bot = DummyBot()
    directed, reason = await is_message_for_bot(msg, bot)
    assert directed is False
