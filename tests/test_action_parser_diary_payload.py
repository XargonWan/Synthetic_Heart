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


@pytest.mark.asyncio
async def test_diary_forwards_thread_id_from_message(monkeypatch):
    captured = {}

    fake_diary_module = types.ModuleType("plugins.ai_diary")

    def _fake_is_plugin_enabled():
        return True

    def _fake_create_personal_diary_entry(**kwargs):
        captured.update(kwargs)

    fake_diary_module.is_plugin_enabled = _fake_is_plugin_enabled
    fake_diary_module.create_personal_diary_entry = _fake_create_personal_diary_entry
    monkeypatch.setitem(sys.modules, "plugins.ai_diary", fake_diary_module)

    actions = [{"type": "message_telegram_bot", "payload": {"text": "Ciao"}}]
    ctx = {
        "interface": "telegram_bot",
        "thread_id": "77",
        "payload_thread_id": "88",
    }
    msg = SimpleNamespace(chat_id=123, text="Ping", message_thread_id=42)

    await ap._create_diary_entry_for_actions(actions, ctx, msg)

    assert captured["thread_id"] == "42"


@pytest.mark.asyncio
async def test_diary_uses_llm_rationale_emotions_and_message_id_for_grillo(monkeypatch):
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
            "type": "update_emotion_state",
            "payload": {"emotions": {"neutral": 0.5, "love": 0.2}},
        },
        {
            "type": "message_telegram_bot",
            "payload": {"text": "How have you been holding up?"},
        },
    ]
    ctx = {
        "interface": "telegram_bot",
        "grillo_beat": True,
        "beat_type": "outreach",
        "llm_response_text": '{"actions": [{"type": "update_emotion_state", "payload": {"emotions": {"neutral": 0.5, "love": 0.2}}}, {"type": "message_telegram_bot", "payload": {"text": "How have you been holding up?"}}], "meta": {"rationale": "Initiating a natural check-in while feeling quietly attached."}}',
    }
    msg = SimpleNamespace(
        chat_id=123,
        text="[G.R.I.L.L.O. OUTREACH]",
        message_id="grillo_outreach_6364",
    )

    await ap._create_diary_entry_for_actions(actions, ctx, msg)

    assert captured["grillo_activity_log_id"] == 6364
    assert captured["personal_thought"] == (
        "Initiating a natural check-in while feeling quietly attached."
    )
    assert captured["emotions"] == [
        {"type": "neutral", "intensity": 0.5},
        {"type": "love", "intensity": 0.2},
    ]


@pytest.mark.asyncio
async def test_diary_skips_internal_diary_consolidation(monkeypatch):
    captured = {"called": False}

    fake_diary_module = types.ModuleType("plugins.ai_diary")

    def _fake_is_plugin_enabled():
        return True

    def _fake_create_personal_diary_entry(**kwargs):
        captured["called"] = True

    fake_diary_module.is_plugin_enabled = _fake_is_plugin_enabled
    fake_diary_module.create_personal_diary_entry = _fake_create_personal_diary_entry
    monkeypatch.setitem(sys.modules, "plugins.ai_diary", fake_diary_module)

    actions = [{"type": "update_diary_entry", "payload": {"id": 1}}]
    ctx = {"interface": "diary_merge", "beat_type": "diary_consolidation"}
    msg = SimpleNamespace(chat_id=123, text="merge")

    await ap._create_diary_entry_for_actions(actions, ctx, msg)

    assert captured["called"] is False
