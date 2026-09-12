"""Tests for temporal situational notes (D13) in core.soul."""

from datetime import datetime, timedelta, timezone

import pytest

from core.soul.models import SituationalNote
from core.soul.repository import InMemorySoulRepository


@pytest.fixture
def repo() -> InMemorySoulRepository:
    return InMemorySoulRepository()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


async def _insert(
    repo: InMemorySoulRepository,
    note_type: str,
    subject: str = "test",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    now: datetime | None = None,
) -> str:
    note = SituationalNote(
        id=None,
        note_type=note_type,
        subject=subject,
        summary="test summary",
        valid_from=valid_from
        or (now or datetime.now(timezone.utc)) - timedelta(hours=1),
        valid_until=valid_until,
        priority=1,
        confidence=0.8,
        source="test",
        created_at=now or datetime.now(timezone.utc),
        updated_at=now or datetime.now(timezone.utc),
    )
    return await repo.upsert_situational_note(note)


@pytest.mark.asyncio
async def test_insert_and_retrieve(repo: InMemorySoulRepository, now: datetime) -> None:
    note_id = await _insert(repo, "EVENT", valid_until=now + timedelta(days=1), now=now)
    notes = await repo.list_active_situational_notes(now=now)
    assert len(notes) == 1
    assert notes[0].note_type == "EVENT"
    assert notes[0].id == note_id


@pytest.mark.asyncio
async def test_active_state_note(repo: InMemorySoulRepository, now: datetime) -> None:
    await _insert(repo, "STATE", valid_from=now - timedelta(hours=1), now=now)
    notes = await repo.list_active_situational_notes(now=now)
    assert len(notes) == 1
    assert notes[0].is_active(now)


@pytest.mark.asyncio
async def test_expired_state_note_filtered(
    repo: InMemorySoulRepository, now: datetime
) -> None:
    await _insert(
        repo,
        "STATE",
        valid_from=now - timedelta(hours=5),
        valid_until=now - timedelta(hours=1),
        now=now,
    )
    notes = await repo.list_active_situational_notes(now=now)
    assert len(notes) == 0


@pytest.mark.asyncio
async def test_instant_note_not_yet_active(
    repo: InMemorySoulRepository, now: datetime
) -> None:
    future = now + timedelta(hours=2)
    await _insert(repo, "INSTANT", valid_from=future, now=now)
    notes = await repo.list_active_situational_notes(now=now)
    assert len(notes) == 0


@pytest.mark.asyncio
async def test_interval_note_active(
    repo: InMemorySoulRepository, now: datetime
) -> None:
    await _insert(
        repo,
        "INTERVAL",
        valid_from=now - timedelta(hours=1),
        valid_until=now + timedelta(hours=1),
        now=now,
    )
    notes = await repo.list_active_situational_notes(now=now)
    assert len(notes) == 1
    assert notes[0].is_active(now)


@pytest.mark.asyncio
async def test_archive_expired(repo: InMemorySoulRepository, now: datetime) -> None:
    await _insert(
        repo,
        "STATE",
        valid_from=now - timedelta(hours=5),
        valid_until=now - timedelta(hours=1),
        now=now,
    )
    archived = await repo.archive_expired_situational_notes(now=now)
    assert archived >= 1
    notes = await repo.list_active_situational_notes(now=now)
    assert len(notes) == 0


@pytest.mark.asyncio
async def test_resolve_existing(repo: InMemorySoulRepository, now: datetime) -> None:
    note_id = await _insert(repo, "EVENT", valid_until=now + timedelta(days=1), now=now)
    notes_before = await repo.list_active_situational_notes(now=now)
    assert len(notes_before) == 1
    await repo.resolve_situational_note(note_id, new_status="resolved")
    notes_after = await repo.list_active_situational_notes(now=now)
    assert len(notes_after) == 0


@pytest.mark.asyncio
async def test_resolve_nonexistent(repo: InMemorySoulRepository) -> None:
    await repo.resolve_situational_note("nonexistent-id", new_status="resolved")


@pytest.mark.asyncio
async def test_upsert_updates_existing(
    repo: InMemorySoulRepository, now: datetime
) -> None:
    note = SituationalNote(
        id=None,
        note_type="STATE",
        subject="sick",
        summary="feels unwell",
        valid_from=now,
        valid_until=now + timedelta(hours=24),
        priority=2,
        confidence=0.9,
        source="test",
        created_at=now,
        updated_at=now,
    )
    note_id = await repo.upsert_situational_note(note)

    note.id = note_id
    note.subject = "recovered"
    await repo.upsert_situational_note(note)

    notes = await repo.list_active_situational_notes(now=now)
    found = [n for n in notes if n.id == note_id]
    assert len(found) == 1
    assert found[0].subject == "recovered"


@pytest.mark.asyncio
async def test_same_circumstance_upserts_one_row(
    repo: InMemorySoulRepository, now: datetime
) -> None:
    """Storing the same circumstance twice must update one row, not add a second.

    Regression: the note id was generated fresh on every upsert, so the
    repository's ``ON CONFLICT (id) DO UPDATE`` was unreachable. The debrief
    re-reads the same recent transcript on each run, so a single mentioned event
    accumulated one duplicate note per compile cycle — five identical rows were
    observed in a live store within an hour.
    """
    first = SituationalNote(
        id="",
        note_type="EVENT",
        subject="upcoming_event",
        summary="User mentioned an upcoming event on 2026-09-14",
        valid_from=now,
        valid_until=now + timedelta(days=7),
    )
    # Same circumstance, seen again one hour later: the validity window slides,
    # which is exactly what made the duplicates look like distinct notes.
    second = SituationalNote(
        id="",
        note_type="EVENT",
        subject="upcoming_event",
        summary="User mentioned an upcoming event on 2026-09-14",
        valid_from=now + timedelta(hours=1),
        valid_until=now + timedelta(days=7, hours=1),
    )

    first_id = await repo.upsert_situational_note(first)
    second_id = await repo.upsert_situational_note(second)

    assert first_id == second_id
    notes = await repo.list_active_situational_notes(now=now + timedelta(hours=2))
    assert len(notes) == 1
    # The re-mention refreshes the window rather than spawning a sibling.
    assert notes[0].valid_until == now + timedelta(days=7, hours=1)


@pytest.mark.asyncio
async def test_different_circumstances_stay_separate(
    repo: InMemorySoulRepository, now: datetime
) -> None:
    """Deduplication must not collapse genuinely different circumstances."""
    for summary in (
        "User mentioned an upcoming event on 2026-09-14",
        "User mentioned an upcoming event on 2026-09-15",
    ):
        await repo.upsert_situational_note(
            SituationalNote(
                id="",
                note_type="EVENT",
                subject="upcoming_event",
                summary=summary,
                valid_from=now,
                valid_until=now + timedelta(days=7),
            )
        )

    notes = await repo.list_active_situational_notes(now=now + timedelta(hours=1))
    assert len(notes) == 2
