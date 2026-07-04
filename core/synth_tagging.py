# core/synth_tagging.py

"""Tagging utilities.

This module provides language-agnostic salient-token extraction and a
placeholder for `expand_tags`. The real database-backed expansion will be
implemented later.
"""

import re
import unicodedata
from typing import List

from core.logging_utils import log_debug

# Minimum length (in characters) for a token to be considered salient. Very
# short tokens (articles, prepositions, particles across most languages) carry
# little retrieval signal and only add noise.
_MIN_TOKEN_LEN = 3

# Maximum number of distinct salient tokens returned, to keep downstream
# queries bounded regardless of message length.
_MAX_TOKENS = 12


def extract_tags(text: str) -> list[str]:
    """Extract salient tokens from arbitrary text, language-agnostically.

    This is intentionally free of any hardcoded keyword/phrase lists so it works
    across all languages (project multilingual rule). It lowercases, splits on
    Unicode word boundaries, drops very short tokens and pure numbers, and
    returns distinct tokens in first-seen order (capped).

    NOTE: The returned tokens are content words, not curated tag labels. When
    passed to ``search_memories`` they should be used as ``keywords`` (matched
    against row content via LIKE), not as ``tags`` (matched against the JSON tag
    columns) — the auto-generated tag arrays rarely contain these raw tokens.
    """
    if not text:
        return []

    # Normalise so accented characters split predictably across languages.
    normalized = unicodedata.normalize("NFKC", str(text)).lower()

    seen: set[str] = set()
    tokens: list[str] = []
    # \w with re.UNICODE keeps letters/digits/underscore for any script.
    for raw in re.findall(r"\w+", normalized, flags=re.UNICODE):
        tok = raw.strip("_")
        if len(tok) < _MIN_TOKEN_LEN:
            continue
        if tok.isdigit():
            continue
        if tok in seen:
            continue
        seen.add(tok)
        tokens.append(tok)
        if len(tokens) >= _MAX_TOKENS:
            break

    return tokens


def expand_tags(tags: List[str]) -> List[str]:
    """Return tags unchanged (placeholder implementation).

    This fallback avoids database lookups when the ``tag_links`` table is not
    available. The full implementation will expand tags based on stored
    relationships in the future.
    """
    log_debug("[synth_tagging] expand_tags is not implemented yet (placeholder)")
    return tags
