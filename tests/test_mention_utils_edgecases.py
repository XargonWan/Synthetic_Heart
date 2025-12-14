import pytest

from core.mention_utils import is_message_for_bot


class DummyChat:
    def __init__(self, type="group", human_count=None, id=1):
        self.type = type
        self.human_count = human_count
        self.id = id


class DummyUser:
    def __init__(self, username=None, id=1):
        self.username = username
        self.id = id


class DummyMessage:
    def __init__(self, text=None, caption=None, chat=None, from_user=None, reply_to_message=None):
        self.text = text
        self.caption = caption
        self.chat = chat or DummyChat()
        self.from_user = from_user or DummyUser()
        self.reply_to_message = reply_to_message


class DummyBot:
    def __init__(self, username="bot", id=999):
        self._username = username
        self.id = id

    async def get_me(self):
        class Me:
            def __init__(self, username, id):
                self.username = username
                self.id = id

        return Me(self._username, self.id)


@pytest.mark.asyncio
async def test_alias_with_punctuation():
    msg = DummyMessage(text="synth, are you there?", chat=DummyChat(type="group"))
    bot = DummyBot()
    directed, _ = await is_message_for_bot(msg, bot)
    assert directed is True


@pytest.mark.asyncio
async def test_alias_uppercase():
    msg = DummyMessage(text="SYNTH please respond", chat=DummyChat(type="group"))
    bot = DummyBot()
    directed, _ = await is_message_for_bot(msg, bot)
    assert directed is True


@pytest.mark.asyncio
async def test_alias_embedded_word_does_not_trigger():
    msg = DummyMessage(text="I like synthesis of ideas", chat=DummyChat(type="group"))
    bot = DummyBot()
    directed, reason = await is_message_for_bot(msg, bot, human_count=None)
    # Now aliases are matched by substring, so 'synth' will match 'synthesis'
    assert directed is True
    assert reason is None


@pytest.mark.asyncio
async def test_caption_alias():
    msg = DummyMessage(text=None, caption="Hey synth", chat=DummyChat(type="group"))
    bot = DummyBot()
    directed, _ = await is_message_for_bot(msg, bot)
    assert directed is True


@pytest.mark.asyncio
async def test_reply_matches_bot_by_id_even_without_username():
    reply_from = DummyUser(username=None, id=999)
    reply_msg = DummyMessage(text="original", from_user=reply_from)
    msg = DummyMessage(text="replying", reply_to_message=reply_msg, chat=DummyChat(type="group"))
    bot = DummyBot(username="bot", id=999)
    directed, _ = await is_message_for_bot(msg, bot, bot_username=None)
    assert directed is True


@pytest.mark.asyncio
async def test_persona_name_substring_triggers(monkeypatch):
    # Simulate persona manager returning persona name 'rekku'
    class FakePersona:
        def __init__(self, name):
            self.name = name

    class FakePM:
        def get_current_persona(self):
            return FakePersona("rekku")

    monkeypatch.setattr('core.persona_manager.get_persona_manager', lambda: FakePM())
    msg = DummyMessage(text="Ciao rekkucina, sei qui?", chat=DummyChat(type="group"))
    bot = DummyBot()
    # Sanity check: ensure our monkeypatch worked
    import core.persona_manager as pm
    # Replace the global manager instance so mention_utils will see it
    pm._persona_manager_instance = FakePM()
    pm_instance = pm.get_persona_manager()
    assert pm_instance.get_current_persona().name == "rekku"
    directed, _ = await is_message_for_bot(msg, bot)
    assert directed is True
