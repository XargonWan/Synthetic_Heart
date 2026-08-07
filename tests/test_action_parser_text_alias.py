from core.action_parser import (
    _normalize_text_field_alias,
    _normalize_payload,
    _normalize_vessel_payload_alias,
)


def test_normalize_text_field_alias_renames_message_text():
    """Regression: local models emit {"message_text": "..."} instead of
    {"text": "..."} for message_* actions. This is valid JSON, not a parse
    error, so it used to round-trip through a full LLM correction call just to
    rename a key. It must be fixed inline instead."""
    payload = {
        "message_text": "Oh really? Then I guess I have to find a way...",
        "interface_path": "telegram_bot/5551234567",
    }

    _normalize_text_field_alias("message_telegram_bot", payload)

    assert payload == {
        "text": "Oh really? Then I guess I have to find a way...",
        "interface_path": "telegram_bot/5551234567",
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


def test_normalize_vessel_craft_recipe_to_item():
    payload = {"recipe": "wooden_pickaxe", "count": "1"}

    _normalize_vessel_payload_alias("vessel_minecraft_craft", payload)

    assert payload["item"] == "wooden_pickaxe"
    assert "recipe" not in payload


def test_normalize_vessel_collect_block_to_name():
    payload = {"block": "oak_log", "amount": "3"}

    _normalize_vessel_payload_alias("vessel_minecraft_collect_block", payload)

    assert payload["name"] == "oak_log"
    assert "block" not in payload
    assert payload["count"] == "3"
    assert "amount" not in payload


def test_normalize_vessel_mine_block_to_target():
    payload = {"block": "stone"}

    _normalize_vessel_payload_alias("vessel_minecraft_mine", payload)

    assert payload["target"] == "stone"
    assert "block" not in payload


def test_normalize_vessel_leaves_canonical_untouched():
    payload = {"item": "stick", "recipe": "should be ignored"}

    _normalize_vessel_payload_alias("vessel_minecraft_craft", payload)

    assert payload["item"] == "stick"
    assert payload["recipe"] == "should be ignored"


def test_normalize_vessel_ignores_non_vessel_actions():
    payload = {"recipe": "wooden_pickaxe"}

    _normalize_vessel_payload_alias("create_personal_diary_entry", payload)

    assert payload == {"recipe": "wooden_pickaxe"}


def test_normalize_payload_applies_vessel_alias():
    payload = {"recipe": "wooden_pickaxe"}

    _normalize_payload("vessel_minecraft_craft", payload)

    assert payload["item"] == "wooden_pickaxe"


def test_normalize_payload_renames_emotion_feelings_to_emotions():
    """Models place the prompt's 'feelings' object inside the
    update_emotion_state payload instead of the response top level; that payload
    fails validation (required 'emotions'). Normalization must rename it and
    scale the 0-1 values to the 0-10 schema scale."""
    payload = {"feelings": {"shyness": 0.7, "affection": 0.6}}

    _normalize_payload("update_emotion_state", payload)

    assert "feelings" not in payload
    assert payload["emotions"] == {"shyness": 7.0, "affection": 6.0}


def test_normalize_payload_emotion_keeps_existing_emotions():
    payload = {"emotions": {"joy": 8}, "feelings": {"shyness": 0.7}}

    _normalize_payload("update_emotion_state", payload)

    assert payload["emotions"] == {"joy": 8}
    assert "feelings" not in payload


def test_normalize_payload_emotion_leaves_other_actions_alone():
    payload = {"feelings": {"shyness": 0.7}}

    _normalize_payload("message_telegram_bot", payload)

    assert payload == {"feelings": {"shyness": 0.7}}
