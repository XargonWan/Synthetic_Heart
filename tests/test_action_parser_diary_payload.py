import sys
import types
from types import SimpleNamespace

import pytest

import core.action_parser as ap


@pytest.mark.asyncio
async def test_diary_uses_llm_payload_for_thoughts_and_emotions(monkeypatch):
    captured = {}

    fake_diary_module = types.ModuleType("plugins.ai_diary")

    def _fake_is_plugin_enabled():
        return True

    def _fake_create_personal_diary_entry(**kwargs):
        captured.update(kwargs)

    fake_diary_module.is_plugin_enabled = _fake_is_plugin_enabled
    fake_diary_module.create_personal_diary_entry = _fake_create_personal_diary_entry
    monkeypatch.setitem(sys.modules, "plugins.ai_diary", fake_diary_module)

    actions = [
        {
            "type": "create_personal_diary_entry",
            "payload": {
                "interaction_summary": "Discussione emotiva con tensione relazionale",
                "personal_thought": "Mi sento in conflitto e voglio riparare la fiducia",
                "emotions": [{"type": "conflicted", "intensity": 7}],
                "context_tags": ["relationship", "conflict"],
            },
        },
        {
            "type": "message_telegram_bot",
            "payload": {"text": "Capisco il tuo disagio, non volevo ferirti."},
        },
    ]

    ctx = {"interface": "telegram_bot", "participants": [{"usertag": "@alice"}]}
    msg = SimpleNamespace(chat_id=123, text="Mi hai ferito con quelle parole")

    await ap._create_diary_entry_for_actions(actions, ctx, msg)

    assert (
        captured["interaction_summary"]
        == "Discussione emotiva con tensione relazionale"
    )
    assert (
        captured["personal_thought"]
        == "Mi sento in conflitto e voglio riparare la fiducia"
    )
    assert captured["emotions"] == [{"type": "conflicted", "intensity": 7}]
    assert captured["context_tags"] == ["relationship", "conflict"]
