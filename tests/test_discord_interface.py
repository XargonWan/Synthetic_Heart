import asyncio
import pytest
from interface.discord_interface import DiscordInterface


def test_execute_action_with_interface_path(monkeypatch):
    di = DiscordInterface(bot_token="")

    called = {}

    async def fake_send_message(arg1, arg2=None, **kwargs):
        called['arg1'] = arg1
        called['arg2'] = arg2
        called.update(kwargs)

    monkeypatch.setattr(di, 'send_message', fake_send_message)

    action = {
        'type': 'message_discord_bot',
        'payload': {
            'interface_path': 'discord_bot/111111111/222222222',
            'text': 'hello from test'
        }
    }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(di.execute_action(action, context={}, bot=None))
    loop.close()

    assert 'arg1' in called
    # when interface_path provided, we expect send_message called with dict containing interface_path
    assert isinstance(called['arg1'], dict)
    assert called['arg1']['interface_path'] == 'discord_bot/111111111/222222222'
    assert called['arg1']['text'] == 'hello from test'
