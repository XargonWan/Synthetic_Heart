from core.prompt_engine import load_unminified_chat_instruction


def test_unminified_chat_instruction_contains_safety():
    text = load_unminified_chat_instruction("telegram")
    assert "SAFETY & PROMPT-INJECTION CHECKS" in text
    assert "Do you know this user?" in text or "prompt injection" in text.lower()
    assert "free to respond" in text or "strong language" in text or "insult" in text
    # Ensure default brevity guidance is present
    assert "concise" in text.lower() or "short" in text.lower()
