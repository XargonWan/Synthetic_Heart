import pytest

from core.action_parser import run_actions
from types import SimpleNamespace
from core.config_manager import config_registry


@pytest.mark.asyncio
async def test_llm_action_safe_false_blocked_by_default(monkeypatch):
    # Setup: ensure synth mode is 'suggest' and global override false
    await config_registry.set_value("SYNTH_AUTONOMY_MODE", "suggest")
    await config_registry.set_value("LLM_AUTO_EXECUTE_UNSAFE_ACTIONS", False)

    # Action from LLM
    original_message = SimpleNamespace()
    original_message.from_cortex = True
    original_message.chat_id = -1

    action = {
        "type": "create_personal_diary_entry",
        "payload": {"content": "x"},
        "safe": False,
    }

    import core.action_parser as ap

    ap._ACTION_PLUGINS = []
    monkeypatch.setattr(
        ap, "get_supported_action_types", lambda: {"create_personal_diary_entry"}
    )

    res = await run_actions(
        [action], context={}, bot=None, original_message=original_message
    )

    # Should not execute (blocked/treated as proposal)
    assert res["processed"] == []
    assert len(res["failed_actions"]) == 1
    assert (
        "unsafe" in res["failed_actions"][0]["errors"][0].lower()
        or "proposal" in res["failed_actions"][0]["errors"][0].lower()
    )


@pytest.mark.asyncio
async def test_llm_action_safe_true_allowed_in_suggest_mode_if_low(monkeypatch):
    # With new security-level default=low, LLM-originated low-security actions should be allowed
    await config_registry.set_value("SYNTH_AUTONOMY_MODE", "suggest")
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

    # Monkeypatch plugin discovery to return our fake diary for create_personal_diary_entry
    import core.action_parser as ap

    ap._ACTION_PLUGINS = [FakeDiary()]
    monkeypatch.setattr(
        ap, "get_supported_action_types", lambda: {"create_personal_diary_entry"}
    )

    action = {
        "type": "create_personal_diary_entry",
        "payload": {"content": "x"},
        "safe": True,
    }

    res = await run_actions(
        [action], context={}, bot=None, original_message=original_message
    )
    # With default low-security, action should be executed even in 'suggest' mode
    assert executed.get("ok", False) is True
    assert len(res["failed_actions"]) == 0
    assert len(res["processed"]) == 1


@pytest.mark.asyncio
async def test_autonomous_mode_executes_safe(monkeypatch):
    await config_registry.set_value("SYNTH_AUTONOMY_MODE", "autonomous")
    await config_registry.set_value("AUTONOMY_ALLOWED_ACTIONS", [])  # empty = allow all

    original_message = SimpleNamespace()
    original_message.from_cortex = True

    # Patch DiaryPlugin.execute_action to be a no-op that records execution
    executed = {}

    class FakeDiary:
        @staticmethod
        def execute_action(action, context, bot, original_message):
            executed["ok"] = True

        @staticmethod
        def get_supported_action_types():
            return ["create_personal_diary_entry"]

    # Monkeypatch plugin discovery to return our fake diary for create_personal_diary_entry
    import core.action_parser as ap

    ap._ACTION_PLUGINS = [FakeDiary()]
    monkeypatch.setattr(
        ap, "get_supported_action_types", lambda: {"create_personal_diary_entry"}
    )

    action = {
        "type": "create_personal_diary_entry",
        "payload": {"content": "x"},
        "safe": True,
    }

    res = await run_actions(
        [action], context={}, bot=None, original_message=original_message
    )

    assert executed.get("ok", False) is True
    assert len(res["failed_actions"]) == 0
    assert len(res["processed"]) == 1
