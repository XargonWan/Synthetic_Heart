from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.soul.compiler import NoopEmbedder
from core.soul.repository import InMemorySoulRepository
from core.soul.service import SoulService


def _service() -> tuple[SoulService, InMemorySoulRepository]:
    repo = InMemorySoulRepository()
    return SoulService(repo, NoopEmbedder()), repo


_DAY = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_ingest_is_idempotent_and_keeps_retrieval_history() -> None:
    service, repo = _service()

    first = await service.ingest_summary(
        kind="journal",
        source_key="diary:7",
        text="I spent the evening thinking about the garden.",
        event_timestamp=_DAY,
    )
    assert first is not None and first.status == "created"

    repo.memcells["journal:diary:7"].retrieval_count = 4

    again = await service.ingest_summary(
        kind="journal",
        source_key="diary:7",
        text="I spent the evening thinking about the garden.",
        event_timestamp=_DAY,
    )
    assert again is not None and again.status == "unchanged"

    grown = await service.ingest_summary(
        kind="journal",
        source_key="diary:7",
        text="I spent the evening thinking about the garden. Then I wrote it down.",
        event_timestamp=_DAY,
    )
    assert grown is not None and grown.status == "updated"
    assert len(repo.memcells) == 1
    cell = repo.memcells["journal:diary:7"]
    assert cell.retrieval_count == 4
    assert cell.source_kind == "journal"
    assert cell.source_key == "diary:7"
    # Stamped, so recall does not hide it behind the "undistilled" filter.
    assert cell.distilled_at is not None


@pytest.mark.asyncio
async def test_ingest_ignores_empty_text_and_rejects_the_chat_kind() -> None:
    service, repo = _service()

    assert (
        await service.ingest_summary(
            kind="journal", source_key="x", text="   ", event_timestamp=_DAY
        )
        is None
    )
    with pytest.raises(ValueError):
        await service.ingest_summary(
            kind="chat", source_key="x", text="hello there", event_timestamp=_DAY
        )
    assert repo.memcells == {}


@pytest.mark.asyncio
async def test_recall_can_be_limited_to_a_source_and_a_date_range() -> None:
    service, _ = _service()
    await service.ingest_summary(
        kind="journal",
        source_key="a",
        text="the garden roses bloomed early this spring",
        event_timestamp=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    await service.ingest_summary(
        kind="legacy_memory",
        source_key="b",
        text="the garden roses were planted by the old gate",
        event_timestamp=datetime(2025, 9, 1, tzinfo=timezone.utc),
    )

    everything = await service.recall("garden roses")
    assert {m.cell.source_kind for m in everything} == {"journal", "legacy_memory"}

    only_journal = await service.recall("garden roses", sources={"journal"})
    assert [m.cell.source_kind for m in only_journal] == ["journal"]

    in_2025 = await service.recall(
        "garden roses",
        date_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
        date_to=datetime(2025, 12, 31, tzinfo=timezone.utc),
    )
    assert [m.cell.source_kind for m in in_2025] == ["legacy_memory"]


@pytest.mark.asyncio
async def test_recall_ignores_unknown_sources_and_short_queries() -> None:
    service, _ = _service()
    await service.ingest_summary(
        kind="journal",
        source_key="a",
        text="the garden roses bloomed early",
        event_timestamp=_DAY,
    )
    assert await service.recall("ok") == []
    hits = await service.recall("garden roses", sources={"nonsense"})
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_retire_source_removes_the_cell_but_keeps_it_restorable() -> None:
    service, repo = _service()
    await service.ingest_summary(
        kind="journal",
        source_key="ai_diary:3",
        text="a fragment folded into the day",
        event_timestamp=_DAY,
    )

    assert await service.retire_source(kind="journal", source_key="ai_diary:3")
    assert "journal:ai_diary:3" not in repo.memcells
    assert "journal:ai_diary:3" in repo.evicted_memcells
    assert not await service.retire_source(kind="journal", source_key="ai_diary:3")


@pytest.mark.asyncio
async def test_service_recall_applies_the_same_privacy_and_session_rules_as_passive_recall() -> (
    None
):
    from core.soul.models import EmotionalTag, MemCell
    from core.soul.service import register_on_demand_session_prefixes

    register_on_demand_session_prefixes("radio_host_")
    service, repo = _service()
    embedder = NoopEmbedder()

    def _cell(cell_id: str, session: str, visibility: str) -> MemCell:
        return MemCell(
            id=cell_id,
            episodic_trace="the harbour lighthouse story",
            atomic_facts=[],
            emotional_tag=EmotionalTag(
                state_snapshot={"joy": 0.0, "fear": 0.0, "sad": 0.0, "anger": 0.0},
                dominant_emotion="neutral",
                intensity=0.0,
                valence=0.0,
            ),
            foresight_signals=[],
            event_timestamp=_DAY,
            session_id=session,
            consolidated=True,
            distilled_at=_DAY,
            visibility=visibility,
        )

    for cell in (
        _cell("open", "telegram_bot_-5", "shared"),
        _cell("secret", "telegram_bot_111", "private"),
        _cell("system", "diary_merge_-1", "shared"),
        _cell("radio", "radio_host_-2", "shared"),
    ):
        cell.embedding = await embedder.embed(cell.episodic_trace)
        repo.memcells[cell.id] = cell

    def ids(hits):  # type: ignore[no-untyped-def]
        return {h.cell.id for h in hits}

    elsewhere = await service.recall(
        "harbour lighthouse story", session_id="telegram_bot_9"
    )
    assert ids(elsewhere) == {"open"}

    own_chat = await service.recall(
        "harbour lighthouse story", session_id="telegram_bot_111"
    )
    assert ids(own_chat) == {"open", "secret"}

    # No chat given: no private cell, ever.
    assert "secret" not in ids(await service.recall("harbour lighthouse story"))

    # The radio's sessions come back only to a caller that asks for them by prefix.
    asked = await service.recall(
        "harbour lighthouse story", session_prefixes=("radio_host_",)
    )
    assert ids(asked) == {"radio"}


def _long_day(paragraphs: int) -> str:
    return "\n\n".join(
        f"Entry {n}: " + ("the garden roses and the old gate " * 12).strip()
        for n in range(paragraphs)
    )


def test_chunk_text_packs_paragraphs_and_cuts_long_ones_at_whitespace() -> None:
    from core.soul.service import chunk_text

    assert chunk_text("a short note") == ["a short note"]
    assert chunk_text("") == []

    blocks = chunk_text(_long_day(8), target=1500)
    assert len(blocks) > 1
    assert all(len(b) <= 1500 * 1.5 for b in blocks)
    # Nothing is lost or reordered.
    assert " ".join(blocks).split() == " ".join(_long_day(8).split()).split()

    one_blob = "word " * 2000
    pieces = chunk_text(one_blob, target=1500)
    assert len(pieces) > 1 and all(len(p) <= 1500 * 1.5 for p in pieces)


@pytest.mark.asyncio
async def test_a_long_entry_is_filed_as_blocks_and_a_short_one_is_not() -> None:
    service, repo = _service()

    short = await service.ingest_chunked(
        kind="journal", source_key="d:1", text="a short entry", event_timestamp=_DAY
    )
    assert [r.cell_id for r in short] == ["journal:d:1"]

    await service.ingest_chunked(
        kind="journal", source_key="d:2", text=_long_day(8), event_timestamp=_DAY
    )
    ids = sorted(i for i in repo.memcells if i.startswith("journal:d:2"))
    assert len(ids) > 1 and all("#" in i for i in ids)
    assert repo.memcells[ids[0]].source_key == "d:2#0"


@pytest.mark.asyncio
async def test_changing_the_shape_of_an_entry_retires_the_other_shape() -> None:
    service, repo = _service()
    await service.ingest_chunked(
        kind="journal", source_key="d:3", text="a short entry", event_timestamp=_DAY
    )
    await service.ingest_chunked(
        kind="journal", source_key="d:3", text=_long_day(8), event_timestamp=_DAY
    )
    assert "journal:d:3" not in repo.memcells
    assert any(i.startswith("journal:d:3#") for i in repo.memcells)
    assert "journal:d:3" in repo.evicted_memcells  # archived, not destroyed

    count = len([i for i in repo.memcells if i.startswith("journal:d:3#")])
    await service.ingest_chunked(
        kind="journal", source_key="d:3", text=_long_day(5), event_timestamp=_DAY
    )
    after = [i for i in repo.memcells if i.startswith("journal:d:3#")]
    assert 0 < len(after) < count, "the blocks the shorter text no longer needs go"

    await service.ingest_chunked(
        kind="journal", source_key="d:3", text="a short entry", event_timestamp=_DAY
    )
    assert list(i for i in repo.memcells if i.startswith("journal:d:3")) == [
        "journal:d:3"
    ]


@pytest.mark.asyncio
async def test_retire_source_removes_every_block() -> None:
    service, repo = _service()
    await service.ingest_chunked(
        kind="journal", source_key="d:4", text=_long_day(8), event_timestamp=_DAY
    )
    assert await service.retire_source(kind="journal", source_key="d:4") is True
    assert not [i for i in repo.memcells if i.startswith("journal:d:4")]


@pytest.mark.asyncio
async def test_recall_returns_one_block_per_source() -> None:
    """Every block of a day matches the same topic; the day is shown once."""
    service, _ = _service()
    await service.ingest_chunked(
        kind="journal", source_key="d:5", text=_long_day(8), event_timestamp=_DAY
    )
    await service.ingest_chunked(
        kind="journal",
        source_key="d:6",
        text="the garden roses and the old gate were painted",
        event_timestamp=_DAY,
    )
    hits = await service.recall("garden roses old gate", limit=5)
    sources = [
        (h.cell.source_kind, (h.cell.source_key or "").split("#")[0]) for h in hits
    ]
    assert len(sources) == len(set(sources)) == 2
