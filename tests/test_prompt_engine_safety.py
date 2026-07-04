from core.prompt_engine import load_unminified_chat_instruction


def test_unminified_chat_instruction_contains_safety():
    text = load_unminified_chat_instruction("telegram")
    lowered = text.lower()

    assert "response shape rules" in lowered
    assert "clarifying question" in lowered or "do not guess" in lowered
    assert "reply using only valid json" in lowered
    assert "do not force a fixed response length" in lowered


def test_unminified_chat_instruction_prioritizes_fresh_time_and_natural_length():
    text = load_unminified_chat_instruction("telegram")
    lowered = text.lower()

    assert "authoritative temporal context" in lowered
    assert "never infer the present time" in lowered
    assert "use time and location as ambient context" in lowered
    assert "do not volunteer the exact clock time" in lowered
    assert "do not open or pad ordinary replies with copied runtime facts" in lowered
    assert (
        "let the persona, relationship context, and the user's tone determine how much to say"
        in lowered
    )
