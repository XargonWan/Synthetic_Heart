import pytest

from core.mention_utils import is_message_for_bot


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
    def __init__(self, username=None, id=1):
        self.username = username
        self.id = id


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
    assert reason == "missing_human_count"
