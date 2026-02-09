import asyncio
from types import SimpleNamespace

from plugins.grillo.grillo_impl import GrilloPlugin


async def test_grillo_uses_enqueue_low_priority(monkeypatch):
    called = {}

    async def fake_enqueue_low_priority(bot, message, context_memory=None, interface_id=None, original_message=None):
        called['args'] = {'bot': bot, 'message': message, 'context_memory': context_memory, 'interface_id': interface_id}
        return None

    import core.message_queue as message_queue
    monkeypatch.setattr(message_queue, 'enqueue_low_priority', fake_enqueue_low_priority)

    plugin = GrilloPlugin()
    # simulate enqueueing a beat; call the _enqueue_with_low_priority method
    await plugin._enqueue_with_low_priority('Test beat prompt', 'curiosity')

    assert 'args' in called
    assert called['args']['interface_id'] == 'grillo'
    assert called['args']['message'].text == 'Test beat prompt'
