from types import SimpleNamespace

import pytest

from core.action_parser import _request_selective_correction


@pytest.mark.asyncio
async def test_request_selective_correction_includes_message_in_context(monkeypatch):
    called = {}

    async def fake_run_corrector_middleware(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        # capture context for assertions
        called["text"] = text
        called["bot"] = bot
        called["context"] = context
        called["chat_id"] = chat_id
        called["thread_id"] = thread_id
        # Return a mock corrected JSON to simulate LLM reply (not necessary for test)
        return None

    # patch both the transport layer and the action_parser import
    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware", fake_run_corrector_middleware
    )
    monkeypatch.setattr(
        "core.action_parser.run_corrector_middleware", fake_run_corrector_middleware
    )

    failed_actions = [
        {
            "index": 0,
            "action": {"type": "message_send", "payload": {"text": "hi"}},
            "errors": [
                "Unsupported type 'message_send' - no plugin or interface found to handle it"
            ],
        }
    ]
    successful_actions = [
        {"type": "create_personal_diary_entry", "payload": {"interaction_summary": "x"}}
    ]

    original_message = SimpleNamespace()
    original_message.from_cortex = True
    original_message.chat_id = 999
    original_message.thread_id = None

    # Call the helper
    await _request_selective_correction(
        failed_actions=failed_actions,
        successful_actions=successful_actions,
        bot=None,
        context={"interface": "telegram"},
        original_message=original_message,
    )

    assert "context" in called, "run_corrector_middleware was not invoked"
    ctx = called["context"]
    # It should include the original message under 'message'
    assert "message" in ctx, "context did not include 'message'"
    assert ctx["message"] is original_message
    assert ctx.get("selective_correction", False) is True
    assert "correction_context" in ctx
    # The correction_context should reference the failed action type
    cc = ctx["correction_context"]
    instr = cc.get("instruction", "")
    # Instruction should reference failed actions and include the invalid type
    assert (
        "FAILED ACTIONS" in instr
        or "failed_actions" in instr
        or "not a valid action type" in instr
    )
    assert "message_send" in instr
    assert "Unsupported type" in instr or "not a valid action type" in instr
