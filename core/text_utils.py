"""Text utilities: detection and lightweight recovery for encoding issues (mojibake).

These helpers are intentionally small: they don't try to be perfect, but
provide diagnostic detection and a simple recovery path for typical cases
where UTF-8 bytes were incorrectly decoded as ISO-8859-1/Windows-1252.
"""
from typing import Optional


def looks_like_mojibake(text: Optional[str]) -> bool:
    """Return True when `text` contains patterns that commonly indicate
    UTF-8 bytes were decoded using latin-1 / windows-1252.

    Heuristics used:
    - Presence of replacement-like sequences such as 'Ã', 'Â', or 'ð' followed by
      characters in the U+0080-U+00FF range that are common in mojibake
    - Presence of sequences like '\u00c3' or 'Ã' near other non-ascii
    """
    if not text:
        return False
    # Quick heuristic checks
    if 'Ã' in text or 'Â' in text:
        return True
    # Emojis that often show as two odd characters when mis-decoded start with 'ð'
    if 'ð' in text and any(c in text for c in ('', '', '')):
        return True
    # Some mojibake uses multiple consecutive non-ascii sequences
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    if non_ascii and non_ascii / max(1, len(text)) > 0.2:
        return True
    return False


def try_recover_mojibake(text: Optional[str]) -> Optional[str]:
    """Attempt to recover text by re-encoding as latin-1/windows-1252 and
    decoding back as UTF-8. Returns recovered string if successful or the
    original value on failure.
    """
    if text is None:
        return None
    # If it doesn't look like mojibake, avoid touching it
    if not looks_like_mojibake(text):
        return text

    # Try common recovery strategies
    candidates = []
    try:
        # latin-1 -> utf-8
        recovered = text.encode('latin-1').decode('utf-8')
        candidates.append(recovered)
    except Exception:
        pass

    try:
        # windows-1252 -> utf-8
        recovered = text.encode('cp1252').decode('utf-8')
        candidates.append(recovered)
    except Exception:
        pass

    # Prefer the candidate that contains fewer odd control characters and
    # more common unicode letters (very simple heuristic)
    def score(s: str) -> int:
        score = 0
        for ch in s:
            o = ord(ch)
            if o >= 0x0400 and o <= 0x2FFF:
                score += 2
            elif o >= 0x0020 and o <= 0x007E:
                score += 1
            elif o > 0x007E:
                score += 1
            else:
                score -= 1
        return score

    if not candidates:
        return text

    best = max(candidates, key=score)
    return best


def normalize_for_outbound(text: Optional[str]) -> Optional[str]:
    """Normalize text before sending to interfaces:

    - Attempt mojibake recovery using try_recover_mojibake
    - Unescape common backslash-escaped sequences (unicode_escape) if present

    This function is conservative and returns the original text if any step
    fails or if no likely transformation is detected.
    """
    if text is None:
        return None

    # First, try to recover mojibake
    try:
        recovered = try_recover_mojibake(text)
        if recovered and recovered != text:
            text = recovered
    except Exception:
        # Non-fatal: proceed with original text
        pass

    # If there are visible backslash escapes, attempt unicode unescape
    try:
        if isinstance(text, str) and ("\\u" in text or "\\n" in text or "\\t" in text or "\\r" in text or "\\x" in text or '\\"' in text or "\\'" in text):
            unescaped = bytes(text, "utf-8").decode("unicode_escape")
            if unescaped and unescaped != text:
                text = unescaped
    except Exception:
        # Non-fatal: keep original text
        pass

    # Also collapse leftover JSON-style escapes like \" -> " and \\ -> \ if present
    try:
        if isinstance(text, str) and ('\\"' in text or "\\'" in text or '\\\\' in text):
            new_text = text.replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')
            if new_text != text:
                text = new_text
    except Exception:
        pass

    return text
