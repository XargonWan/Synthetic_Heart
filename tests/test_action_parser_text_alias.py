from core.action_parser import _normalize_text_field_alias, _normalize_payload


def test_normalize_text_field_alias_renames_message_text():
    """Regression: local models emit {"message_text": "..."} instead of
    {"text": "..."} for message_* actions. This is valid JSON, not a parse
    error, so it used to round-trip through a full LLM correction call just to
    rename a key. It must be fixed inline instead."""
    payload = {
        "message_text": "Oh really? Then I guess I have to find a way...",
        "interface_path": "telegram_bot/5208932647",
    }

    _normalize_text_field_alias("message_telegram_bot", payload)

    assert payload == {
        "text": "Oh really? Then I guess I have to find a way...",
        "interface_path": "telegram_bot/5208932647",
    }


def test_normalize_text_field_alias_leaves_existing_text_untouched():
    payload = {"text": "already correct", "message_text": "should be ignored"}

    _normalize_text_field_alias("message_telegram_bot", payload)

    assert payload["text"] == "already correct"
    assert payload["message_text"] == "should be ignored"


def test_normalize_text_field_alias_ignores_non_message_actions():
    payload = {"message_text": "not a message action"}

    _normalize_text_field_alias("create_personal_diary_entry", payload)

    assert "text" not in payload
    assert payload["message_text"] == "not a message action"


def test_normalize_text_field_alias_ignores_empty_alias_values():
    payload = {"message_text": "   ", "content": ""}

    _normalize_text_field_alias("message_discord_bot", payload)

    assert "text" not in payload


def test_normalize_payload_applies_text_alias_for_message_actions():
    """_normalize_payload is the entry point action_parser calls before
    validation on every parse attempt (not just corrections) — verify the
    alias rename is actually wired in there."""
    payload = {"message_text": "hi there", "chat_id": "123"}

    _normalize_payload("message_matrix_chat", payload)

    assert payload["text"] == "hi there"
    assert payload["chat_id"] == 123  # existing numeric coercion still runs
