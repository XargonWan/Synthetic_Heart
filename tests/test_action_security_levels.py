import pytest
from types import SimpleNamespace
from core.config_manager import config_registry
import core.action_parser as ap
from core.core_initializer import core_initializer


@pytest.mark.asyncio
async def test_declared_high_security_blocked_in_suggest_mode(monkeypatch):
    # Setup env
    await config_registry.set_value("SYNTH_AUTONOMY_MODE", "suggest")
    original_message = SimpleNamespace()
    original_message.from_cortex = True

    # Monkeypatch actions_block to declare action as high security
    core_initializer.actions_block = {
        "available_actions": {
            "danger_action": {"security_level": "high", "external_effects": []}
        }
    }

    action = {"type": "danger_action", "payload": {}, "safe": True}

    # No plugin to execute; should be blocked by safety before execution
    res = await ap.run_actions(
        [action], context={}, bot=None, original_message=original_message
    )

    assert res["processed"] == []
    assert len(res["failed_actions"]) == 1
    assert "blocked" in res["failed_actions"][0]["errors"][0].lower()


@pytest.mark.asyncio
async def test_external_effects_increase_to_medium_and_block_in_suggest(monkeypatch):
    await config_registry.set_value("SYNTH_AUTONOMY_MODE", "suggest")
    original_message = SimpleNamespace()
    original_message.from_cortex = True

    core_initializer.actions_block = {
        "available_actions": {
            "maybe_network": {"security_level": "low", "external_effects": ["http"]}
        }
    }

    action = {"type": "maybe_network", "payload": {}, "safe": True}

    res = await ap.run_actions(
        [action], context={}, bot=None, original_message=original_message
    )

    assert res["processed"] == []
    assert len(res["failed_actions"]) == 1
    assert "blocked" in res["failed_actions"][0]["errors"][0].lower()


@pytest.mark.asyncio
async def test_grillo_allows_medium_if_configured(monkeypatch):
    # Grillo configured to allow medium
    await config_registry.set_value("GRILLO_ALLOWED_SECURITY_LEVEL", "medium")
    original_message = SimpleNamespace()
    original_message.from_cortex = True

    core_initializer.actions_block = {
        "available_actions": {
            "dream_diary": {"security_level": "medium", "external_effects": []}
        }
    }

    executed = {}

    class FakeDiary:
        @staticmethod
        def execute_action(action, context, bot, original_message):
            executed["ok"] = True

        @staticmethod
        def get_supported_action_types():
            return ["dream_diary"]

    ap._ACTION_PLUGINS = [FakeDiary()]
    ap.get_supported_action_types = lambda: set(["dream_diary"])

    res = await ap.run_actions(
        [{"type": "dream_diary", "payload": {}}],
        context={"grillo_beat": True},
        bot=None,
        original_message=original_message,
    )

    assert executed.get("ok", False) is True
    assert len(res["failed_actions"]) == 0
    assert len(res["processed"]) == 1
