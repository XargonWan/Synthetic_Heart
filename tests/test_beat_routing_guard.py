"""Tests for the Grillo beat routing guard in core.message_chain.

These verify that observer/outreach beats cannot deliver a ``message_*`` action
to a conversation the beat never offered the model (the Volpino wrong-chat
misrouting regression).
"""

from core.message_chain import (
    _collect_beat_allowed_paths,
    _drop_misrouted_beat_actions,
)


def test_collect_allowed_paths_none_when_not_a_beat():
    assert _collect_beat_allowed_paths(None) is None
    assert _collect_beat_allowed_paths({}) is None
    assert _collect_beat_allowed_paths({"grillo_beat": False}) is None


def test_collect_allowed_paths_from_snippets_and_targets():
    ctx = {
        "grillo_beat": True,
        "beat_type": "observer",
        "grillo_snippets": [
            "(chat:telegram_bot/-1002646330049/4 | sender:Volpino | 2026-07-09T07:32:48Z) gioco?",
            "(chat:telegram_bot/-100999 | sender:alice | 2026-07-09T07:00:00Z) hi",
        ],
        "grillo_targets": [
            {"interface_path": "telegram_bot/-1002634148259"},
            {"interface_path": "telegram_bot/-100888"},
        ],
    }
    allowed = _collect_beat_allowed_paths(ctx)
    assert allowed == {
        "telegram_bot/-1002646330049/4",
        "telegram_bot/-100999",
        "telegram_bot/-1002634148259",
        "telegram_bot/-100888",
    }


def test_collect_allowed_paths_empty_returns_none():
    ctx = {"grillo_beat": True, "grillo_snippets": [], "grillo_targets": []}
    assert _collect_beat_allowed_paths(ctx) is None


def test_drop_misrouted_action_to_unoffered_chat():
    ctx = {
        "grillo_beat": True,
        "beat_type": "observer",
        "grillo_snippets": [
            "(chat:telegram_bot/-1002646330049/4 | sender:Volpino | 2026-07-09T07:32:48Z) gioco?",
        ],
        "grillo_targets": [],
    }
    actions = [
        {
            "type": "message_telegram_bot",
            "payload": {
                "text": "Uh, aspetta un attimo Volpino!",
                "interface_path": "telegram_bot/-1002634148259",
            },
        }
    ]
    kept = _drop_misrouted_beat_actions(actions, ctx)
    assert kept == []


def test_keep_action_routed_to_snippet_origin():
    ctx = {
        "grillo_beat": True,
        "beat_type": "observer",
        "grillo_snippets": [
            "(chat:telegram_bot/-1002646330049/4 | sender:Volpino | 2026-07-09T07:32:48Z) gioco?",
        ],
        "grillo_targets": [],
    }
    actions = [
        {
            "type": "message_telegram_bot",
            "payload": {
                "text": "Ciao Volpino!",
                "interface_path": "telegram_bot/-1002646330049/4",
            },
        }
    ]
    kept = _drop_misrouted_beat_actions(actions, ctx)
    assert kept == actions


def test_keep_action_routed_to_eligible_target():
    ctx = {
        "grillo_beat": True,
        "beat_type": "observer",
        "grillo_snippets": [],
        "grillo_targets": [{"interface_path": "telegram_bot/-100888"}],
    }
    actions = [
        {
            "type": "message_telegram_bot",
            "payload": {
                "text": "hey",
                "interface_path": "telegram_bot/-100888",
            },
        }
    ]
    kept = _drop_misrouted_beat_actions(actions, ctx)
    assert kept == actions


def test_non_beat_actions_untouched():
    ctx = {"interface_path": "telegram_bot/123", "chat_id": 123}
    actions = [
        {
            "type": "message_telegram_bot",
            "payload": {
                "text": "hi",
                "interface_path": "telegram_bot/999",
            },
        }
    ]
    kept = _drop_misrouted_beat_actions(actions, ctx)
    assert kept == actions


def test_non_message_actions_kept():
    ctx = {
        "grillo_beat": True,
        "beat_type": "observer",
        "grillo_snippets": [
            "(chat:telegram_bot/-100777 | sender:x | 2026-07-09T00:00:00Z) hey",
        ],
        "grillo_targets": [],
    }
    actions = [
        {"type": "create_personal_diary_entry", "payload": {"content": "thought"}},
    ]
    kept = _drop_misrouted_beat_actions(actions, ctx)
    assert kept == actions


def test_action_without_interface_path_is_left_for_downstream():
    ctx = {
        "grillo_beat": True,
        "beat_type": "observer",
        "grillo_snippets": [
            "(chat:telegram_bot/-100777 | sender:x | 2026-07-09T00:00:00Z) hey",
        ],
        "grillo_targets": [],
    }
    actions = [
        {"type": "message_telegram_bot", "payload": {"text": "hi"}},
    ]
    kept = _drop_misrouted_beat_actions(actions, ctx)
    assert kept == actions
