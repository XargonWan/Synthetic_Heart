import pytest
import asyncio
from types import SimpleNamespace

from plugins.grillo.grillo_action_checker import GrilloActionChecker

import core.plugin_instance as plugin_instance


@pytest.mark.asyncio
async def test_requests_actions_using_user_and_assistant_text(monkeypatch):
    checker = GrilloActionChecker()

    llm_reply = "Va bene, ti avviso domani alle 9"

    captured = {}

    async def fake_handle(bot, message, prompt):
        # Capture prompt for assertions
        captured['prompt'] = prompt
        # Return a valid JSON string containing actions
        return '{"actions": [{"type": "schedule_message", "payload": {"text": "Promemoria", "send_in": "1 day"}}]}'

    monkeypatch.setattr(plugin_instance, 'handle_incoming_message', fake_handle)

    message = SimpleNamespace()
    message.chat_id = 123
    message.thread_id = None

    actions = await checker.inspect_reply_and_suggest_actions(llm_reply, "Per favore ricordami domani", {}, message)

    assert isinstance(actions, list)
    assert actions[0]['type'] == 'schedule_message'

    # Ensure the prompt includes both user and assistant text and asks for ONLY JSON
    assert 'User message' in captured['prompt']
    assert 'Assistant reply' in captured['prompt']
    assert 'ONLY a JSON' in captured['prompt']

    # Also ensure we marked the message as checked to avoid duplicate processing
    assert getattr(message, 'grillo_checked', True) is True


@pytest.mark.asyncio
async def test_prompt_includes_available_actions_snippet(monkeypatch):
    checker = GrilloActionChecker()

    # Mock available actions in core_initializer
    from core.core_initializer import core_initializer
    core_initializer.actions_block = {
        'available_actions': {
            'schedule_message': {
                'schema': {
                    'required': ['text', 'send_in'],
                    'properties': {'text': {}, 'send_in': {}, 'interface_path': {}}
                }
            },
            'message_telegram_bot': {
                'schema': {
                    'required': ['text'],
                    'properties': {'text': {}, 'reply_to_message_id': {}, 'interface_path': {}}
                }
            }
        }
    }

    captured = {}

    async def fake_handle(bot, message, prompt):
        captured['prompt'] = prompt
        return '{"actions": []}'

    monkeypatch.setattr(plugin_instance, 'handle_incoming_message', fake_handle)

    message = SimpleNamespace()
    message.chat_id = 2
    # Provide a last_action_result to ensure it is included in the prompt
    message.last_action_result = {'processed': [], 'failed': [], 'errors': []}

    actions = await checker.inspect_reply_and_suggest_actions("Va bene, ti avviso domani", "Ricordami domani", {'chain_result': 'ACTIONS_EXECUTED'}, message)

    assert isinstance(actions, list)
    # Prompt should include the compact action snippet we built
    assert 'Relevant actions:' in captured['prompt']
    assert '- schedule_message' in captured['prompt']
    assert 'required' in captured['prompt']

    # Ensure execution summary is present in the prompt
    assert 'Execution summary' in captured['prompt']
    assert 'ACTIONS_EXECUTED' in captured['prompt']
    assert 'last_action_result' in captured['prompt']


@pytest.mark.asyncio
async def test_returns_empty_actions_when_llm_says_nothing_to_do(monkeypatch):
    checker = GrilloActionChecker()
    llm_reply = "Ok, fatto"

    async def fake_handle(bot, message, prompt):
        # Return JSON with empty actions array
        return '{"actions": []}'

    monkeypatch.setattr(plugin_instance, 'handle_incoming_message', fake_handle)

    message = SimpleNamespace()
    message.chat_id = 1

    actions = await checker.inspect_reply_and_suggest_actions(llm_reply, "Ping", {}, message)
    # Should return an empty list (no actions needed)
    assert isinstance(actions, list)
    assert actions == []

