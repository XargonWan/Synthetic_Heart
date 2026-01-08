import pytest
import asyncio
from types import SimpleNamespace

from plugins.message_plugin import MessagePlugin
import core.core_initializer as core_init
from core.chat_context_manager import get_or_create_chat_context


class FakeHandler:
    def __init__(self):
        self.calls = []

    async def send_message(self, payload, original_message=None):
        self.calls.append((payload, original_message))


@pytest.mark.asyncio
async def test_grillo_suppresses_when_last_is_synth(monkeypatch):
    fake = FakeHandler()
    # Ensure INTERFACE_REGISTRY has a fake telegram handler
    monkeypatch.setitem(core_init.INTERFACE_REGISTRY, 'telegram_bot', fake)

    # Prepare in-memory context for the target interface_path
    ctx = get_or_create_chat_context('telegram_bot/123/2')
    ctx.clear()
    # Append a synth/self message as last
    ctx.append({'sender_name': 'self', 'sender_id': 'self', 'text': 'I already said that', 'timestamp': '2026-01-08T00:00:00Z', 'interface_path': 'telegram_bot/123/2'})

    plugin = MessagePlugin()
    action = {'type': 'message_telegram_bot', 'payload': {'text': 'Hello again', 'interface_path': 'telegram_bot/123/2'}}

    await plugin.execute_action(action, context={'grillo_beat': True, 'activity_log_id': 1}, bot=None, original_message=None)

    # Fake handler should not have been called
    assert fake.calls == [], "Grillo should not send when last message is from synth"


@pytest.mark.asyncio
async def test_grillo_obeys_toggle_and_allows_when_disabled(monkeypatch):
    """If GRILLO_SUPPRESS_INACTIVE is disabled, Grillo should send even if last message is synth."""
    fake = FakeHandler()
    monkeypatch.setitem(core_init.INTERFACE_REGISTRY, 'telegram_bot', fake)

    # prepare context with last message from synth
    ctx = get_or_create_chat_context('telegram_bot/123/2')
    ctx.clear()
    ctx.append({'sender_name': 'self', 'sender_id': 'self', 'text': 'I already said that', 'timestamp': '2026-01-08T00:00:00Z', 'interface_path': 'telegram_bot/123/2'})

    # Monkeypatch config_registry.get_value to return False for the suppression key
    import core.config_manager as cm
    orig_get = cm.config_registry.get_value

    def fake_get(key, default, **kwargs):
        if key == 'GRILLO_SUPPRESS_INACTIVE':
            return False
        return orig_get(key, default, **kwargs)

    monkeypatch.setattr(cm.config_registry, 'get_value', fake_get)

    plugin = MessagePlugin()
    action = {'type': 'message_telegram_bot', 'payload': {'text': 'Hello allowed', 'interface_path': 'telegram_bot/123/2'}}

    await plugin.execute_action(action, context={'grillo_beat': True, 'activity_log_id': 3}, bot=None, original_message=None)

    # Fake handler should have been called since suppression is disabled
    assert len(fake.calls) == 1
    assert fake.calls[0][0]['text'] == 'Hello allowed'


@pytest.mark.asyncio
async def test_suppressed_increments_counter(monkeypatch):
    """When suppression happens, GrilloPlugin.suppressed_count should increase and activity_log annotated."""
    fake = FakeHandler()
    monkeypatch.setitem(core_init.INTERFACE_REGISTRY, 'telegram_bot', fake)

    ctx = get_or_create_chat_context('telegram_bot/123/2')
    ctx.clear()
    ctx.append({'sender_name': 'self', 'sender_id': 'self', 'text': 'I already said that', 'timestamp': '2026-01-08T00:00:00Z', 'interface_path': 'telegram_bot/123/2'})

    # Ensure suppression is enabled
    import core.config_manager as cm
    orig_get = cm.config_registry.get_value

    def fake_get_true(key, default, **kwargs):
        if key == 'GRILLO_SUPPRESS_INACTIVE':
            return True
        return orig_get(key, default, **kwargs)

    monkeypatch.setattr(cm.config_registry, 'get_value', fake_get_true)

    # Reset counter
    from plugins.grillo.grillo_impl import GrilloPlugin
    GrilloPlugin.suppressed_count = 0

    plugin = MessagePlugin()
    action = {'type': 'message_telegram_bot', 'payload': {'text': 'Hello again', 'interface_path': 'telegram_bot/123/2'}}

    await plugin.execute_action(action, context={'grillo_beat': True, 'activity_log_id': 4}, bot=None, original_message=None)

    assert GrilloPlugin.suppressed_count == 1, "Suppressed counter should increment when a duplicate is suppressed"


@pytest.mark.asyncio
async def test_record_suppressed_updates_db(monkeypatch):
    """record_suppressed_event should attempt a DB update; test via mocking get_conn_ctx"""
    calls = {"exec": []}

    class DummyCursor:
        def __init__(self):
            self.exec_calls = []

        async def execute(self, sql, params=None):
            calls["exec"].append((sql, params))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    class DummyConn:
        def __init__(self):
            self.cur = DummyCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def cursor(self):
            return self.cur

        async def commit(self):
            pass

    class DummyCtx:
        def __init__(self):
            self.conn = DummyConn()

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            pass

    import core.db as cdb

    monkeypatch.setattr(cdb, 'get_conn_ctx', lambda: DummyCtx())

    from plugins.grillo.grillo_impl import GrilloPlugin

    # Reset counter
    GrilloPlugin.suppressed_count = 0

    await GrilloPlugin.record_suppressed_event(activity_log_id=123, reason='unit-test')

    # Ensure our mocked execute was called with an UPDATE that increments suppressed_count
    assert calls["exec"], "Expected DB execute to be called"
    found = any('suppressed_count' in (sql or '') for sql, _ in calls["exec"])
    assert found, "Expected SQL to mention suppressed_count"

@pytest.mark.asyncio
async def test_grillo_allows_when_last_is_user(monkeypatch):
    fake = FakeHandler()
    monkeypatch.setitem(core_init.INTERFACE_REGISTRY, 'telegram_bot', fake)

    ctx = get_or_create_chat_context('telegram_bot/123/2')
    ctx.clear()
    # Append a user message as last
    ctx.append({'sender_name': 'Alice', 'sender_id': '42', 'text': "I'm here", 'timestamp': '2026-01-08T00:01:00Z', 'interface_path': 'telegram_bot/123/2'})

    plugin = MessagePlugin()
    action = {'type': 'message_telegram_bot', 'payload': {'text': 'Hello Alice', 'interface_path': 'telegram_bot/123/2'}}

    await plugin.execute_action(action, context={'grillo_beat': True, 'activity_log_id': 2}, bot=None, original_message=None)

    assert len(fake.calls) == 1
    assert fake.calls[0][0]['text'] == 'Hello Alice'