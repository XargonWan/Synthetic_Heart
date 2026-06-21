"""Regression tests for ``_normalize_payload`` numeric coercion.

Grammar-constrained local models (``force_action_grammar``) emit valid JSON
whose payload values may be quoted numbers — ``{"limit": "5"}`` instead of
``{"limit": 5}``. The grammar cannot constrain value types, so the action
parser normalizes them. These tests pin that behaviour, including the fields
that must *stay* strings.
"""

from __future__ import annotations

from core.action_parser import _normalize_payload


def test_numeric_param_string_coerced_to_int():
    payload = {"limit": "5"}
    _normalize_payload("get_recent_chats", payload)
    assert payload["limit"] == 5
    assert isinstance(payload["limit"], int)


def test_numeric_param_decimal_coerced_to_float():
    payload = {"intensity": "0.9"}
    _normalize_payload("set_emotion", payload)
    assert payload["intensity"] == 0.9
    assert isinstance(payload["intensity"], float)


def test_id_field_coerced_to_int():
    payload = {"chat_id": "5208932647", "thread_id": "2"}
    _normalize_payload("message_telegram_bot", payload)
    assert payload["chat_id"] == 5208932647
    assert payload["thread_id"] == 2


def test_string_typed_target_left_unchanged():
    # Telegram target is a numeric-looking string that must stay a string.
    payload = {"text": "Hello", "target": "-1003098886330"}
    _normalize_payload("message_telegram_bot", payload)
    assert payload["target"] == "-1003098886330"
    assert isinstance(payload["target"], str)


def test_already_numeric_and_non_numeric_left_unchanged():
    payload = {"limit": 5, "older_than_days": "soon", "animation_state": "think"}
    _normalize_payload("cleanup_old_chats", payload)
    assert payload["limit"] == 5
    assert payload["older_than_days"] == "soon"
    assert payload["animation_state"] == "think"


def test_nested_dict_and_list_recursion():
    payload = {
        "target": {"chat_id": "42", "thread_id": "7"},
        "batch": [{"limit": "3"}, {"limit": "4"}],
    }
    _normalize_payload("fan_out", payload)
    assert payload["target"]["chat_id"] == 42
    assert payload["target"]["thread_id"] == 7
    assert payload["batch"][0]["limit"] == 3
    assert payload["batch"][1]["limit"] == 4


def test_non_finite_and_scientific_not_coerced():
    payload = {"limit": "inf", "offset": "nan", "count": "1e3"}
    _normalize_payload("noop", payload)
    assert payload["limit"] == "inf"
    assert payload["offset"] == "nan"
    assert payload["count"] == "1e3"
