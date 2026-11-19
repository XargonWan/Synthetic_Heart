from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from core.soul.compiler import NoopEmbedder
from core.soul.legacy_ingest import diary_emotion, diary_text, ingest_legacy
from core.soul.repository import InMemorySoulRepository
from core.soul.service import SoulService


def _at(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 3, day, hour, tzinfo=timezone.utc)


class _Cursor:
    """Answers each legacy-table SELECT with canned rows, in the column order asked."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables
        self._rows: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, params: tuple = ()) -> None:
        columns_part, _, rest = sql.partition(" FROM ")
        columns = [c.strip() for c in columns_part.replace("SELECT", "").split(",")]
        table = rest.split()[0]
        self._rows = [
            tuple(row.get(c) for c in columns) for row in self._tables.get(table, [])
        ]

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


def _tables() -> dict[str, list[dict[str, Any]]]:
    return {
        "memories": [
            {  # a compaction summary: stands for March 1st
                "id": 1,
                "content": "A quiet day about the garden.",
                "author": "grillo",
                "source": "compaction",
                "created_at": _at(1),
                "emotion": "calm",
                "intensity": 4,
            },
            {  # a voice memory
                "id": 2,
                "content": "We chatted by voice about the trip.",
                "author": "synth",
                "source": "voice",
                "created_at": _at(2),
                "emotion": None,
                "intensity": 0,
            },
            {  # a raw copy of chat: never carried over
                "id": 3,
                "content": "someone said hello",
                "author": "observer",
                "source": "grillo_observer",
                "created_at": _at(2),
                "emotion": None,
                "intensity": 0,
            },
        ],
        "ai_diary": [
            {
                "id": 10,
                "content": "Wrote about the market.",
                "personal_thought": "I liked the noise.",
                "emotions": '[{"type": "joy", "intensity": 8}]',
                "interaction_summary": "We talked about the market.",
                "created_at": _at(5),
            }
        ],
        "ai_diary_archive": [
            {  # same day as a compaction summary -> skipped
                "id": 20,
                "content": "raw fragment of March 1st",
                "personal_thought": None,
                "emotions": "[]",
                "interaction_summary": None,
                "created_at": _at(1, 15),
            },
            {  # same day as a live diary row -> skipped
                "id": 21,
                "content": "fragment folded into March 5th",
                "personal_thought": None,
                "emotions": "[]",
                "interaction_summary": None,
                "created_at": _at(5, 8),
            },
            {  # two fragments of one otherwise-unfiled day -> ONE cell
                "id": 22,
                "content": "first part of March 7th",
                "personal_thought": None,
                "emotions": "[]",
                "interaction_summary": None,
                "created_at": _at(7, 9),
            },
            {
                "id": 23,
                "content": "second part of March 7th",
                "personal_thought": None,
                "emotions": '[{"type": "sad", "intensity": 6}]',
                "interaction_summary": None,
                "created_at": _at(7, 18),
            },
        ],
    }


def test_diary_text_joins_the_columns_without_repeating_itself() -> None:
    row = {
        "interaction_summary": "We talked about the market.",
        "personal_thought": "I liked the noise.",
        "content": "We talked about the market. I liked the noise.",
    }
    assert diary_text(row) == "We talked about the market. I liked the noise."


def test_diary_emotion_picks_the_strongest_and_scales_to_one() -> None:
    row = {
        "emotions": '[{"type": "joy", "intensity": 3}, {"type": "love", "intensity": 8}]'
    }
    assert diary_emotion(row) == ("love", 0.8)
    assert diary_emotion({"emotions": "not json"}) == ("neutral", 0.0)
    assert diary_emotion({"emotions": None}) == ("neutral", 0.0)


@pytest.mark.asyncio
async def test_ingest_files_the_right_rows_and_is_idempotent() -> None:
    repo = InMemorySoulRepository()
    service = SoulService(repo, NoopEmbedder())
    cursor = _Cursor(_tables())

    first = await ingest_legacy(service, cursor)

    ids = set(repo.memcells)
    assert "diary_day:memories:1" in ids  # compaction summary
    assert "legacy_memory:memories:2" in ids  # voice memory
    assert "diary_day:memories:3" not in ids and "legacy_memory:memories:3" not in ids
    assert "journal:ai_diary:10" in ids
    assert "journal:ai_diary_archive:day:2026-03-07" in ids
    assert not any(i.startswith("journal:ai_diary_archive:day:2026-03-01") for i in ids)
    assert not any(i.startswith("journal:ai_diary_archive:day:2026-03-05") for i in ids)
    assert len(ids) == 4
    assert first.created == 4 and first.unchanged == 0
    assert first.skipped["memories: observer copy"] == 1

    # The two March 7th fragments became one cell holding both.
    day = repo.memcells["journal:ai_diary_archive:day:2026-03-07"]
    assert "first part" in day.episodic_trace and "second part" in day.episodic_trace
    assert day.emotional_tag.dominant_emotion == "sad"

    second = await ingest_legacy(service, _Cursor(_tables()))
    assert second.created == 0 and second.updated == 0 and second.unchanged == 4
    assert len(repo.memcells) == 4


@pytest.mark.asyncio
async def test_dry_run_writes_nothing() -> None:
    repo = InMemorySoulRepository()
    report = await ingest_legacy(
        SoulService(repo, NoopEmbedder()), _Cursor(_tables()), dry_run=True
    )
    assert repo.memcells == {}
    assert report.created == 4
