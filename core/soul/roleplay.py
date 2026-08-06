"""Detect roleplay / explicit speech turns for SOUL compilation.

SyntH runs on chats that mix ordinary conversation with in-character roleplay.
Only *ordinary* turns should be compiled into memories or the DSP user profile:
roleplay is fiction (in-world, in-character speech), not a stable record of who
the user is or what happened.

Detection is deliberately narrow and content-based (the repo already uses
content matching for emotion inference, e.g. ``_infer_emotional_event`` in the
SOUL plugin). It is a data-cleaning heuristic for the memory pipeline only — it
is never used for routing, intent detection, or any other product logic.

This module is pure and side-effect free; it owns no state.
"""

from __future__ import annotations

import re

# Explicit sexual / roleplay speech markers. A turn is treated as roleplay when
# it hits enough of these (or the strongest action-oriented markers) — enough to
# catch in-character explicit dialogue while letting ordinary conversation
# (even affectionate conversation) through. Word-boundary matched on lowercase.
_ROLEPLAY_TERMS = frozenset(
    {
        "fuck",
        "fucking",
        "fuckable",
        "slut",
        "whore",
        "bitch",
        "cum",
        "cumming",
        "cums",
        "cock",
        "dick",
        "pussy",
        "asshole",
        "tits",
        "boobs",
        "squirt",
        "horny",
        "hornier",
        "daddy",
        "mommy",
        "ride",
        "riding",
        "choke",
        "choking",
        "pregnant",
        "womb",
        "fuckhole",
        "breed",
        "breedable",
        "creampie",
        "orgasm",
        "moan",
        "moaning",
        "thrust",
        "thrusting",
        "pant",
        "panting",
        "lewd",
        "naughty",
        "suck",
        "sucking",
        "tongue",
        "nipples",
        "clit",
        "balls",
        "penis",
        "vagina",
    }
)

# Action-framing markers that strongly indicate in-character roleplay narration
# (first-person *action* narration), independent of the term list.
_ROLEPLAY_ACTION_RE = re.compile(
    r"(?i)\b(?:i\s+(?:pull|grab|force|slide|pick\s+up|pin|bend|pound|fuck|"
    r"thrust|ride|choke|squeeze|wrap|lean|mount)\b)|"
    r"\b(?:as\s+(?:i|she|he)\s+(?:cum|fuck|thrust|moan|squirt))\b"
)

_ROLEPLAY_EXCLAMATION_RE = re.compile(
    r"(?i)\b(?:breed me|fill me|deeper|harder|faster)\b"
)

# How many distinct terms count as "definitely roleplay" vs a stray word.
_MIN_TERM_HITS = 2
# A single very strong term (or an action frame) also counts when combined with
# any second weaker signal like an exclamation or any other term.
_STRONG_SINGLE_HITS = frozenset(
    {"cum", "cumming", "cock", "pussy", "slut", "whore", "fuckhole"}
)


def is_roleplay_turn(text: str | None) -> bool:
    """Return True when ``text`` looks like an explicit roleplay turn.

    Structural scoring:
    - 2+ distinct explicit terms  -> roleplay
    - an action-narration frame  -> roleplay
    - an explicit demand/breed phrase -> roleplay
    - a single *strong* explicit term + an exclamation/demand -> roleplay
    """
    if not text or not str(text).strip():
        return False
    lower = str(text).lower()
    words = re.findall(r"[a-z']+", lower)

    hits = {word for word in words if word in _ROLEPLAY_TERMS}
    if len(hits) >= _MIN_TERM_HITS:
        return True

    strong = hits & _STRONG_SINGLE_HITS
    if strong and _ROLEPLAY_EXCLAMATION_RE.search(lower):
        return True
    if _ROLEPLAY_ACTION_RE.search(lower):
        return True
    return False


def strip_roleplay_lines(transcript: str | None) -> str:
    """Remove roleplay turns from a compiled transcript, line by line.

    Keeps every non-roleplay line in order. Lines are expected in the transcript
    format produced by ``_build_daily_transcript`` (``speaker: "text"``) or the
    SOUL buffer (bare text) — the detector runs on the whole line content.
    """
    if not transcript:
        return ""
    kept: list[str] = []
    for line in str(transcript).splitlines():
        if line.strip() and not is_roleplay_turn(line):
            kept.append(line)
    return "\n".join(kept)
