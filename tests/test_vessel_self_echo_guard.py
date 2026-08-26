"""Regression tests for the vessel self-echo (parrot) guard.

These cover the two pure helpers that back the structural anti-echo fix in
``core.message_chain``: ``_last_self_vessel_utterance`` (find Synth's own last
spoken line in the current vessel chat) and ``_build_missing_reply_hint`` (steer
the corrector retry toward the player's new message and away from repeating the
previous line). Detection is structural (author identity + exact equality) and
keyword-free, so the tests avoid any language-specific token.
"""

from __future__ import annotations

from collections import deque

from core.message_chain import (
    _build_missing_reply_hint,
    _last_self_vessel_utterance,
)


def test_last_self_vessel_utterance_returns_latest_self_line() -> None:
    interface_path = "vessel/minecraft"
    history = deque(
        [
            {"user_id": "player_1", "text": "first player line"},
            {"user_id": "self", "text": "older self line"},
            {"user_id": "player_1", "text": "second player line"},
            {"user_id": "self", "text": "latest self line"},
        ]
    )
    ctx = {interface_path: history}
    assert _last_self_vessel_utterance(ctx, interface_path) == "latest self line"


def test_last_self_vessel_utterance_accepts_sender_id_alias() -> None:
    interface_path = "vessel/minecraft"
    history = [{"sender_id": "self", "text": "spoken via sender_id"}]
    ctx = {interface_path: history}
    assert _last_self_vessel_utterance(ctx, interface_path) == "spoken via sender_id"


def test_last_self_vessel_utterance_none_when_no_self_line() -> None:
    interface_path = "vessel/minecraft"
    history = [
        {"user_id": "player_1", "text": "only players here"},
        {"user_id": "player_2", "text": "still no self"},
    ]
    ctx = {interface_path: history}
    assert _last_self_vessel_utterance(ctx, interface_path) is None


def test_last_self_vessel_utterance_none_on_empty_or_missing() -> None:
    assert _last_self_vessel_utterance({}, "vessel/minecraft") is None
    assert (
        _last_self_vessel_utterance({"vessel/minecraft": []}, "vessel/minecraft")
        is None
    )
    assert _last_self_vessel_utterance({"vessel/minecraft": deque()}, "") is None


def test_missing_reply_hint_vessel_names_correct_say_action() -> None:
    hint = _build_missing_reply_hint("vessel/minecraft", True)
    assert "vessel_minecraft_say" in hint
    assert "CHAT REPLY REQUIRED" in hint


def test_missing_reply_hint_includes_current_player_message() -> None:
    hint = _build_missing_reply_hint(
        "vessel/minecraft",
        True,
        current_player_message="come with me",
    )
    assert "come with me" in hint


def test_missing_reply_hint_forbids_repeating_last_self_line() -> None:
    hint = _build_missing_reply_hint(
        "vessel/minecraft",
        True,
        current_player_message="are you there",
        last_self_line="previous parroted line",
    )
    assert "previous parroted line" in hint
    assert "Do NOT repeat" in hint
    assert "are you there" in hint


def test_missing_reply_hint_non_vessel_unaffected_by_new_params() -> None:
    hint = _build_missing_reply_hint(
        "telegram_bot/123",
        False,
        current_player_message="ignored for non-vessel",
        last_self_line="also ignored",
    )
    assert "message_telegram_bot" in hint
    assert "ignored for non-vessel" not in hint
    assert "also ignored" not in hint
