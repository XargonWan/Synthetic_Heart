import pytest

from types import SimpleNamespace
from core.config_manager import config_registry
import core.action_parser as ap


@pytest.mark.asyncio
async def test_whitelisted_mode_enforces_allowed_list(monkeypatch):
    # Set mode to whitelisted and allowed_actions to contain only 'create_personal_diary_entry'
    await config_registry.set_value("SYNTH_AUTONOMY_MODE", "whitelisted")
    await config_registry.set_value(
        "AUTONOMY_ALLOWED_ACTIONS", ["create_personal_diary_entry"]
    )

    original_message = SimpleNamespace()
    original_message.from_cortex = True

    # Allowed action should execute (we patch plugin)
    executed = {}

    class FakeDiary:
        @staticmethod
        def execute_action(action, context, bot, original_message):
            executed["ok"] = True

        @staticmethod
        def get_supported_action_types():
            return ["create_personal_diary_entry"]

    ap._ACTION_PLUGINS = [FakeDiary()]
    monkeypatch.setattr(
        ap, "get_supported_action_types", lambda: {"create_personal_diary_entry"}
    )

    action = {
        "type": "create_personal_diary_entry",
        "payload": {"content": "x"},
        "safe": True,
    }
    res = await ap.run_actions(
        [action], context={}, bot=None, original_message=original_message
    )
    assert executed.get("ok", False) is True
    assert len(res["processed"]) == 1

    # Non-whitelisted action should be blocked
    executed.clear()
    action2 = {"type": "message_telegram_bot", "payload": {"text": "hi"}, "safe": True}
    # Ensure system knows this action type exists
    monkeypatch.setattr(
        ap,
        "get_supported_action_types",
        lambda: {"create_personal_diary_entry", "message_telegram_bot"},
    )
    # No plugin for message_telegram_bot in our fake plugin list -> will be blocked as unsupported
    res2 = await ap.run_actions(
        [action2], context={}, bot=None, original_message=original_message
    )
    assert res2["processed"] == [] or len(res2["failed_actions"]) == 1


@pytest.mark.asyncio
async def test_autonomous_mode_ignores_whitelist(monkeypatch):
    # Set mode to autonomous and allowed_actions to a non-matching list
    await config_registry.set_value("SYNTH_AUTONOMY_MODE", "autonomous")
    await config_registry.set_value("AUTONOMY_ALLOWED_ACTIONS", ["some_other_action"])

    original_message = SimpleNamespace()
    original_message.from_cortex = True

    executed = {}

    class FakeDiary:
        @staticmethod
        def execute_action(action, context, bot, original_message):
            executed["ok"] = True

        @staticmethod
        def get_supported_action_types():
            return ["create_personal_diary_entry"]

    ap._ACTION_PLUGINS = [FakeDiary()]
    monkeypatch.setattr(
        ap, "get_supported_action_types", lambda: {"create_personal_diary_entry"}
    )

    action = {
        "type": "create_personal_diary_entry",
        "payload": {"content": "x"},
        "safe": True,
    }
    res = await ap.run_actions(
        [action], context={}, bot=None, original_message=original_message
    )
    assert executed.get("ok", False) is True
    assert len(res["processed"]) == 1
