"""Tests for the Grillo beat routing guard in core.message_chain.

These verify that observer/outreach beats cannot deliver a ``message_*`` action
to a conversation the beat never offered the model (the Volpino wrong-chat
misrouting regression).
"""

from core.message_chain import (
    _collect_beat_allowed_paths,
    _drop_misrouted_beat_actions,
)
from core.prompt_engine import _derive_outbound_beat_target_interfaces


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


def test_web_search_delivery_scoped_to_human_origin():
    """A web_search_result delivery turn is hard-scoped to its originating
    interface_path: the report must land in the chat where the search was
    prompted, never redirected elsewhere.
    """
    ctx = {
        "grillo_beat": True,
        "beat_type": "web_search_result",
        "interface_path": "telegram_bot/-1003098886330/4297",
        "web_search_task_id": "t1",
    }
    assert _collect_beat_allowed_paths(ctx) == {"telegram_bot/-1003098886330/4297"}

    # A message routed to a DIFFERENT chat is dropped (would be misrouted spam).
    actions = [
        {
            "type": "message_telegram_bot",
            "payload": {
                "text": "ecco i risultati",
                "interface_path": "telegram_bot/-100999999999",
            },
        }
    ]
    assert _drop_misrouted_beat_actions(actions, ctx) == []

    # A message routed to the originating chat is kept.
    kept = _drop_misrouted_beat_actions(
        [
            {
                "type": "message_telegram_bot",
                "payload": {
                    "text": "ecco i risultati",
                    "interface_path": "telegram_bot/-1003098886330/4297",
                },
            }
        ],
        ctx,
    )
    assert kept == [
        {
            "type": "message_telegram_bot",
            "payload": {
                "text": "ecco i risultati",
                "interface_path": "telegram_bot/-1003098886330/4297",
            },
        }
    ]


def test_web_search_delivery_scoped_to_internal_origin_blocks_outward():
    """A self-initiated Grillo search (origin ``grillo/-1``, no human requester)
    must NOT deliver to any outward chat — otherwise the report is spammed into
    conversations pulled from the observer snippets (observed regression).
    """
    ctx = {
        "grillo_beat": True,
        "beat_type": "web_search_result",
        "interface_path": "grillo/-1",
        "web_search_task_id": "t2",
    }
    assert _collect_beat_allowed_paths(ctx) == {"grillo/-1"}

    actions = [
        {
            "type": "message_discord_bot",
            "payload": {
                "text": "Guarda cosa ho trovato",
                "interface_path": "discord_bot/1465433261634224293/1466328972567646281",
            },
        },
        {"type": "create_personal_diary_entry", "payload": {"content": "thought"}},
    ]
    kept = _drop_misrouted_beat_actions(actions, ctx)
    # The outward Discord message is dropped; the non-message diary action is kept.
    assert kept == [
        {"type": "create_personal_diary_entry", "payload": {"content": "thought"}}
    ]


def test_web_search_delivery_without_origin_is_not_scoped():
    ctx = {
        "grillo_beat": True,
        "beat_type": "web_search_result",
        "web_search_task_id": "t3",
    }
    assert _collect_beat_allowed_paths(ctx) is None


def test_derive_outbound_interfaces_from_prior_context():
    """A web_search_result beat must re-derive the real interfaces its
    originating grillo observer beat offered (nested under prior_context),
    so a registered interface like telegram_bot is not dropped as
    out-of-scope and the search answer is actually delivered.
    """
    ctx = {
        "grillo_beat": True,
        "beat_type": "web_search_result",
        "interface_path": "grillo/-1",
        "web_search_task_id": "t1",
        "prior_context": {
            "grillo_beat": True,
            "beat_type": "observer",
            "grillo_snippets": [
                "(chat:telegram_bot/-1003098886330/4297 | sender:Jay Cheshire | 2m) si dai prova a cercare",
            ],
            "grillo_targets": [{"interface_path": "telegram_bot/-1002646330049/574"}],
        },
    }
    outbound_interfaces = _derive_outbound_beat_target_interfaces(
        ctx, "web_search_result"
    )
    assert outbound_interfaces == {"telegram_bot"}


def test_outbound_targets_top_level_still_works():
    ctx = {
        "grillo_beat": True,
        "beat_type": "observer",
        "grillo_snippets": [
            "(chat:telegram_bot/-1002646330049/4 | sender:Volpino | 2026-07-09T07:32:48Z) gioco?",
        ],
    }
    assert _derive_outbound_beat_target_interfaces(ctx, "observer") == {"telegram_bot"}


def test_outbound_targets_ignores_non_outbound_beat():
    ctx = {
        "grillo_beat": True,
        "prior_context": {"grillo_snippets": ["(chat:telegram_bot/-1 ...)"]},
    }
    assert _derive_outbound_beat_target_interfaces(ctx, "chat") == set()


def test_outbound_targets_from_top_level_interface_path():
    """A web_search_result beat is addressed to its real target via the
    top-level interface_path (the beat runs under the synthetic web_search
    interface). Its prefix must be offered so message_telegram_bot is not
    dropped as out-of-scope.
    """
    ctx = {
        "grillo_beat": True,
        "beat_type": "web_search_result",
        "interface_path": "telegram_bot/-1003098886330/4297",
        "web_search_task_id": "t2",
        "prior_context": {"thread_id": 4297, "history_scope": "local"},
    }
    assert _derive_outbound_beat_target_interfaces(ctx, "web_search_result") == {
        "telegram_bot"
    }


def test_outbound_targets_from_interface_keyed_prior_context():
    """Direct-chat shape: prior_context may be an interface-keyed history map
    (key -> deque of messages). Keys that look like interface paths are offered
    structurally even when the top-level interface_path is synthetic.
    """
    from collections import deque

    ctx = {
        "grillo_beat": True,
        "beat_type": "web_search_result",
        "interface_path": "grillo/-1",
        "prior_context": {
            "telegram_bot/-1003098886330/4297": deque([{"message_id": 1}]),
            "thread_id": 4297,
        },
    }
    assert _derive_outbound_beat_target_interfaces(ctx, "web_search_result") == {
        "telegram_bot"
    }
