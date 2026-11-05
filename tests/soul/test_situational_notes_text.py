"""Tests for the plain-text editor view of situational notes."""

from datetime import datetime, timedelta, timezone

import pytest

from core.soul.models import SituationalNote, situational_note_id
from core.soul.repository import InMemorySoulRepository
from core.soul.situational import (
    apply_notes_text,
    parse_notes_text,
    render_notes_text,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _note(
    subject: str, summary: str, *, note_id: str | None = None, priority: int = 2
) -> SituationalNote:
    return SituationalNote(
        id=note_id or situational_note_id("EVENT", subject, summary),
        note_type="EVENT",
        subject=subject,
        summary=summary,
        valid_from=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(days=1, microseconds=1234),
        priority=priority,
        confidence=0.7,
    )


async def _repo_with(*notes: SituationalNote) -> InMemorySoulRepository:
    repo = InMemorySoulRepository()
    for note in notes:
        repo.situational_notes[note.id] = note
    return repo


def test_parse_defaults_and_pipes_in_summary() -> None:
    entries = parse_notes_text(
        "# comment\n\nstate | sore back |  -> | rests a lot | no gym\n", now=NOW
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.note_type == "STATE"
    assert entry.summary == "rests a lot | no gym"
    assert entry.valid_from == NOW
    assert entry.valid_until == NOW + timedelta(hours=24)


@pytest.mark.parametrize(
    "line",
    [
        "EVENT | only three | fields",
        "PARTY | dentist visit | -> | at 10",
        "EVENT | dentist visit | 2026-10-03 | at 10",
        "EVENT | dentist visit | nope -> | at 10",
        "EVENT | dentist visit | 2026-10-03 -> 2026-10-02 | at 10",
    ],
)
def test_parse_rejects_bad_lines(line: str) -> None:
    with pytest.raises(ValueError, match="line 2"):
        parse_notes_text(f"# header\n{line}\n", now=NOW)


@pytest.mark.asyncio
async def test_untouched_text_changes_nothing() -> None:
    legacy = _note("dentist visit", "dentist on 2026-10-03", note_id="legacy-random")
    repo = await _repo_with(_note("wedding day", "wedding on 2026-10-04"), legacy)
    text = render_notes_text(await repo.list_active_situational_notes(now=NOW))

    counts = await apply_notes_text(repo, text, now=NOW)

    assert counts == {"added": 0, "updated": 0, "unchanged": 2, "removed": 0}
    assert all(n.status == "active" for n in repo.situational_notes.values())


@pytest.mark.asyncio
async def test_edit_add_remove() -> None:
    keep = _note("wedding day", "wedding on 2026-10-04")
    drop = _note("dentist visit", "dentist on 2026-10-03")
    repo = await _repo_with(keep, drop)
    text = (
        "EVENT | wedding day | 2026-10-04T00:00:00+00:00 -> 2026-10-05T00:00:00+00:00"
        " | wedding on 2026-10-04\n"
        "STATE | flight to Osaka | -> 2026-10-06 | flying to Osaka on 2026-10-05\n"
    )

    counts = await apply_notes_text(repo, text, now=NOW)

    assert counts == {"added": 1, "updated": 1, "unchanged": 0, "removed": 1}
    assert keep.priority == 2
    assert keep.valid_until == datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert drop.status == "resolved"
    added = [n for n in repo.situational_notes.values() if n.source == "webui"]
    assert len(added) == 1 and added[0].subject == "flight to Osaka"


@pytest.mark.asyncio
async def test_bad_line_writes_nothing() -> None:
    note = _note("wedding day", "wedding on 2026-10-04")
    repo = await _repo_with(note)

    with pytest.raises(ValueError):
        await apply_notes_text(repo, "garbage line\n", now=NOW)

    assert note.status == "active"
