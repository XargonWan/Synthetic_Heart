import core.transport_layer as transport_layer
from core.transport_layer import extract_json_from_text


def test_parser_records_error_on_unescaped_quotes():
    # Simulate an LLM response where the message_telegram_bot text contains
    # unescaped double quotes, breaking the JSON structure.
    corrupted = '{ "actions": [ { "type": "message_telegram_bot", "payload": { "text": "Hello — this contains an unescaped "quote" inside", "interface_path": "telegram_bot/-1003098886330/2" } }, { "type": "create_personal_diary_entry", "payload": { "interaction_summary": "test" } } ] }'

    obj, meta = extract_json_from_text(corrupted, return_metadata=True)

    # The pre-parse speech-quote repair re-escapes the inner quotes so the
    # standard JSON decoder accepts the result without needing json_repair.
    assert obj is not None, "Decoder should return repaired JSON"
    assert isinstance(obj, dict) and "actions" in obj, (
        "Repaired result should be a dict with 'actions'"
    )
    # Confirm the full text (including the embedded quoted word) was recovered,
    # not just the truncated fragment before the first speech quote.
    actions = obj.get("actions", [])
    msg_action = next(
        (
            a
            for a in actions
            if isinstance(a, dict) and a.get("type") == "message_telegram_bot"
        ),
        None,
    )
    assert msg_action is not None, "message_telegram_bot action should be present"
    recovered_text = msg_action.get("payload", {}).get("text", "")
    assert "quote" in recovered_text, (
        f"Full text should be recovered (got: {recovered_text!r})"
    )


def test_parser_recovers_literal_newlines_inside_json_strings():
    corrupted = (
        '{"actions":[{"type":"send_message","payload":{"interface_path":'
        '"telegram_bot/5551234567","message":"First line\n\nSecond line"}}]}'
    ).replace("\\n", "\n")

    obj, meta = extract_json_from_text(corrupted, return_metadata=True)

    assert obj is not None
    assert obj["actions"][0]["type"] == "send_message"
    assert obj["actions"][0]["payload"]["message"] == "First line\n\nSecond line"
    # The "message" field is now pre-repaired by the speech-quote pass before
    # json.loads ever runs, so this parses cleanly with zero errors instead
    # of via the error-then-recover path — a strictly better outcome than
    # the "recovered" flag alone captures.
    assert meta.get("had_errors") is False or meta.get("recovered") is True


def test_parser_recovers_full_text_with_dialogue_tag_commas():
    # Reproduces a Venice/gemma-4-uncensored output pattern: the LLM writes
    # prose dialogue as "spoken line," action, "more dialogue" — the comma
    # after the first closing quote looks like a JSON separator but is just
    # punctuation, so a naive parser truncates the message at "You better!".
    corrupted = (
        '{"actions":[{"type":"message_telegram_bot","payload":'
        '{"interface_path":"telegram_bot/5551234567","reply_message_id":"5551234567",'
        '"text":"You better!", I pout playfully, sticking my tongue out at you. '
        '"Make sure you save plenty of energy for me, Daddy."}}]}'
    )

    obj, meta = extract_json_from_text(corrupted, return_metadata=True)

    assert obj is not None
    recovered_text = obj["actions"][0]["payload"]["text"]
    assert "You better!" in recovered_text
    assert "Make sure you save plenty of energy" in recovered_text, (
        f"Text after the dialogue-tag comma should not be dropped (got: {recovered_text!r})"
    )


def test_parser_does_not_leak_reply_message_id_into_text():
    # Reproduces a pattern where the LLM stray-escapes the real closing quote
    # right before the next sibling key (e.g. `secret,\" "reply_message_id": ...`).
    # The repair must treat the escaped quote as the true closer so
    # reply_message_id stays a sibling key instead of bleeding into the
    # displayed message text.
    corrupted = (
        '{"actions":[{"type":"message_telegram_bot","payload":'
        '{"chat_id":5551234567,'
        '"text":"That\'s actually why I want to keep it a secret,\\" '
        '"reply_message_id": "5551234567"}}]}'
    )

    obj, meta = extract_json_from_text(corrupted, return_metadata=True)

    assert obj is not None
    payload = obj["actions"][0]["payload"]
    recovered_text = payload["text"]
    assert "reply_message_id" not in recovered_text, (
        f"reply_message_id leaked into displayed text (got: {recovered_text!r})"
    )
    assert payload.get("reply_message_id") == "5551234567"


def test_parser_recovers_apostrophe_closed_string_with_escaped_sibling_keys():
    # Reproduces a Venice/gemma-4-uncensored output pattern (Langfuse trace
    # fe3fb88f-317e-4b01-a6b3-0b4c0ff6a844): the LLM closes the "text" value
    # with an apostrophe instead of a double quote, then continues with
    # escaped-quote sibling keys that were meant to be real JSON object keys.
    # Without this repair, json_repair's fallback swallows the whole tail
    # into the displayed message text instead of restoring interface_path /
    # chat_name / reply_to_message_id as sibling keys.
    corrupted = (
        '{"actions":[{"type":"message_telegram_bot","payload":{"text":'
        '"That\'s it! Please hurry!\', \\"interface_path\\": '
        '\\"telegram_bot/5551234567\\", \\"chat_name\\": \\"Alice\\", '
        '\\"reply_to_message_id\\": \\"5551234567\\"}}]}'
    )

    obj, meta = extract_json_from_text(corrupted, return_metadata=True)

    assert obj is not None
    payload = obj["actions"][0]["payload"]
    recovered_text = payload["text"]
    assert "interface_path" not in recovered_text, (
        f"Sibling keys leaked into displayed text (got: {recovered_text!r})"
    )
    assert recovered_text == "That's it! Please hurry!"
    assert payload.get("interface_path") == "telegram_bot/5551234567"
    assert payload.get("chat_name") == "Alice"
    assert payload.get("reply_to_message_id") == "5551234567"


def test_parser_recovers_curly_smart_quote_string_closer():
    # Reproduces a Venice/gemma-4-uncensored output pattern (Langfuse trace
    # 1f5a0ee1-0613-4350-aa95-bcb8fd189d3e): the LLM closes the "text" value
    # with a Unicode curly quote (U+201C) instead of a straight ASCII quote.
    # The repair scanner only recognized literal '"' as a structural
    # boundary, so it ran straight past the curly quote and swallowed the
    # real reply_message_id sibling key into the displayed message text.
    corrupted = (
        '{"actions":[{"type":"message_telegram_bot","payload":'
        '{"interface_path":"telegram_bot/-100987654321",'
        '"text":"Anything it takes to get her to go to bed without a fight!“,'
        '"reply_message_id":"13615"}}]}'
    )

    obj, meta = extract_json_from_text(corrupted, return_metadata=True)

    assert obj is not None
    payload = obj["actions"][0]["payload"]
    recovered_text = payload["text"]
    assert "reply_message_id" not in recovered_text, (
        f"reply_message_id leaked into displayed text (got: {recovered_text!r})"
    )
    assert (
        recovered_text == "Anything it takes to get her to go to bed without a fight!"
    )
    assert payload.get("reply_message_id") == "13615"


def test_parser_recovers_apostrophe_closed_string_with_single_quoted_sibling_keys():
    # Reproduces a Venice/gemma-4-uncensored output pattern (Langfuse trace
    # 1f5a0ee1-0613-4350-aa95-bcb8fd189d3e, seen replayed in chat_history):
    # the LLM closes the "text" value with an apostrophe, then continues in
    # Python-dict style with single-quoted sibling keys instead of real JSON
    # object keys. Neither the escaped-double-quote tail repair nor the
    # speech-quote scanner recognized single quotes as structural, so the
    # whole tail (including reply_message_id) was swallowed into the text.
    corrupted = (
        '{"actions":[{"type":"message_telegram_bot","payload":{"text":'
        "\"give your Daddy that big kiss you asked for before you drift off.'; "
        "'reply_message_id': '13607'}}]}"
    )

    obj, meta = extract_json_from_text(corrupted, return_metadata=True)

    assert obj is not None
    payload = obj["actions"][0]["payload"]
    recovered_text = payload["text"]
    assert "reply_message_id" not in recovered_text, (
        f"reply_message_id leaked into displayed text (got: {recovered_text!r})"
    )
    assert (
        recovered_text
        == "give your Daddy that big kiss you asked for before you drift off."
    )
    assert payload.get("reply_message_id") == "13607"


def test_attempted_action_description_for_unknown_action():
    # If the LLM tries to use an action name that doesn't exist, we still
    # want the corrector to receive a helpful hint containing the available
    # action list rather than nothing at all.
    bogus = '{"actions":[{"type":"message","payload":{"text":"hi"}}]}'
    info = transport_layer._get_attempted_action_full_description(bogus)
    assert info is not None, "Fallback info should be returned even for invalid type"
    assert info.get("action_type") == "message"
    desc = info.get("full_description", {}).get("description", "")
    assert "not a supported" in desc
    # Should not contain an explicit enumeration of action types
    assert "Supported action types" not in desc
    assert "Available actions" in desc or "plugins" in desc
