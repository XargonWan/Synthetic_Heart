from datetime import date, datetime, timezone

import pytest

from core.soul.compiler import (
    NoopEmbedder,
    RuleBasedDspBuilder,
    RuleBasedSummaryBuilder,
    SoulCompiler,
)
from core.soul.models import EmotionalTag, MemCell
from core.soul.repository import InMemorySoulRepository
from core.soul.schemas import DspExtractionModel, MemCellExtractionModel


class FakeMemCellExtractor:
    async def extract_memcells(
        self, *, transcript: str, current_date: date
    ) -> list[MemCellExtractionModel]:
        return [
            MemCellExtractionModel.model_validate(
                {
                    "episodic_trace": "User said they felt anxious last week.",
                    "atomic_facts": ["User|feels_anxious_about|inspection next week"],
                    "emotional_tag": {
                        "state_snapshot": {
                            "joy": 0.1,
                            "fear": 0.4,
                            "sad": 0.2,
                            "anger": 0.0,
                        },
                        "dominant_emotion": "fear",
                        "intensity": 0.4,
                        "valence": -0.2,
                    },
                    "foresight_signals": [
                        {
                            "content": "User may be anxious about inspection",
                            "valid_until": date(2026, 4, 24),
                            "trigger": "inspection_date",
                            "emotional_implication": {"fear": 0.3},
                        }
                    ],
                    "timestamp": datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
                }
            )
        ]


class FakeDspExtractor:
    async def extract_dsp(
        self, *, transcript: str, current_date: date
    ) -> DspExtractionModel:
        return DspExtractionModel(
            user_facts=["Building a voice AI system"],
            user_preferences=["Prefers concise technical responses"],
            ai_self_facts=[],
        )


class FakeNoFactsExtractor:
    async def extract_memcells(
        self, *, transcript: str, current_date: date
    ) -> list[MemCellExtractionModel]:
        return [
            MemCellExtractionModel.model_validate(
                {
                    "episodic_trace": "We discussed refactoring and test priorities.",
                    "atomic_facts": [],
                    "emotional_tag": {
                        "state_snapshot": {
                            "joy": 0.0,
                            "fear": 0.0,
                            "sad": 0.0,
                            "anger": 0.0,
                        },
                        "dominant_emotion": "neutral",
                        "intensity": 0.0,
                        "valence": 0.0,
                    },
                    "foresight_signals": [],
                    "timestamp": datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
                }
            )
        ]


@pytest.mark.asyncio
async def test_post_session_compile_resolves_relative_time() -> None:
    repo = InMemorySoulRepository()
    compiler = SoulCompiler(
        repository=repo,
        memcell_extractor=FakeMemCellExtractor(),
        dsp_extractor=FakeDspExtractor(),
        dsp_builder=RuleBasedDspBuilder(),
        summary_builder=RuleBasedSummaryBuilder(),
        embedder=NoopEmbedder(),
    )

    ids = await compiler.post_session_compile(
        current_date=date(2026, 4, 17),
        transcript="sample transcript",
        session_id="session-1",
    )

    assert len(ids) == 1
    cell = repo.memcells[ids[0]]
    assert cell.embedding is not None
    assert len(cell.embedding) == 768
    assert "week of 2026-04-06" in cell.episodic_trace
    assert "week of 2026-04-20" in cell.atomic_facts[0]


@pytest.mark.asyncio
async def test_async_consolidate_creates_scene_and_kg() -> None:
    repo = InMemorySoulRepository()
    compiler = SoulCompiler(
        repository=repo,
        memcell_extractor=FakeMemCellExtractor(),
        dsp_extractor=FakeDspExtractor(),
        dsp_builder=RuleBasedDspBuilder(),
        summary_builder=RuleBasedSummaryBuilder(),
        embedder=NoopEmbedder(),
    )

    await compiler.post_session_compile(
        current_date=date(2026, 4, 17),
        transcript="sample transcript",
        session_id="session-1",
    )
    scene_ids = await compiler.async_consolidate()

    assert len(scene_ids) == 1
    assert len(repo.scenes) == 1
    assert len(repo.kg_triples) == 1


@pytest.mark.asyncio
async def test_nightly_rollup_bootstraps_dsp() -> None:
    repo = InMemorySoulRepository()
    compiler = SoulCompiler(
        repository=repo,
        memcell_extractor=FakeMemCellExtractor(),
        dsp_extractor=FakeDspExtractor(),
        dsp_builder=RuleBasedDspBuilder(),
        summary_builder=RuleBasedSummaryBuilder(),
        embedder=NoopEmbedder(),
        dsp_bootstrap_min_extractions=2,
    )

    result_1 = await compiler.nightly_rollup(
        current_date=date(2026, 4, 17),
        transcript="day 1",
        session_id="session-1",
    )
    result_2 = await compiler.nightly_rollup(
        current_date=date(2026, 4, 18),
        transcript="day 2",
        session_id="session-2",
    )

    assert result_1["dsp_updated"] == 0
    assert result_2["dsp_updated"] == 1
    assert repo.active_dsp is not None
    assert "<user_profile>" in repo.active_dsp.content


@pytest.mark.asyncio
async def test_post_session_compile_adds_fallback_atomic_fact() -> None:
    repo = InMemorySoulRepository()
    compiler = SoulCompiler(
        repository=repo,
        memcell_extractor=FakeNoFactsExtractor(),
        dsp_extractor=FakeDspExtractor(),
        dsp_builder=RuleBasedDspBuilder(),
        summary_builder=RuleBasedSummaryBuilder(),
        embedder=NoopEmbedder(),
    )

    ids = await compiler.post_session_compile(
        current_date=date(2026, 4, 18),
        transcript="discussion",
        session_id="session-2",
    )

    assert len(ids) == 1
    facts = repo.memcells[ids[0]].atomic_facts
    assert facts
    assert facts[0].startswith("Conversation|summary|")


@pytest.mark.asyncio
async def test_backfill_embeddings_updates_missing_vectors() -> None:
    repo = InMemorySoulRepository()
    cell = MemCell(
        id="session-3:1",
        episodic_trace="Need to follow up tomorrow about release checks.",
        atomic_facts=[
            "Conversation|summary|Need to follow up tomorrow about release checks"
        ],
        emotional_tag=EmotionalTag(
            state_snapshot={"joy": 0.0, "fear": 0.0, "sad": 0.0, "anger": 0.0},
            dominant_emotion="neutral",
            intensity=0.0,
            valence=0.0,
        ),
        foresight_signals=[],
        timestamp=datetime(2026, 4, 18, 13, 0, tzinfo=timezone.utc),
        session_id="session-3",
        embedding=None,
    )
    repo.memcells[cell.id] = cell

    compiler = SoulCompiler(
        repository=repo,
        memcell_extractor=FakeMemCellExtractor(),
        dsp_extractor=FakeDspExtractor(),
        dsp_builder=RuleBasedDspBuilder(),
        summary_builder=RuleBasedSummaryBuilder(),
        embedder=NoopEmbedder(),
    )

    updated = await compiler.backfill_embeddings(limit=10)

    assert updated == 1
    assert repo.memcells[cell.id].embedding is not None
    assert len(repo.memcells[cell.id].embedding or []) == 768
