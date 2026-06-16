from types import SimpleNamespace
from typing import Any

import pytest

from core.debrief import run_debrief
from plugins.debrief_action_intent import DebriefActionIntentPlugin


def _plugin_config_value(
    key: str,
    default: Any = None,
    value_type: Any = None,
    **_: Any,
) -> Any:
    overrides = {
        "ACTION_INTENT_DEBRIEF_ENABLED": True,
        "ACTION_INTENT_ALLOW_MESSAGE_ACTIONS": False,
        "ACTION_INTENT_MAX_ACTIONS": 3,
        "ACTION_INTENT_PROACTIVE_ENABLED": True,
    }
    return overrides.get(key, default)


@pytest.mark.asyncio
async def test_debrief_action_intent_uses_corrector_for_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = DebriefActionIntentPlugin()
    monkeypatch.setattr(
        "plugins.debrief_action_intent.config_registry.get_value",
        _plugin_config_value,
    )

    from core.core_initializer import core_initializer

    core_initializer.actions_block = {
        "available_actions": {
            "schedule_message": {
                "schema": {
                    "required": ["text", "send_in"],
                    "properties": {"text": {}, "send_in": {}, "send_at": {}},
                }
            },
            "message_telegram_bot": {
                "schema": {
                    "required": ["text"],
                    "properties": {"text": {}, "interface_path": {}},
                }
            },
        }
    }

    monkeypatch.setattr(
        "core.action_schema_converter.normalize_action_schema",
        lambda action_name, action_def: action_def,
    )
    monkeypatch.setattr(
        "core.action_schema_converter.extract_for_llm_prompt",
        lambda action_name, normalized: normalized,
    )

    class DummyEngine:
        async def generate_response(self, messages: list[dict[str, Any]]) -> str:
            return "{not valid json"

    class DummyRegistry:
        def get_engine(self, name: str) -> DummyEngine:
            return DummyEngine()

        def load_engine(self, name: str) -> DummyEngine:
            return DummyEngine()

    async def fake_active_cortex_engine(scope: Any = None) -> str:
        return "dummy"

    called: dict[str, Any] = {}

    async def fake_corrector(
        text: str,
        bot: Any = None,
        context: dict[str, Any] | None = None,
        chat_id: Any = None,
        thread_id: Any = None,
    ) -> str:
        called["text"] = text
        called["context"] = context
        called["chat_id"] = chat_id
        called["thread_id"] = thread_id
        return '{"actions":[{"type":"schedule_message","payload":{"text":"Promemoria","send_in":"1 day"}}]}'

    monkeypatch.setattr("core.config.derive_cortex_scope", lambda ctx: "base")
    monkeypatch.setattr(
        "core.config.get_active_cortex_engine", fake_active_cortex_engine
    )
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: DummyRegistry()
    )
    monkeypatch.setattr(
        "plugins.debrief_action_intent.run_corrector_middleware", fake_corrector
    )

    original_message = SimpleNamespace(
        text="Ricordamelo domani",
        chat_id=42,
        thread_id=7,
        interface_path="telegram/42",
        from_cortex=True,
    )
    context = {
        "from_cortex": True,
        "original_user_message": "Ricordamelo domani",
        "llm_response_text": "Va bene, ti scrivo domani.",
        "interface": "telegram",
        "interface_path": "telegram/42",
    }

    result = await plugin.on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={"llm_response_text": "Va bene, ti scrivo domani."},
        context=context,
        original_message=original_message,
    )

    assert called["chat_id"] == 42
    assert called["thread_id"] == 7
    assert called["context"]["original_user_message"] == "Ricordamelo domani"
    assert called["context"]["allowed_action_types"] == ["schedule_message"]
    assert called["context"]["message"].interface_path == "telegram/42"
    assert result == {
        "recovery_actions": [
            {
                "action_type": "schedule_message",
                "payload": {"text": "Promemoria", "send_in": "1 day"},
                "reason": "debrief_intent",
                "confidence": "medium",
            }
        ]
    }


@pytest.mark.asyncio
async def test_run_debrief_preserves_context_for_auto_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePlugin:
        def on_debrief(
            self,
            processed_actions: list[dict[str, Any]],
            failed_actions: list[dict[str, Any]],
            results: dict[str, Any],
            context: dict[str, Any],
            original_message: Any,
        ) -> dict[str, Any]:
            return {
                "recovery_actions": [
                    {
                        "action_type": "schedule_message",
                        "payload": {"text": "Promemoria", "send_in": "1 day"},
                    }
                ]
            }

    def fake_get_var(key: str, default: Any = None, **_: Any) -> Any:
        if key == "ENABLE_DEBRIEF":
            return True
        return default

    def fake_get_value(
        key: str,
        default: Any = None,
        value_type: Any = None,
        **_: Any,
    ) -> Any:
        if key == "AUTO_RECOVERY_POLICY":
            return "auto_always"
        return default

    captured: dict[str, Any] = {}

    async def fake_run_actions(
        actions: list[dict[str, Any]],
        context: dict[str, Any],
        bot: Any,
        original_message: Any,
    ) -> dict[str, Any]:
        captured["actions"] = actions
        captured["context"] = context
        captured["original_message"] = original_message
        return {"processed": actions, "failed_actions": [], "errors": []}

    monkeypatch.setattr("core.debrief.config_registry.get_var", fake_get_var)
    monkeypatch.setattr("core.debrief.config_registry.get_value", fake_get_value)
    monkeypatch.setattr(
        "core.core_initializer.PLUGIN_REGISTRY", {"fake_debrief": FakePlugin()}
    )
    monkeypatch.setattr("core.action_parser.run_actions", fake_run_actions)

    original_message = SimpleNamespace(
        text="Ricordamelo domani",
        chat_id=101,
        thread_id=202,
        interface_path="telegram/101",
        from_cortex=True,
    )
    context = {
        "from_cortex": True,
        "original_user_message": "Ricordamelo domani",
        "llm_response_text": "Va bene, ti scrivo domani.",
        "allowed_action_types": ["schedule_message"],
        "interface": "telegram",
        "interface_path": "telegram/101",
    }

    await run_debrief(
        processed_actions=[
            {
                "type": "message_telegram_bot",
                "payload": {"text": "Va bene", "interface_path": "telegram/101"},
            }
        ],
        failed_actions=[],
        results={"llm_response_text": "Va bene, ti scrivo domani."},
        context=context,
        original_message=original_message,
    )

    assert captured["actions"] == [
        {
            "type": "schedule_message",
            "payload": {"text": "Promemoria", "send_in": "1 day"},
        }
    ]
    assert captured["context"]["from_debrief"] is True
    assert captured["context"]["debrief_ran"] is True
    assert captured["context"]["debrief_depth"] == 1
    assert captured["context"]["from_cortex"] is True
    assert captured["context"]["chat_id"] == 101
    assert captured["context"]["thread_id"] == 202
    assert captured["context"]["interface_path"] == "telegram/101"
    assert captured["context"]["original_user_message"] == "Ricordamelo domani"
    assert captured["original_message"] is original_message
