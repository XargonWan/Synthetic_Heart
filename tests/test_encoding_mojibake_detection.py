from core.text_utils import (
    looks_like_mojibake,
    normalize_for_outbound,
    try_recover_mojibake,
)


def test_detects_mojibake_for_flag_emoji():
    proper = "🇬🇧 English: Loehnert"
    # create mojibake by decoding UTF-8 bytes as latin-1
    mojibake = proper.encode("utf-8").decode("latin-1")
    assert looks_like_mojibake(mojibake) is True
    recovered = try_recover_mojibake(mojibake)
    assert recovered == proper


def test_no_false_positive_on_normal_ascii():
    text = "Hello, this is normal ASCII text."
    assert looks_like_mojibake(text) is False
    assert try_recover_mojibake(text) == text


def test_recovery_handles_windows1252_variants():
    proper = "Café — résumé"
    mojibake = proper.encode("utf-8").decode("latin-1")
    assert looks_like_mojibake(mojibake) is True
    recovered = try_recover_mojibake(mojibake)
    assert recovered == proper


# --- CJK / non-Latin script tests ---


def test_no_false_positive_on_japanese():
    """Japanese text (Hiragana, Katakana, Kanji) must never be detected as mojibake."""
    text = "木村さんはきてないか"
    assert looks_like_mojibake(text) is False
    assert try_recover_mojibake(text) == text


def test_no_false_positive_on_mixed_japanese_latin():
    """Mixed Japanese + Latin text must not be flagged as mojibake."""
    text = "La frase 木村さんはきてないか è giustissima!"
    assert looks_like_mojibake(text) is False
    assert try_recover_mojibake(text) == text


def test_normalize_outbound_preserves_japanese():
    """normalize_for_outbound must return Japanese kanji/kana intact."""
    text = "木村さんはきてないか"
    result = normalize_for_outbound(text)
    assert result == text, f"Japanese garbled: {result!r}"


def test_normalize_outbound_resolves_literal_newline_escape_in_japanese():
    """A literal \\n escape inside Japanese text is resolved to a real newline
    without corrupting any multibyte character."""
    text = "木村さん\\nはきてないか"
    result = normalize_for_outbound(text)
    assert result == "木村さん\nはきてないか", f"Unexpected: {result!r}"


def test_normalize_outbound_resolves_literal_unicode_escape():
    """A literal \\uXXXX escape is resolved to the correct Unicode character
    without corrupting adjacent multibyte characters."""
    text = "木村\\u3055"  # \\u3055 == さ
    result = normalize_for_outbound(text)
    assert result == "木村さ", f"Unexpected: {result!r}"


def test_normalize_outbound_does_not_corrupt_chinese():
    """Simplified Chinese must pass through normalize_for_outbound unchanged."""
    text = "你好世界"
    assert normalize_for_outbound(text) == text


def test_normalize_outbound_does_not_corrupt_cyrillic():
    """Cyrillic text must pass through normalize_for_outbound unchanged."""
    text = "Привет мир"
    assert normalize_for_outbound(text) == text
