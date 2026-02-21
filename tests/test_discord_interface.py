import asyncio
from types import SimpleNamespace

import pytest
from interface.discord_interface import DiscordInterface


def test_execute_action_with_interface_path(monkeypatch):
    di = DiscordInterface(bot_token="")

    called = {}

    async def fake_send_message(arg1, arg2=None, **kwargs):
        called["arg1"] = arg1
        called["arg2"] = arg2
        called.update(kwargs)

    monkeypatch.setattr(di, "send_message", fake_send_message)

    action = {
        "type": "message_discord_bot",
        "payload": {
            "interface_path": "discord_bot/111111111/222222222",
            "text": "hello from test",
        },
    }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(di.execute_action(action, context={}, bot=None))
    loop.close()

    assert "arg1" in called
    # when interface_path provided, we expect send_message called with dict containing interface_path
    assert isinstance(called["arg1"], dict)
    assert called["arg1"]["interface_path"] == "discord_bot/111111111/222222222"
    assert called["arg1"]["text"] == "hello from test"


async def _async_noop(*args, **kwargs):
    return None


def test_send_dm_to_user_id(monkeypatch):
    di = DiscordInterface(bot_token="")

    # Create a fake client where get_channel returns None and get_user returns an object
    class FakeUser:
        def __init__(self):
            self.sent = []

        async def send(self, content):
            self.sent.append(content)

    class FakeClient:
        def get_channel(self, id):
            return None

        def get_user(self, id):
            return FakeUser()

        async def fetch_user(self, id):
            return FakeUser()

    fake_client = FakeClient()
    di.client = fake_client

    # Run the send via the internal method
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(di._discord_send("111111111", "hello dm"))
    loop.close()

    # Verify that the fake user's send was called by checking the object returned
    # (get_user returns a new FakeUser instance, so we can't inspect it directly here)
    # Instead, monkeypatch get_user to return a shared instance and test it
    shared_user = FakeUser()
    fake_client.get_user = lambda id: shared_user

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(di._discord_send("222222222", "second dm"))
    loop.close()

    assert shared_user.sent == ["second dm"]


async def _fake_add_message_to_context(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_plain_at_symbol_does_not_mark_explicit_trigger(monkeypatch):
    """Typing a plain '@' (without Discord-style <@id> mention) must NOT wake Synth."""
    di = DiscordInterface(bot_token="")

    # Fake client with bot user set
    di.client = SimpleNamespace(user=SimpleNamespace(id=999, name="SynthBot"))

    # Fake message that contains a plain '@' but does NOT include a Discord mention token
    class FakeChannel:
        id = 12345
        type = "text"

    fake_message = SimpleNamespace(
        content="hello @someone",
        author=SimpleNamespace(id=2, name="user2", display_name="User Two"),
        channel=FakeChannel(),
        guild=SimpleNamespace(id=1),
        mentions=[],
        role_mentions=[],
        reference=None,
        created_at=SimpleNamespace(isoformat=lambda: "2026-01-01T00:00:00"),
        id=111,
        attachments=[],
        content_raw=None,
    )

    captured = {}

    async def fake_enqueue(bot, wrapped, **kwargs):
        captured["wrapped"] = wrapped
        return None

    # Prevent external helpers from raising during the unit test
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context", _fake_add_message_to_context
    )
    monkeypatch.setattr(
        "interface.discord_interface.chat_link_store.update_names_from_resolver",
        (lambda *a, **k: _fake_add_message_to_context()),
    )
    monkeypatch.setattr("core.message_queue.enqueue", fake_enqueue)

    await di._process_message(fake_message)

    assert "wrapped" in captured
    assert getattr(captured["wrapped"], "is_explicit_trigger", False) is False


@pytest.mark.asyncio
async def test_real_discord_mention_marks_explicit_trigger(monkeypatch):
    """A real Discord-style mention (<@id>) should still mark explicit trigger."""
    di = DiscordInterface(bot_token="")

    # Fake client with bot user set
    di.client = SimpleNamespace(user=SimpleNamespace(id=999, name="SynthBot"))

    # Fake message that contains Discord mention token and mentions list contains bot
    class FakeChannel:
        id = 22222
        type = "text"

    fake_message = SimpleNamespace(
        content=f"<@{999}> hello",
        author=SimpleNamespace(id=3, name="user3", display_name="User Three"),
        channel=FakeChannel(),
        guild=SimpleNamespace(id=2),
        mentions=[SimpleNamespace(id=999, name="SynthBot")],
        role_mentions=[],
        reference=None,
        created_at=SimpleNamespace(isoformat=lambda: "2026-01-01T00:00:00"),
        id=222,
        attachments=[],
    )

    captured = {}

    async def fake_enqueue(bot, wrapped, **kwargs):
        captured["wrapped"] = wrapped
        return None

    # Prevent external helpers from raising during the unit test
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context", _fake_add_message_to_context
    )
    monkeypatch.setattr(
        "interface.discord_interface.chat_link_store.update_names_from_resolver",
        (lambda *a, **k: _fake_add_message_to_context()),
    )
    monkeypatch.setattr("core.message_queue.enqueue", fake_enqueue)

    await di._process_message(fake_message)

    assert "wrapped" in captured
    assert getattr(captured["wrapped"], "is_explicit_trigger", False) is True
