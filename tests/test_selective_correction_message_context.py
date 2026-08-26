from types import SimpleNamespace

import pytest

from core.action_parser import (
    _request_selective_correction,
    _is_delivered_auto_tts_failure,
)


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
    assert cc["successful_actions"] == successful_actions
    assert cc["failed_actions"] == failed_actions
    assert cc["successful_count"] == 1
    assert cc["failed_count"] == 1
    instr = cc.get("instruction", "")
    # Instruction should reference failed actions and include the invalid type
    assert (
        "FAILED ACTIONS" in instr
        or "failed_actions" in instr
        or "not a valid action type" in instr
    )
    assert "message_send" in instr
    assert "Unsupported type" in instr or "not a valid action type" in instr


@pytest.mark.asyncio
async def test_request_selective_correction_skips_unfixable_failures(monkeypatch):
    called = {"count": 0}

    async def fake_run_corrector_middleware(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        called["count"] += 1
        return None

    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware", fake_run_corrector_middleware
    )
    monkeypatch.setattr(
        "core.action_parser.run_corrector_middleware", fake_run_corrector_middleware
    )

    failed_actions = [
        {
            "index": 0,
            "action": {
                "type": "update_emotion_state",
                "payload": {"emotions": {"love": 5.0}},
            },
            "errors": [
                "Action 'update_emotion_state' blocked by safety policy: blocked: not whitelisted"
            ],
            "unfixable": True,
        }
    ]

    original_message = SimpleNamespace(from_cortex=True, chat_id=999, thread_id=None)

    await _request_selective_correction(
        failed_actions=failed_actions,
        successful_actions=[
            {
                "type": "message_telegram_bot",
                "payload": {"text": "visible reply"},
            }
        ],
        bot=None,
        context={"interface": "telegram"},
        original_message=original_message,
    )

    assert called["count"] == 0


@pytest.mark.asyncio
async def test_request_selective_correction_skips_delivered_auto_tts(monkeypatch):
    """Auto-injected tts_speak failures whose text-only fallback already
    delivered the reply must NOT reach the corrector middleware.

    Regression for the double-interface-output bug: message_chain merges the
    standalone message action into an auto-injected tts_speak and drops it, so
    when synthesis fails VoxPlugin's text-only fallback (reason
    ``tts_failed_fallback_sent``) IS the reply. Re-running the LLM would
    re-emit the message action and deliver a duplicate audio message.
    """
    called = {"count": 0}

    async def fake_run_corrector_middleware(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        called["count"] += 1
        return None

    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware", fake_run_corrector_middleware
    )
    monkeypatch.setattr(
        "core.action_parser.run_corrector_middleware", fake_run_corrector_middleware
    )

    failed_actions = [
        {
            "index": 0,
            "action": {
                "type": "tts_speak",
                "payload": {
                    "text": "hello there",
                    "__auto_injected": True,
                    "__merged_text": "hello there",
                },
            },
            "errors": ["action failed"],
            "result_reason": "tts_failed_fallback_sent",
        }
    ]

    original_message = SimpleNamespace(from_cortex=True, chat_id=999, thread_id=None)

    await _request_selective_correction(
        failed_actions=failed_actions,
        successful_actions=[
            {
                "type": "create_personal_diary_entry",
                "payload": {"interaction_summary": "x"},
            }
        ],
        bot=None,
        context={"interface": "telegram"},
        original_message=original_message,
    )

    assert called["count"] == 0, (
        "corrector middleware must not run for an auto-injected tts_speak "
        "whose text-only fallback already delivered the reply"
    )


@pytest.mark.asyncio
async def test_request_selective_correction_keeps_other_failures(monkeypatch):
    """A genuinely-failed non-TTS action must still reach the corrector even
    when a delivered auto-injected tts_speak failure is also present."""
    called = {"count": 0, "context": None}

    async def fake_run_corrector_middleware(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        called["count"] += 1
        called["context"] = context
        return None

    monkeypatch.setattr(
        "core.transport_layer.run_corrector_middleware", fake_run_corrector_middleware
    )
    monkeypatch.setattr(
        "core.action_parser.run_corrector_middleware", fake_run_corrector_middleware
    )

    failed_actions = [
        {
            "index": 0,
            "action": {
                "type": "tts_speak",
                "payload": {"text": "hi", "__auto_injected": True},
            },
            "errors": ["action failed"],
            "result_reason": "tts_failed_fallback_sent",
        },
        {
            "index": 1,
            "action": {"type": "message_send", "payload": {"text": "hi"}},
            "errors": [
                "Unsupported type 'message_send' - no plugin or interface found to handle it"
            ],
        },
    ]

    original_message = SimpleNamespace(from_cortex=True, chat_id=999, thread_id=None)

    await _request_selective_correction(
        failed_actions=failed_actions,
        successful_actions=[
            {
                "type": "create_personal_diary_entry",
                "payload": {"interaction_summary": "x"},
            }
        ],
        bot=None,
        context={"interface": "telegram"},
        original_message=original_message,
    )

    assert called["count"] == 1
    cc = called.get("context", {}).get("correction_context", {})
    failed_types = [
        f.get("action", {}).get("type")
        for f in cc.get("failed_actions", [])
        if isinstance(f, dict)
    ]
    assert "message_send" in failed_types
    assert "tts_speak" not in failed_types


def test_is_delivered_auto_tts_failure_detection():
    """Structural detection: only auto-injected tts_speak with the fallback-sent
    result reason counts as already-delivered."""
    base = {
        "index": 0,
        "action": {
            "type": "tts_speak",
            "payload": {"text": "hi", "__auto_injected": True},
        },
        "errors": ["action failed"],
        "result_reason": "tts_failed_fallback_sent",
    }
    assert _is_delivered_auto_tts_failure(dict(base)) is True
    # Model-emitted tts_speak (no __auto_injected) must NOT be suppressed.
    no_auto_flag = dict(base)
    no_auto_flag["action"] = {"type": "tts_speak", "payload": {"text": "hi"}}
    assert _is_delivered_auto_tts_failure(no_auto_flag) is False
    # Fallback NOT sent (e.g. VOX_FALLBACK_TO_TEXT off) must NOT be suppressed.
    no_fallback = dict(base)
    no_fallback["result_reason"] = "tts_failed_no_fallback"
    assert _is_delivered_auto_tts_failure(no_fallback) is False
    # A non-TTS failure must never be suppressed.
    other = dict(base)
    other["action"] = {"type": "message_telegram_bot", "payload": {"text": "hi"}}
    assert _is_delivered_auto_tts_failure(other) is False
    # Missing result_reason must not be suppressed.
    missing_reason = dict(base)
    missing_reason.pop("result_reason")
    assert _is_delivered_auto_tts_failure(missing_reason) is False
    # Non-dict failed item is never suppressed.
    assert _is_delivered_auto_tts_failure(None) is False
    assert _is_delivered_auto_tts_failure("boom") is False
