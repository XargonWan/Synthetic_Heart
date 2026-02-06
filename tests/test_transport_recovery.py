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
    assert obj is not None, "Decoder should return at least partial JSON (other actions)"
    assert meta.get('had_errors', False) is True, "Parser should mark had_errors=True"
    assert transport_layer.LAST_JSON_ERROR_INFO is not None and isinstance(transport_layer.LAST_JSON_ERROR_INFO, str)
    assert 'Expecting' in transport_layer.LAST_JSON_ERROR_INFO or 'Invalid' in transport_layer.LAST_JSON_ERROR_INFO


def test_corrector_skips_on_valid_json_with_noise():
    import asyncio
    from core.transport_layer import run_corrector_middleware

    noisy = (
        "Traceback (most recent call last):\n"
        "  File \"/app/selenium_llm_base.py\", line 2617, in send\n"
        "    raise Exception('element click intercepted')\n"
        "Exception: element click intercepted\n"
        "#0 0x6543f8826a4a <unknown>\n"
        "#1 0x6543f823f6a2 <unknown>\n"
        '{"actions": [{"type": "message_telegram_bot", "payload": {"text": "Test message", "interface_path": "telegram_bot/31321637"}}]}'
    )

    # The corrector should skip correction and return the original text because a valid JSON can
    # be extracted from the noisy payload.
    res = asyncio.run(run_corrector_middleware(noisy, bot=None, context=None, chat_id=31321637))
    assert res == noisy


def test_extract_json_from_text_with_stacktrace():
    noisy = (
        "Traceback (most recent call last):\n"
        "  File \"/app/selenium_llm_base.py\", line 2617, in send\n"
        "    raise Exception('element click intercepted')\n"
        "Exception: element click intercepted\n"
        "#0 0x6543f8826a4a <unknown>\n"
        "#1 0x6543f823f6a2 <unknown>\n"
        "{"
        '"actions": [{"type": "message_telegram_bot", "payload": {"text": "Test message", "interface_path": "telegram_bot/31321637"}}]'
        "} some trailing text"
    )

    obj, meta = extract_json_from_text(noisy, return_metadata=True)

    assert obj is not None, "Should recover JSON even when stacktrace is present"
    assert isinstance(obj, dict)
    assert 'actions' in obj and isinstance(obj['actions'], list)
    assert meta.get('had_extra_text') or meta.get('noise_removed', False)
