import pytest
from types import SimpleNamespace

from core import message_chain


@pytest.mark.asyncio
async def test_no_duplicate_message_when_second_correction_reeemits(monkeypatch):
    """CHANGELOG 2026-06-26: after a first correction delivers a message_* and
    a non-message action (use_animation) fails, the second correction must NOT
    re-deliver the message. The second correction's LLM output re-emits the
    message action; both the retry-payload filter AND the correction prompt
    must prevent a duplicate delivery."""

    deliveries: list[str] = []
    run_calls: list[list[str]] = []

    async def fake_run_actions(actions, ctx, bot, message):
        types = [
            a.get("type") or a.get("action")
            for a in (actions or [])
            if isinstance(a, dict)
        ]
        run_calls.append(types)
        processed = []
        failed = []
        for a in actions or []:
            atype = a.get("type")
            if atype == "message_telegram_bot":
                deliveries.append((a.get("payload") or {}).get("text", ""))
                processed.append(a)
            elif atype == "use_animation":
                failed.append({"action": a, "errors": ["invalid field"]})
        return {"processed": processed, "failed_actions": failed, "errors": []}

    corrector_calls = {"n": 0}

    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None, **kwargs
    ):
        corrector_calls["n"] += 1
        # The weak model ignores "do not repeat successful ones" and re-emits
        # the full response on every correction.
        return (
            '{"actions": ['
            '{"type": "message_telegram_bot", "payload": {"text": "REPLY", '
            '"interface_path": "telegram_bot/1"}}, '
            '{"type": "use_animation", "payload": {"animation_state": "think"}}]}'
        )

    def fake_extract_json(text, return_metadata=False):
        return (
            {
                "actions": [
                    {"type": "message_telegram_bot", "payload": {"text": "REPLY"}},
                    {"type": "use_animation", "payload": {"animation_state": "think"}},
                ]
            },
            {"had_errors": False, "recovered": False, "had_extra_text": False},
        )

    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)
    monkeypatch.setattr(
        "core.transport_layer.extract_json_from_text", fake_extract_json
    )
    monkeypatch.setattr("core.transport_layer.run_corrector_middleware", fake_corrector)
    monkeypatch.setattr(
        "core.action_parser.get_supported_action_types",
        lambda: {"message_telegram_bot", "use_animation"},
    )

    msg = SimpleNamespace(chat_id=1, interface_path="telegram_bot/1", from_cortex=True)
    await message_chain.handle_incoming_message(
        bot=None,
        message=msg,
        text='{"bad json',
        source="llm",
        context={"max_retries": 3},
    )

    # The reply must be delivered exactly once despite the double correction.
    assert deliveries == ["REPLY"], f"duplicate deliveries: {deliveries}"
    # The second run_actions call must not contain the already-delivered type.
    assert "message_telegram_bot" not in run_calls[-1]
