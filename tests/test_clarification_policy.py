def test_json_instructions_require_clarification():
    from core.prompt_engine import load_json_instructions

    inst = load_json_instructions()
    # The JSON instructions must require asking for clarification when the user's
    # intent/referent is ambiguous instead of guessing.
    assert (
        "clarifying question" in inst.lower()
        or "clarification policy" in inst.lower()
        or "do not guess" in inst.lower()
    ), "Prompt must instruct model to ask clarifying questions when ambiguous"
    assert "memory honesty" in inst.lower()
    assert "prefer honesty over confidence" in inst.lower()
    assert "say so" in inst.lower()


def test_unminified_chat_instruction_includes_clarify():
    from core.prompt_engine import load_unminified_chat_instruction

    txt = load_unminified_chat_instruction("telegram")
    assert "clarify" in txt.lower() or "clarifying" in txt.lower(), (
        "Unminified chat instructions should tell the model to ask clarifying questions"
    )
    assert "prefer explicit honesty over confident reconstruction" in txt.lower()
    assert "potentially incomplete or reconstructed" in txt.lower()
