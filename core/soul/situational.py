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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------------------
# Plain-text view of the standing notes (WebUI editor)
# ---------------------------------------------------------------------------
#
# One note per line: ``TYPE | subject | valid_from -> valid_until | summary``.
# The summary is the last field, so it may itself contain ``|``. Blank lines and
# lines starting with ``#`` are ignored. A note's id is derived from type,
# subject and summary, so a line left untouched maps back onto its own row; a
# line whose text was edited is a different note, and the row it replaced is
# resolved.

NOTES_TEXT_HEADER = (
    "# One note per line: TYPE | subject | valid_from -> valid_until | summary\n"
    "# TYPE is EVENT, STATE, INTERVAL or INSTANT. Dates are ISO 8601 (UTC when no\n"
    "# offset is given); a blank start means now, a blank end means 24 hours later.\n"
    "# Delete a line to retire that note. Lines starting with # are ignored."
)

_WINDOW_SEPARATOR = "->"
_DEFAULT_TTL = timedelta(hours=24)


@dataclass(slots=True)
class ParsedNoteLine:
    """One note as written in the plain-text editor."""

    note_type: str
    subject: str
    summary: str
    valid_from: datetime
    valid_until: datetime


def _format_when(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_when(raw: str, line_no: int) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"line {line_no}: '{text}' is not an ISO date") from exc
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    """Equal to the second: the text view drops sub-second precision."""
    if left is None or right is None:
        return left is right
    return abs((left - right).total_seconds()) < 1


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def render_notes_text(notes: list[Any]) -> str:
    """Render notes in the editor's one-line-per-note format."""
    lines = [NOTES_TEXT_HEADER]
    for note in notes:
        window = (
            f"{_format_when(note.valid_from)} {_WINDOW_SEPARATOR} "
            f"{_format_when(note.valid_until)}"
        )
        lines.append(
            f"{note.note_type} | {note.subject} | {window} | {_one_line(note.summary)}"
        )
    return "\n".join(lines) + "\n"


def parse_notes_text(text: str, *, now: datetime) -> list[ParsedNoteLine]:
    """Parse the editor text. Raises ``ValueError`` naming the first bad line."""
    from core.soul.models import SITUATIONAL_NOTE_TYPES

    parsed: list[ParsedNoteLine] = []
    for line_no, raw_line in enumerate(str(text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|", 3)]
        if len(parts) != 4:
            raise ValueError(
                f"line {line_no}: expected 'TYPE | subject | from -> until | summary'"
            )
        note_type, subject, window, summary = parts
        note_type = note_type.upper()
        if note_type not in SITUATIONAL_NOTE_TYPES:
            raise ValueError(
                f"line {line_no}: type must be one of {', '.join(SITUATIONAL_NOTE_TYPES)}"
            )
        if not subject or not summary:
            raise ValueError(f"line {line_no}: subject and summary are required")
        if _WINDOW_SEPARATOR not in window:
            raise ValueError(f"line {line_no}: window must read 'from -> until'")
        raw_from, raw_until = window.split(_WINDOW_SEPARATOR, 1)
        valid_from = _parse_when(raw_from, line_no) or now
        valid_until = _parse_when(raw_until, line_no) or valid_from + _DEFAULT_TTL
        if valid_until <= valid_from:
            raise ValueError(f"line {line_no}: the window ends before it starts")
        parsed.append(
            ParsedNoteLine(
                note_type=note_type,
                subject=subject,
                summary=summary,
                valid_from=valid_from,
                valid_until=valid_until,
            )
        )
    return parsed


async def apply_notes_text(
    repository: Any, text: str, *, now: datetime
) -> dict[str, int]:
    """Make the active notes match the editor text.

    The whole text is parsed before anything is written, so a bad line changes
    nothing. Lines matching an active note keep its priority and confidence and
    only move its window; new lines are stored as operator notes; active notes
    missing from the text are resolved, never deleted.
    """
    from core.soul.models import situational_note_from_extraction, situational_note_id

    entries = parse_notes_text(text, now=now)
    active = await repository.list_active_situational_notes(now=now)
    # Keyed by content, not by the stored id: rows written before ids were
    # derived carry random ids, and must still map onto their own line.
    by_key = {
        situational_note_id(note.note_type, note.subject, _one_line(note.summary)): note
        for note in active
    }

    counts = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    seen: set[str] = set()
    kept_ids: set[str] = set()
    for entry in entries:
        key = situational_note_id(entry.note_type, entry.subject, entry.summary)
        if key in seen:
            continue
        seen.add(key)
        existing = by_key.get(key)
        if existing is not None:
            kept_ids.add(existing.id)
            if _same_instant(existing.valid_from, entry.valid_from) and _same_instant(
                existing.valid_until, entry.valid_until
            ):
                counts["unchanged"] += 1
                continue
            existing.valid_from = entry.valid_from
            existing.valid_until = entry.valid_until
            await repository.upsert_situational_note(existing)
            counts["updated"] += 1
            continue
        note = situational_note_from_extraction(
            note_type=entry.note_type,
            subject=entry.subject,
            summary=entry.summary,
            confidence=1.0,
            valid_from=entry.valid_from,
            valid_until=entry.valid_until,
            source="webui",
            now=now,
        )
        kept_ids.add(await repository.upsert_situational_note(note))
        counts["added"] += 1

    for note in active:
        if note.id not in kept_ids:
            await repository.resolve_situational_note(note.id, new_status="resolved")
            counts["removed"] += 1
    return counts
