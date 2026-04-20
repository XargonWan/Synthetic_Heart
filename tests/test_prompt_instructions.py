def test_unminified_chat_instruction_mentions_direct_reference():
    # Read raw source to avoid importing modules that require optional DB deps
    import io
    import os

    p = os.path.join(os.path.dirname(__file__), os.pardir, "core", "prompt_engine.py")
    p = os.path.normpath(p)
    with io.open(p, "r", encoding="utf-8") as f:
        src = f.read()
    src_low = src.lower()
    # Check for presence of the new English instructions
    assert "refer to its author" in src_low, (
        "Instruction to reference the author in a generic way is missing"
    )
    assert "avoid vague or impersonal" in src_low, (
        "Instruction to avoid vague phrasing is missing"
    )
    # Ensure no leftover Italian fragments remain
    assert "ho visto" not in src_low and "qualcuno" not in src_low, (
        "Found Italian text in unminified chat instruction; should be English only"
    )
