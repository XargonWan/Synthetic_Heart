import pytest
from types import SimpleNamespace

from core.action_parser import corrector_orchestrator


@pytest.mark.asyncio
async def test_corrector_invoked_when_top_level_message_without_message_action(monkeypatch):
    called = {}

    async def fake_run_corrector_middleware(text, bot=None, context=None, chat_id=None, interface_path=None):
        called['text'] = text
        called['bot'] = bot
        called['context'] = context
        called['chat_id'] = chat_id
        called['interface_path'] = interface_path
        return None

    monkeypatch.setattr('core.transport_layer.run_corrector_middleware', fake_run_corrector_middleware)

    # Build a JSON-like LLM response: has actions that are internal only and a top-level message
    llm_text = '''{
        "actions": [
            {"type": "use_animation", "payload": {"animation_state": "think"}},
            {"type": "create_personal_diary_entry", "payload": {"interaction_summary": "x"}}
        ],
        "message": "This is a reply that should be sent to the user"
    }'''

    message = SimpleNamespace()
    message.from_llm = True
    message.chat_id = 12345
    message.interface_path = 'telegram_bot/12345'

    result = await corrector_orchestrator(llm_text, context={'interface': 'telegram'}, bot=None, message=message, max_retries=1)

    # Corrector should have been invoked; result may be True because valid actions were executed
    # and correction is requested for invalid synthetic actions
    assert result in (True, False)
    assert 'context' in called
    ctx = called['context']
    # Correction context should include the original message under 'message'
    assert 'message' in ctx
    assert ctx['message'] is message
    # The correction instruction should mention that a 'message' was provided and failed
    assert 'message' in called['text'] or (ctx.get('correction_context') and 'message' in ctx['correction_context'].get('instruction', ''))
