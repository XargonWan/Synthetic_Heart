from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import message_chain, transport_layer


@pytest.mark.asyncio
async def test_send_llm_fallback_records_primary_failure(monkeypatch) -> None:
    recorded = []

    async def fake_record_failure_entry(entry):
        recorded.append(entry)
        return 1

    async def fake_universal_send(send_fn, chat_id, **kwargs):
        return None

    monkeypatch.setattr(
        "core.llm_failure_log.record_failure_entry",
        fake_record_failure_entry,
    )
    monkeypatch.setattr(
        "core.transport_layer.universal_send",
        fake_universal_send,
    )
    monkeypatch.setattr(
        "core.animation_handler.get_karada_state_server",
        lambda: None,
    )

    bot = SimpleNamespace(send_message=AsyncMock(return_value=None))
    msg = SimpleNamespace(
        chat_id="sid",
        interface_path="synth_webui/sid",
        original_text='{"actions":[{"type":"message_unknown"}]}',
        correction_context={
            "errors": [
                "Unsupported type 'message_unknown' - no plugin or interface found to handle it"
            ]
        },
    )

    await message_chain.send_llm_fallback_message(
        bot,
        msg,
        failure_reason="Exhausted 2 correction attempts for invalid JSON",
        context={"interface_path": "synth_webui/sid"},
    )

    assert len(recorded) == 1
    assert recorded[0]["stage"] == "llm_fallback"
    assert recorded[0]["failure_code"] == "unsupported_action"


@pytest.mark.asyncio
async def test_universal_send_records_delivery_failure(monkeypatch) -> None:
    recorded = []

    async def fake_record_failure_entry(entry):
        recorded.append(entry)
        return 1

    async def failing_send_message(*args, **kwargs):
        raise RuntimeError("no active websocket for session sid")

    monkeypatch.setattr(
        "core.llm_failure_log.record_failure_entry",
        fake_record_failure_entry,
    )
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context",
        AsyncMock(return_value=None),
    )

    with pytest.raises(RuntimeError, match="no active websocket"):
        await transport_layer.universal_send(
            failing_send_message,
            "sid",
            text="hello from synth",
            interface_path="synth_webui/sid",
        )

    assert len(recorded) == 1
    assert recorded[0]["stage"] == "delivery"
    assert recorded[0]["failure_code"] == "delivery_failed"
    assert recorded[0]["interface_path"] == "synth_webui/sid"
