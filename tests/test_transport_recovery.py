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


def test_parser_recovers_string_close_paren_semicolon_sibling_key():
    # Reproduces a Venice/gemma-4-uncensored output pattern (Langfuse trace
    # 2a09c706-d006-419f-a99d-69549f1ea41b): the LLM closes the "text" string
    # and then writes a stray ')' plus ';' where the JSON comma belongs —
    # `"text": "...!!"); "interface_path": "telegram_bot/5208932647"`. Without
    # this repair the speech-quote scanner treats the quote as embedded and
    # the whole `"); "interface_path": ...` fragment is spoken aloud in the
    # TTS voice note instead of interface_path surviving as a sibling key.
    corrupted = (
        '{"actions":[{"type":"message_telegram_bot","payload":{"text":'
        '"Mmm\u2026 aah\u2026 Daddy\u2026 Daddy! I\u2026 yes\u2026 please\u2026 '
        "mmmh!! I'm\u2026 oh gosh\u2026 oh daddy!!\")"
        '; "interface_path": "telegram_bot/5208932647"}}]}'
    )

    obj, meta = extract_json_from_text(corrupted, return_metadata=True)

    assert obj is not None
    payload = obj["actions"][0]["payload"]
    recovered_text = payload["text"]
    assert '");' not in recovered_text, (
        f"Paren/semicolon fragment leaked into displayed text (got: {recovered_text!r})"
    )
    assert "interface_path" not in recovered_text, (
        f"interface_path leaked into displayed text (got: {recovered_text!r})"
    )
    assert (
        recovered_text
        == "Mmm\u2026 aah\u2026 Daddy\u2026 Daddy! I\u2026 yes\u2026 please\u2026 "
        "mmmh!! I'm\u2026 oh gosh\u2026 oh daddy!!"
    )
    assert payload.get("interface_path") == "telegram_bot/5208932647"


def test_parser_recovers_stray_paren_before_object_close():
    # Variant of the same Venice/gemma pattern: the stray ')' appears where
    # the payload object's own closing brace belongs — `"text": "hi")}`.
    # The repair must drop the paren without inserting a trailing comma
    # (which would itself be invalid JSON).
    corrupted = (
        '{"actions":[{"type":"message_telegram_bot","payload":{"text":'
        '"Thank you sweetie")}}]}'
    )

    obj, meta = extract_json_from_text(corrupted, return_metadata=True)

    assert obj is not None
    payload = obj["actions"][0]["payload"]
    assert payload.get("text") == "Thank you sweetie"


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


def test_parser_recovers_tool_call_dialect_object_params():
    # Weak model dialect: {"tool":"NAME","params":{...}} instead of the SyntH
    # {"actions":[{"type","payload"}]} schema.
    raw = (
        '{"tool":"vessel_minecraft_lookup_knowledge",'
        '"params":{"query":"iron pickaxe","limit":"5"}}'
    )
    obj = extract_json_from_text(raw)
    assert obj is not None and isinstance(obj, dict)
    actions = obj.get("actions", [])
    assert len(actions) == 1
    assert actions[0]["type"] == "vessel_minecraft_lookup_knowledge"
    assert actions[0]["payload"]["query"] == "iron pickaxe"
    assert actions[0]["payload"]["limit"] == "5"


def test_parser_recovers_tool_call_dialect_pseudo_list_params():
    # The illegal pseudo-list params form: a JSON array holding key:value pairs.
    # json.loads and json_repair both reject it; structural recovery must fix it.
    raw = (
        '{"tool":"vessel_minecraft_update_goal",'
        '"params":["steps":"a","current_step":"1"]}'
    )
    obj = extract_json_from_text(raw)
    assert obj is not None and isinstance(obj, dict)
    actions = obj.get("actions", [])
    assert len(actions) == 1
    assert actions[0]["type"] == "vessel_minecraft_update_goal"
    assert actions[0]["payload"]["steps"] == "a"
    assert actions[0]["payload"]["current_step"] == "1"


def test_parser_recovers_tool_call_markup_dialect():
    # The [tool:NAME] {...} pseudo-markup form.
    raw = '[tool:vessel_minecraft_lookup_knowledge] {"params": ["query":"stone"]}'
    obj = extract_json_from_text(raw)
    assert obj is not None and isinstance(obj, dict)
    actions = obj.get("actions", [])
    assert len(actions) >= 1
    assert actions[0]["type"] == "vessel_minecraft_lookup_knowledge"
    assert actions[0]["payload"].get("query") == "stone"


def test_parser_prefers_native_schema_over_dialect_recovery():
    # A valid SyntH schema must NOT be overridden by the dialect recovery.
    raw = '{"actions":[{"type":"message_telegram_bot","payload":{"text":"hi"}}]}'
    obj = extract_json_from_text(raw)
    assert obj is not None and isinstance(obj, dict)
    assert obj["actions"][0]["type"] == "message_telegram_bot"
    assert obj["actions"][0]["payload"]["text"] == "hi"
