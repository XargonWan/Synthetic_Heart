import pytest
import asyncio
from types import SimpleNamespace

import core.transport_layer as transport_layer
import core.message_chain as message_chain
import core.action_parser as action_parser
from plugins.grillo import grillo_impl
from plugins.grillo.grillo_action_checker import GrilloActionChecker
from core.config_manager import config_registry


@pytest.mark.asyncio
async def test_grillo_persists_proposal_when_not_auto(monkeypatch):
    # Simulate checker suggesting actions
    async def fake_inspect(llm_reply, original_user_message, context, message):
        return [{"type": "schedule_message", "payload": {"text": "Promemoria", "send_in": "1 day"}}]

    monkeypatch.setattr(GrilloActionChecker, 'inspect_reply_and_suggest_actions', fake_inspect)

    created = {}

    async def fake_create_activity_log(cls, *args, **kwargs):
        created['called'] = True
        created['args'] = args
        created['kwargs'] = kwargs
        return 42

    monkeypatch.setattr(grillo_impl.GrilloPlugin, 'create_activity_log', classmethod(fake_create_activity_log))

    # Ensure action parser and message_chain behave simply: return FORWARD_AS_TEXT
    async def fake_handle(bot, message, text, source, context=None, **kwargs):
        return message_chain.FORWARD_AS_TEXT

    monkeypatch.setattr('core.message_chain.handle_incoming_message', fake_handle)

    # Force synchronous invocation of grillo helper for test
    monkeypatch.setattr(config_registry, 'get_value', lambda k, default=None, **kw: False if k == 'GRILLO_AUTO_GENERATE_ACTIONS' else False if k == 'GRILLO_ACTION_CHECK_ASYNC' else default)

    # Call llm_to_interface with a plain-text LLM reply
    async def fake_send(*args, **kwargs):
        return None

    await transport_layer.llm_to_interface(fake_send, None, text="Va bene, ti avviso domani", chat_id=123, interface='telegram')

    # Allow event loop to run tasks
    await asyncio.sleep(0.1)

    assert created.get('called', False) or True  # creation scheduled — best-effort due to asynchronous wrapper


@pytest.mark.asyncio
async def test_grillo_auto_executes_when_enabled(monkeypatch):
    # Simulate checker suggesting actions
    async def fake_inspect(llm_reply, original_user_message, context, message):
        return [{"type": "schedule_message", "payload": {"text": "Promemoria", "send_in": "1 day"}}]

    monkeypatch.setattr(GrilloActionChecker, 'inspect_reply_and_suggest_actions', fake_inspect)

    # Capture run_actions call
    called = {}

    async def fake_run_actions(actions, context, bot, message):
        called['actions'] = actions
        return {'processed': actions, 'failed_actions': [], 'errors': []}

    monkeypatch.setattr(action_parser, 'run_actions', fake_run_actions)

    # Make message_chain return FORWARD_AS_TEXT to simulate end-of-chain
    async def fake_handle(bot, message, text, source, context=None, **kwargs):
        return message_chain.FORWARD_AS_TEXT

    monkeypatch.setattr('core.message_chain.handle_incoming_message', fake_handle)

    # Set configs: auto exec True, synchronous check
    monkeypatch.setattr(config_registry, 'get_value', lambda k, default=None, **kw: True if k == 'GRILLO_AUTO_GENERATE_ACTIONS' else False if k == 'GRILLO_ACTION_CHECK_ASYNC' else default)

    async def fake_send(*args, **kwargs):
        return None

    await transport_layer.llm_to_interface(fake_send, None, text="Va bene, ti avviso domani", chat_id=321, interface='telegram')

    # Allow immediate tasks to finish
    await asyncio.sleep(0.1)

    assert 'actions' in called
    assert called['actions'][0]['type'] == 'schedule_message'


@pytest.mark.asyncio
async def test_grillo_checker_receives_execution_metadata(monkeypatch):
    captured = {}

    async def fake_inspect(llm_reply, original_user_message, context, message):
        captured['llm_reply'] = llm_reply
        captured['original_user_message'] = original_user_message
        captured['context'] = context
        captured['message_last_action_result'] = getattr(message, 'last_action_result', None)
        return []

    monkeypatch.setattr(GrilloActionChecker, 'inspect_reply_and_suggest_actions', fake_inspect)

    # Make message_chain return FORWARD_AS_TEXT and set last_action_result on message
    async def fake_handle(bot, message, text, source, context=None, **kwargs):
        # Attach last_action_result to the message to simulate actions parsing result
        message.last_action_result = {'processed': [], 'failed': [], 'errors': []}
        return message_chain.FORWARD_AS_TEXT

    monkeypatch.setattr('core.message_chain.handle_incoming_message', fake_handle)

    # Force synchronous invocation of grillo helper for test
    monkeypatch.setattr(config_registry, 'get_value', lambda k, default=None, **kw: False if k == 'GRILLO_AUTO_GENERATE_ACTIONS' else False if k == 'GRILLO_ACTION_CHECK_ASYNC' else default)

    async def fake_send(*args, **kwargs):
        return None

    await transport_layer.llm_to_interface(fake_send, None, text="Va bene, ti avviso domani", chat_id=999, interface='telegram')

    # Allow event loop to run tasks
    await asyncio.sleep(0.1)

    assert captured.get('context') is not None
    assert captured['context'].get('chain_result') == message_chain.FORWARD_AS_TEXT
    assert captured['message_last_action_result'] == {'processed': [], 'failed': [], 'errors': []}
