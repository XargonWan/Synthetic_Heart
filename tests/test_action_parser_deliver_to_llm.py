import pytest
from types import SimpleNamespace

from core.action_parser import run_actions
from core.config_manager import config_registry


@pytest.mark.asyncio
async def test_deliver_to_llm_result_captured_and_delivered(monkeypatch):
    """A plugin result tagged ``deliver_to_llm=True`` is captured into
    ``action_outputs`` and voiced via ``request_llm_delivery``.

    Covers fetch-only actions whose answer *is* the reply (e.g.
    ``recall_last_dream``): the plugin returns content but never sends, so the
    action parser must feed it back to the LLM for an in-character recount.
    """
    await config_registry.set_value("SYNTH_AUTONOMY_MODE", "autonomous")
    await config_registry.set_value("AUTONOMY_ALLOWED_ACTIONS", [])  # allow all

    original_message = SimpleNamespace()
    original_message.from_cortex = True
    original_message.chat_id = 42
    original_message.message_id = 7
    original_message.interface_path = "telegram_bot/42"

    class FakeDream:
        @staticmethod
        def execute_action(action, context, bot, original_message):
            return {
                "status": "success",
                "dream_content": "a dream about a red cat",
                "message": "Recalled dream",
                "deliver_to_llm": True,
            }

        @staticmethod
        def get_supported_action_types():
            return ["recall_last_dream"]

    import core.action_parser as ap

    ap._ACTION_PLUGINS = [FakeDream()]
    monkeypatch.setattr(ap, "get_supported_action_types", lambda: {"recall_last_dream"})

    captured = {}

    async def fake_delivery(*, action_outputs, original_context, action_type):
        captured["action_outputs"] = action_outputs
        captured["action_type"] = action_type
        captured["original_context"] = original_context

    import core.auto_response as auto_response

    monkeypatch.setattr(auto_response, "request_llm_delivery", fake_delivery)

    action = {"type": "recall_last_dream", "payload": {}, "safe": True}
    res = await run_actions(
        [action],
        context={"interface": "telegram_bot"},
        bot=None,
        original_message=original_message,
    )

    # Captured into action_outputs, control flag stripped, real type stamped.
    assert len(res["action_outputs"]) == 1
    out = res["action_outputs"][0]
    assert out["type"] == "recall_last_dream"
    assert out["dream_content"] == "a dream about a red cat"
    assert "deliver_to_llm" not in out

    # Voiced via request_llm_delivery with the real action type (loop prevention),
    # not the hardcoded "terminal".
    assert captured["action_type"] == "recall_last_dream"
    assert captured["action_outputs"] == res["action_outputs"]


@pytest.mark.asyncio
async def test_result_without_deliver_to_llm_not_captured(monkeypatch):
    """A normal plugin result (no ``deliver_to_llm`` flag) is left untouched —
    the additive branch must not change existing action behaviour."""
    await config_registry.set_value("SYNTH_AUTONOMY_MODE", "autonomous")
    await config_registry.set_value("AUTONOMY_ALLOWED_ACTIONS", [])

    original_message = SimpleNamespace()
    original_message.from_cortex = True
    original_message.chat_id = 1

    class FakePlugin:
        @staticmethod
        def execute_action(action, context, bot, original_message):
            return {"status": "success", "dream_content": "ignored"}

        @staticmethod
        def get_supported_action_types():
            return ["recall_last_dream"]

    import core.action_parser as ap

    ap._ACTION_PLUGINS = [FakePlugin()]
    monkeypatch.setattr(ap, "get_supported_action_types", lambda: {"recall_last_dream"})

    delivered = {"called": False}

    async def fake_delivery(**kwargs):
        delivered["called"] = True

    import core.auto_response as auto_response

    monkeypatch.setattr(auto_response, "request_llm_delivery", fake_delivery)

    action = {"type": "recall_last_dream", "payload": {}, "safe": True}
    res = await run_actions(
        [action],
        context={"interface": "telegram_bot"},
        bot=None,
        original_message=original_message,
    )

    assert res["action_outputs"] == []
    assert delivered["called"] is False
    assert len(res["processed"]) == 1
