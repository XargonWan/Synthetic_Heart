# core/synth_tagging.py

"""Tagging utilities.

This module currently provides only simple tag extraction and a placeholder for
`expand_tags`. The real database-backed expansion will be implemented later.
"""

from typing import List

from core.logging_utils import log_debug


def extract_tags(text: str) -> list[str]:
    text = text.lower()
    tags = []
    if "jay" in text:
        tags.append("jay")
    if "retrodeck" in text:
        tags.append("retrodeck")
    if "amore" in text or "affetto" in text:
        tags.append("emozioni")
    return tags


def expand_tags(tags: List[str]) -> List[str]:
    """Return tags unchanged (placeholder implementation).

    This fallback avoids database lookups when the ``tag_links`` table is not
    available. The full implementation will expand tags based on stored
    relationships in the future.
    """
    log_debug("[synth_tagging] expand_tags is not implemented yet (placeholder)")
    return tags
