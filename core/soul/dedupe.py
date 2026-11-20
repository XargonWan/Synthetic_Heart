"""Near-duplicate detection for memory cells, by word overlap.

Two cells that say the same thing restate each other's words, in any language, so
the measure is the share of distinct words (three letters or more) they have in
common. There is no vocabulary here and nothing language-specific: it is the same
comparison the clean-up script and the compiler use, so what the script would merge
is what the compiler no longer writes.

Measured on a live store, cells that were the same text had a median overlap of
0.90 and unrelated ones 0.09, so 0.8 is a conservative bar: it only treats a cell as
a repeat when almost every word is shared.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

#: Overlap at or above which a new cell is a repeat of an existing one.
DUPLICATE_OVERLAP = 0.8

_WORD_RE = re.compile(r"\w{3,}")


def content_words(text: str) -> frozenset[str]:
    """The distinct words of ``text`` that are three letters or longer, lower-cased."""
    return frozenset(_WORD_RE.findall(str(text or "").lower()))


def overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard overlap of two word sets (0 when either is empty)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_near_duplicate(
    trace: str,
    candidates: Iterable[Any],
    *,
    visibility: str,
    threshold: float = DUPLICATE_OVERLAP,
) -> Any | None:
    """The first candidate cell that repeats ``trace`` with the same audience.

    Cells with a different ``visibility`` are never treated as repeats of each
    other: merging a private memory into a shared one (or the reverse) would change
    who may be reminded of it.
    """
    words = content_words(trace)
    if not words:
        return None
    for cell in candidates:
        if getattr(cell, "visibility", "shared") != visibility:
            continue
        if overlap(words, content_words(cell.episodic_trace)) >= threshold:
            return cell
    return None


def union_participants(
    *groups: Iterable[dict[str, str]],
) -> list[dict[str, str]]:
    """Participants of several cells, once each (by id, else by name)."""
    seen: dict[str, dict[str, str]] = {}
    for group in groups:
        for person in group or []:
            key = str(person.get("id") or person.get("name") or "").strip()
            if key and key not in seen:
                seen[key] = dict(person)
    return list(seen.values())
