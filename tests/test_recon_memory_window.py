from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.recon.recon_memory_recollector as recollector
from core.soul.compiler import NoopEmbedder
from core.soul.models import EmotionalTag, MemCell
from core.soul.repository import InMemorySoulRepository
from core.soul.service import SoulService
from core.time_window import parse_time_window


def test_the_window_parser_reads_only_language_neutral_forms() -> None:
    start, end = parse_time_window("2026-03-01/2026-03-31")  # type: ignore[misc]
    assert start == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert end.date().isoformat() == "2026-03-31" and end.hour == 23

    day = parse_time_window("2026-03-10")
    assert day is not None and day[0].day == 10 and day[1].hour == 23

    assert parse_time_window({"start": "2026-01-10", "end": "2026-01-12"}) is not None
    now = datetime.now(timezone.utc)
    back = parse_time_window({"duration": {"days": 7}})
    assert back is not None and abs((back[1] - now).total_seconds()) < 5

    # Words are never interpreted, in any language: that is Recon's job.
    for words in ("yesterday", "last week", "7d", "48 hours", "ayer", "先週", "ieri"):
        assert parse_time_window(words) is None
    assert parse_time_window(None) is None


def _cell(
    cell_id: str, kind: str, text: str, when: datetime, session: str, visibility: str
) -> MemCell:
    return MemCell(
        id=cell_id,
        episodic_trace=text,
        atomic_facts=[],
        emotional_tag=EmotionalTag(
            state_snapshot={"joy": 0.0, "fear": 0.0, "sad": 0.0, "anger": 0.0},
            dominant_emotion="neutral",
            intensity=0.0,
            valence=0.0,
        ),
        foresight_signals=[],
        event_timestamp=when,
        session_id=session,
        consolidated=True,
        distilled_at=when,
        source_kind=kind,
        visibility=visibility,
    )


async def _service() -> SoulService:
    repo = InMemorySoulRepository()
    embedder = NoopEmbedder()
    march = datetime(2026, 3, 10, tzinfo=timezone.utc)
    for cell in (
        _cell(
            "in",
            "journal",
            "we planned the garden trip in March",
            march,
            "journal",
            "shared",
        ),
        _cell(
            "old",
            "journal",
            "we planned the garden trip long ago",
            datetime(2025, 9, 1, tzinfo=timezone.utc),
            "journal",
            "shared",
        ),
        _cell(
            "secret",
            "chat",
            "told in confidence: the garden trip is a surprise",
            march,
            "telegram_bot_111",
            "private",
        ),
        _cell(
            "system",
            "chat",
            "garden trip consolidation transcript",
            march,
            "diary_merge_-1",
            "shared",
        ),
    ):
        cell.embedding = await embedder.embed(cell.episodic_trace)
        repo.memcells[cell.id] = cell
    return SoulService(repo, embedder)


@pytest.mark.asyncio
async def test_a_dated_question_recalls_that_period_and_nothing_private_or_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _service()
    monkeypatch.setattr(SoulService, "from_registry", classmethod(lambda cls: service))
    soul = SimpleNamespace(session_id_for=lambda path: path.replace("/", "_"))
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {"soul_plugin": soul})

    async def _no_legacy(**_: Any) -> list[dict]:
        return []

    monkeypatch.setattr(recollector, "search_memories", _no_legacy)

    plugin = recollector.ReconMemoryRecollectorPlugin()
    message = SimpleNamespace(interface_path="telegram_bot/999")
    contributions = await plugin.parse_recon_response(
        {
            "tags": ["trip"],
            "keywords": ["garden", "trip"],
            "time_window": "2026-03-01/2026-03-31",
        },
        message=message,
        text="what did we plan for the garden trip in March?",
    )

    ids = {c["content"]["id"] for c in contributions}
    assert ids == {"in"}, "only the shared cell of that period may come back"
    assert contributions[0]["content"]["source"] == "my_diary"


@pytest.mark.asyncio
async def test_the_private_memory_comes_back_in_its_own_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _service()
    monkeypatch.setattr(SoulService, "from_registry", classmethod(lambda cls: service))
    soul = SimpleNamespace(session_id_for=lambda path: path.replace("/", "_"))
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {"soul_plugin": soul})

    async def _no_legacy(**_: Any) -> list[dict]:
        return []

    monkeypatch.setattr(recollector, "search_memories", _no_legacy)

    contributions = (
        await recollector.ReconMemoryRecollectorPlugin().parse_recon_response(
            {"keywords": ["garden", "trip"], "time_window": "2026-03-01/2026-03-31"},
            message=SimpleNamespace(interface_path="telegram_bot/111"),
            text="the garden trip",
        )
    )
    assert "secret" in {c["content"]["id"] for c in contributions}


@pytest.mark.asyncio
async def test_without_a_window_nothing_is_added_from_soul(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _service()
    monkeypatch.setattr(SoulService, "from_registry", classmethod(lambda cls: service))
    seen: list[dict] = []

    async def _legacy(**kwargs: Any) -> list[dict]:
        seen.append(kwargs)
        return [
            {"source": "memories", "id": 1, "timestamp": "2026-03-10", "snippet": "x"}
        ]

    monkeypatch.setattr(recollector, "search_memories", _legacy)

    contributions = (
        await recollector.ReconMemoryRecollectorPlugin().parse_recon_response(
            {"tags": ["trip"], "keywords": ["garden"]},
            message=SimpleNamespace(interface_path="telegram_bot/999"),
            text="the garden",
        )
    )
    assert seen, "the keyword search still runs"
    assert [c["content"]["id"] for c in contributions] == [1]


@pytest.mark.asyncio
async def test_keyword_hits_outside_the_window_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _legacy(**_: Any) -> list[dict]:
        return [
            {
                "source": "memories",
                "id": 1,
                "timestamp": "2026-03-10T10:00:00",
                "snippet": "in",
            },
            {
                "source": "memories",
                "id": 2,
                "timestamp": "2025-01-01T10:00:00",
                "snippet": "out",
            },
            {"source": "memories", "id": 3, "timestamp": None, "snippet": "undated"},
        ]

    monkeypatch.setattr(recollector, "search_memories", _legacy)
    monkeypatch.setattr(SoulService, "from_registry", classmethod(lambda cls: None))

    contributions = (
        await recollector.ReconMemoryRecollectorPlugin().parse_recon_response(
            {"keywords": ["x"], "time_window": "2026-03-01/2026-03-31"},
            message=SimpleNamespace(interface_path="telegram_bot/999"),
            text="what happened in March",
        )
    )
    assert [c["content"]["id"] for c in contributions] == [1]


def test_the_instruction_states_today_and_asks_for_an_optional_window() -> None:
    instruction = recollector.ReconMemoryRecollectorPlugin().get_recon_instruction()
    assert datetime.now(timezone.utc).date().isoformat() in instruction
    assert "time_window" in instruction and "ABSOLUTE ISO interval" in instruction
    # No word vocabulary is offered: the model resolves the period itself.
    assert "yesterday" not in instruction and "last week" not in instruction
