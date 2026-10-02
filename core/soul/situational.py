"""Deterministic helpers for recognising when two situational notes are one thing.

A situational note names a circumstance in its ``subject``, and the debrief
re-describes the same circumstance on every turn it stays true, so the store
accumulates several accounts of one event under different wording. Two notes
have to be recognisable as the same circumstance without asking an LLM, which
is what these token helpers do.

They are deliberately blunt: token sets, containment, no embeddings and no
weights. Nothing is ever deleted on their word - they decide which notes a
prompt shows and which older account a newer one supersedes.
"""

from __future__ import annotations

import re
from typing import Any

# A subject needs at least this many meaningful tokens to name a circumstance.
# Below it the subject names a person or a bare topic ("Scar", "Human"), which
# must never stand in for another note.
MIN_MEANINGFUL_TOKENS = 2

# Time-of-day words and hedging words are dropped so that two accounts of one
# circumstance written on different days collide instead of looking distinct
# ("Gathering at Sandro's" / "Gathering at Sandro's tonight").
_SUBJECT_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "at",
        "of",
        "for",
        "and",
        "in",
        "on",
        "to",
        "with",
        "from",
        "is",
        "are",
        "today",
        "tonight",
        "tomorrow",
        "evening",
        "morning",
        "afternoon",
        "later",
        "next",
        "this",
        "that",
        "upcoming",
        "planned",
        "possible",
        "possibly",
    }
)


def subject_tokens(subject: Any) -> set[str]:
    """Return the meaningful tokens of a note subject."""
    words = re.findall(r"[a-z0-9]+", str(subject or "").lower())
    return {word for word in words if len(word) > 1 and word not in _SUBJECT_STOPWORDS}


def is_same_circumstance(left: set[str], right: set[str]) -> bool:
    """True when two subject token sets name the same circumstance.

    Identical token sets always match, whatever their size. A subject that
    reduces to one meaningful token because its other words are time words
    ("wedding tomorrow" -> {"wedding"}) is a filed claim the correcting turn can
    only ever name by copying it verbatim, so the minimum-token rule below must
    not make it unreachable: it would leave the claim standing forever (live,
    2026-09-23). The one direction that stays blocked is a thin subject ABSORBING
    a richer one.

    Beyond that, containment, not equality: "Gathering at Sandro's" and
    "Gathering at Sandro's place" are the same circumstance, one described with
    more detail. Subjects below ``MIN_MEANINGFUL_TOKENS`` never match anything
    else, so a bare person's name cannot absorb or replace a real circumstance.
    """
    if left and left == right:
        return True
    if len(left) < MIN_MEANINGFUL_TOKENS or len(right) < MIN_MEANINGFUL_TOKENS:
        return False
    return left <= right or right <= left
