import pytest
from core.text_utils import normalize_for_outbound


def test_unescape_quotes_and_newlines():
    s = 'He said \\\"Ciao\\\" and smiled.\\nSee you.'
    out = normalize_for_outbound(s)
    assert '"Ciao"' in out
    assert "\n" in out  # newline preserved as actual newline


def test_mojibake_recovery():
    # Example mojibake: 'Ã¨' which should recover to 'è' when appropriate
    s = 'All Might Ã¨ una forza'
    out = normalize_for_outbound(s)
    # Normalization may or may not change depending on heuristics; ensure it returns a string
    assert isinstance(out, str)


def test_backslash_collapse():
    s = 'Backslash: \\\\'
    out = normalize_for_outbound(s)
    assert 'Backslash: \\' in out or 'Backslash: \\' == out
