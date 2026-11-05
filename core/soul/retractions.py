"""Retracted claims: things the persona must stop treating as true.

The human sometimes says that something the persona believes is false ("Ca'
Senz'Autobus doesn't exist, it was a joke"). Situational notes cannot hold that:
they expire on their own and are about circumstances, while the false belief
spreads into the diary, the growth text, memories and goals, and is then re-read
into every prompt. A :class:`~core.soul.models.RetractedClaim` is the one place
that says "this is not true", and the helpers here turn it into

* a block every prompt and generator can carry (:func:`render_known_false_block`),
* a deterministic test for whether a note, goal or memory restates it
  (:func:`claim_matching`).

Matching reuses the blunt token helpers of :mod:`core.soul.situational`; nothing
is ever deleted on their word - a match only retires a live row or labels one.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

from core.logging_utils import log_debug

from .models import RetractedClaim
from .situational import MIN_MEANINGFUL_TOKENS, is_same_circumstance, subject_tokens

ACTIVE_STATUSES = ("active",)
PENDING_STATUSES = ("pending",)

# Appended to the instructions of generators that SUMMARISE history (diary
# compaction, consolidation): they must not drop what happened, only say that the
# belief was a mistake.
KNOWN_FALSE_SUMMARY_INSTRUCTION = (
    " The payload's known_false_claims lists statements that are FALSE. If the "
    "entries mention one, keep it as a recorded mistake (for example 'wrongly "
    "believed X; the trainer said it is not true'), never as a fact, a plan or a "
    "lasting circumstance."
)

# A prompt block is budget, not an archive.
MAX_BLOCK_CLAIMS = 8
_MAX_FIELD_CHARS = 160

_WORD_RE = re.compile(r"[a-z0-9]+")

# Free text that restates a claim rarely repeats the subject word for word ("trip
# to Ca' Senz'Autobus" vs a diary line about "Ca' Senz'Autobus"), so text matches
# when this share of the subject's tokens is present. Two-token subjects still
# need both.
_TEXT_MATCH_SHARE = 0.75


def _text_tokens(text: Any) -> set[str]:
    return set(_WORD_RE.findall(str(text or "").lower()))


def claim_tokens(claim: RetractedClaim) -> set[str]:
    """Meaningful tokens that identify what the claim is about."""
    return subject_tokens(claim.subject)


def claim_matching(
    claims: Iterable[RetractedClaim], *, subject: Any = "", text: Any = ""
) -> RetractedClaim | None:
    """Return the first claim that ``subject`` or ``text`` restates.

    ``subject`` is compared as a circumstance (:func:`is_same_circumstance`).
    ``text`` matches when most meaningful tokens of the claim's subject occur in
    it (:data:`_TEXT_MATCH_SHARE`), so a diary line or a goal that only mentions
    the thing still collides.
    A claim whose subject has fewer than ``MIN_MEANINGFUL_TOKENS`` tokens never
    matches free text: a bare word would swallow unrelated rows.
    """
    subject_set = subject_tokens(subject) if subject else set()
    text_set = _text_tokens(text) if text else set()
    for claim in claims:
        tokens = claim_tokens(claim)
        if not tokens:
            continue
        if subject_set and is_same_circumstance(subject_set, tokens):
            return claim
        if text_set and len(tokens) >= MIN_MEANINGFUL_TOKENS:
            needed = math.ceil(len(tokens) * _TEXT_MATCH_SHARE)
            if len(tokens & text_set) >= needed:
                return claim
    return None


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def strip_claim_sentences(text: str, claims: Iterable[RetractedClaim]) -> str:
    """Drop the sentences of ``text`` that restate a claim; keep the rest.

    Used on text a generator is about to build on (the previous growth note, a
    week of diary): the false sentence goes, everything else stays as written.
    Newlines are kept as separators so a list or a dated diary line survives.
    """
    claims = list(claims)
    if not claims or not text:
        return text
    kept: list[str] = []
    for line in str(text).split("\n"):
        pieces = _SENTENCE_SPLIT_RE.split(line) if line.strip() else [line]
        survivors = [
            piece
            for piece in pieces
            if not (piece.strip() and claim_matching(claims, text=piece))
        ]
        kept.append(" ".join(survivors) if survivors != pieces else line)
    return "\n".join(kept)


def drop_claim_items(
    items: Iterable[str], claims: Iterable[RetractedClaim]
) -> list[str]:
    """The items (recalled memories, diary lines) that do not restate a claim."""
    claims = list(claims)
    return [item for item in items if not claim_matching(claims, text=item)]


def _one_line(value: Any, limit: int = _MAX_FIELD_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_known_false_block(claims: Iterable[RetractedClaim]) -> str:
    """Prompt block naming the claims in force, or ``""`` when there are none."""
    lines = []
    for claim in list(claims)[:MAX_BLOCK_CLAIMS]:
        line = f"- {_one_line(claim.claim or claim.subject)}"
        if claim.reason:
            line += f" (the trainer said: {_one_line(claim.reason)})"
        lines.append(line)
    if not lines:
        return ""
    return (
        "These statements are FALSE. Do not state them, plan around them or write "
        "them into your diary, goals or self-description. If a memory, diary entry "
        "or note of yours says otherwise, it records a mistake, not a fact:\n"
        + "\n".join(lines)
    )


def render_pending_block(claims: Iterable[RetractedClaim]) -> str:
    """Prompt block asking the trainer to confirm retractions someone else made."""
    lines = []
    for claim in list(claims)[:MAX_BLOCK_CLAIMS]:
        who = (
            f" (said by {_one_line(claim.asserted_by, 60)})"
            if claim.asserted_by
            else ""
        )
        lines.append(f"- [{claim.id}] {_one_line(claim.claim or claim.subject)}{who}")
    if not lines:
        return ""
    return (
        "Someone other than the trainer said the statements below are false. Keep "
        "treating them as they were until the trainer confirms; ask the trainer "
        "about them at the next natural opportunity:\n" + "\n".join(lines)
    )


def get_soul_repository() -> Any | None:
    """Return the SOUL repository, or ``None`` when the SOUL plugin is absent.

    Shared by every reader and writer outside SOUL, so each does not re-implement
    the guarded registry lookup.
    """
    try:
        from core.core_initializer import PLUGIN_REGISTRY

        soul = PLUGIN_REGISTRY.get("soul_plugin")
        getter = getattr(soul, "get_repository", None)
        return getter() if callable(getter) else None
    except Exception as exc:
        log_debug(f"[retractions] SOUL repository unavailable: {exc}")
        return None


async def load_claims(
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
) -> list[RetractedClaim]:
    """Fetch claims in the given statuses; any failure yields an empty list.

    Fail-safe on purpose: a prompt must never be lost to a store hiccup.
    """
    repository = get_soul_repository()
    if repository is None:
        return []
    try:
        return list(await repository.list_retracted_claims(statuses))
    except Exception as exc:
        log_debug(f"[retractions] claim lookup failed: {exc}")
        return []


async def get_known_false_claims_block() -> str:
    """The block of claims in force, ready to inject into a prompt or payload."""
    return render_known_false_block(await load_claims(ACTIVE_STATUSES))


async def get_pending_retractions_block() -> str:
    """The block of retractions awaiting the trainer's confirmation."""
    return render_pending_block(await load_claims(PENDING_STATUSES))
