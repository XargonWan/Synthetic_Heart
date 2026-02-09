import asyncio

from types import SimpleNamespace

from core.mention_utils import is_message_for_bot, get_bot_username


class FakeDiscordClient:
    def __init__(self, user_id: int, name: str):
        self.user = SimpleNamespace(id=user_id, name=name)


def make_reply_message(reply_author_id=None, reply_author_name=None, chat_type="group", text="hi"):
    reply = SimpleNamespace(
        from_user=SimpleNamespace(id=reply_author_id, username=reply_author_name)
    )
    msg = SimpleNamespace(
        text=text,
        chat=SimpleNamespace(type=chat_type),
        reply_to_message=reply,
    )
    return msg


async def test_get_bot_username_handles_discord_shape():
    bot = FakeDiscordClient(user_id=1234, name="SynthBot")
    username = await get_bot_username(bot)
    assert username.lower() == "synthbot"


async def test_is_message_for_bot_recognizes_reply_to_discord_bot():
    bot = FakeDiscordClient(user_id=555, name="synth")
    # message is a reply to a message authored by the bot
    msg = make_reply_message(reply_author_id=555, reply_author_name="synth", chat_type="group", text="Thanks")
    directed, reason = await is_message_for_bot(msg, bot, bot_username=None, human_count=2)
    assert directed is True
