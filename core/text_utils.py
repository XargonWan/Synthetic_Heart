"""Text utilities: detection and lightweight recovery for encoding issues (mojibake).

These helpers are intentionally small: they don't try to be perfect, but
provide diagnostic detection and a simple recovery path for typical cases
where UTF-8 bytes were incorrectly decoded as ISO-8859-1/Windows-1252.
"""

import re
from typing import Optional

# Unicode ranges for scripts that are inherently non-ASCII but are never mojibake.
# Any character in these ranges means the text is legitimate Unicode, not garbled.
_LEGITIMATE_NONLATIN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0370, 0x03FF),  # Greek and Coptic
    (0x0400, 0x04FF),  # Cyrillic
    (0x0600, 0x06FF),  # Arabic
    (0x0900, 0x097F),  # Devanagari
    (0x3000, 0x9FFF),  # CJK unified (Hiragana, Katakana, Kanji …)
    (0xAC00, 0xD7FF),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x1F300, 0x1FAFF),  # Emoji / Supplemental Symbols (when intact)
)


def _contains_legitimate_script(text: str) -> bool:
    """Return True if *text* contains any character that belongs to a
    non-Latin script that is inherently non-ASCII (CJK, Cyrillic, Arabic …).

    Such text is never mojibake: it is valid Unicode and must not be touched.
    """
    for ch in text:
        o = ord(ch)
        for lo, hi in _LEGITIMATE_NONLATIN_RANGES:
            if lo <= o <= hi:
                return True
    return False


def looks_like_mojibake(text: Optional[str]) -> bool:
    """Return True when `text` contains patterns that commonly indicate
    UTF-8 bytes were decoded using latin-1 / windows-1252.

    Heuristics used:
    - Presence of replacement-like sequences such as 'Ã', 'Â', or 'ð' followed by
      characters in the U+0080-U+00FF range that are common in mojibake
    - Presence of sequences like '\u00c3' or 'Ã' near other non-ascii

    Important: text that contains characters from legitimate non-Latin scripts
    (CJK, Cyrillic, Arabic, Devanagari …) is *never* treated as mojibake —
    those codepoints cannot appear as a result of UTF-8→latin-1 mis-decoding.
    """
    if not text:
        return False

    # Guard: legitimate non-Latin scripts are real Unicode, never mojibake.
    if _contains_legitimate_script(text):
        return False

    # Quick heuristic checks for the typical UTF-8-as-latin-1 pattern
    if "Ã" in text or "Â" in text:
        return True
    # Emojis that often show as two odd characters when mis-decoded start with 'ð'
    if "ð" in text and any(c in text for c in ("", "", "")):
        return True
    # Multiple consecutive non-ASCII bytes in the latin-1 range are suspicious
    # only when the text does NOT belong to a known good script (checked above).
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
        recovered = text.encode("latin-1").decode("utf-8")
        candidates.append(recovered)
    except Exception:
        pass

    try:
        # windows-1252 -> utf-8
        recovered = text.encode("cp1252").decode("utf-8")
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

    # If there are visible backslash-escape sequences, resolve them safely.
    # IMPORTANT: we never use bytes().decode("unicode_escape") because that codec
    # treats each UTF-8 byte as an independent latin-1 codepoint, corrupting any
    # multibyte character (CJK, Cyrillic, emoji …).  Instead we perform targeted
    # string-level substitutions that are safe for all Unicode content.
    try:
        if isinstance(text, str) and (
            "\\u" in text
            or "\\n" in text
            or "\\t" in text
            or "\\r" in text
            or "\\x" in text
            or '\\"' in text
            or "\\'" in text
        ):
            # Resolve \uXXXX -> Unicode character (string-level, Unicode-safe)
            resolved = re.sub(
                r"\\u([0-9a-fA-F]{4})",
                lambda m: chr(int(m.group(1), 16)),
                text,
            )
            # Resolve common single-character escapes
            resolved = (
                resolved.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace("\\r", "\r")
                .replace('\\"', '"')
                .replace("\\'", "'")
                .replace("\\\\", "\\")
            )
            if resolved and resolved != text:
                text = resolved
    except Exception:
        # Non-fatal: keep original text
        pass

    return text
