import core.transport_layer as transport_layer
from core.transport_layer import extract_json_from_text


def test_parser_records_error_on_unescaped_quotes():
    # Simulate an LLM response where the message_telegram_bot text contains
    # unescaped double quotes, breaking the JSON structure.
    corrupted = '{ "actions": [ { "type": "message_telegram_bot", "payload": { "text": "Hello — this contains an unescaped "quote" inside", "interface_path": "telegram_bot/-1003098886330/2" } }, { "type": "create_personal_diary_entry", "payload": { "interaction_summary": "test" } } ] }'

    obj, meta = extract_json_from_text(corrupted, return_metadata=True)

    # For this conservative approach we don't require full recovery here.
    # The important behavior is that the parser detected errors and recorded a
    # clear hint so the corrector middleware can use it to ask the LLM to
    # regenerate valid JSON.
    assert obj is not None, (
        "Decoder should return at least partial JSON (other actions)"
    )
    assert meta.get("had_errors", False) is True, "Parser should mark had_errors=True"
    assert transport_layer.LAST_JSON_ERROR_INFO is not None and isinstance(
        transport_layer.LAST_JSON_ERROR_INFO, str
    )
    assert (
        "Expecting" in transport_layer.LAST_JSON_ERROR_INFO
        or "Invalid" in transport_layer.LAST_JSON_ERROR_INFO
    )
