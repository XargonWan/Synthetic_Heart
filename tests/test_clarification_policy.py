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
