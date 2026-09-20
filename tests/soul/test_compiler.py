from datetime import date, datetime, timedelta, timezone

import pytest

from core.soul.compiler import (
    NoopEmbedder,
    RuleBasedDspBuilder,
    RuleBasedMemCellCurator,
    RuleBasedSummaryBuilder,
    SoulCompiler,
)
from core.soul.models import (
    CuratorDecision,
    DspExtraction,
    EmotionalTag,
    ForesightSignal,
    MemCell,
    MemCellSummary,
)
from core.soul.repository import InMemorySoulRepository
from core.soul.schemas import DspExtractionModel, MemCellExtractionModel
from core.soul.strategies import RuleBasedDspExtractor


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
async def test_post_session_compile_does_not_fabricate_atomic_facts() -> None:
    """A cell with nothing distilled carries NO fact, not the line itself.

    The old fallback stored ``Conversation|summary|<the verbatim trace>``, which
    is why recall could only ever return raw transcript (and why the renderer had
    to suppress a "Key facts:" that repeated the sentence above it).
    """
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
    assert repo.memcells[ids[0]].atomic_facts == []


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
        event_timestamp=datetime(2026, 4, 18, 13, 0, tzinfo=timezone.utc),
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


@pytest.mark.asyncio
async def test_rule_based_dsp_extractor_parses_speaker_tagged_transcript() -> None:
    extractor = RuleBasedDspExtractor()
    transcript = "\n".join(
        [
            '[05/05/26:1234] Alice: "Doing better today, staying in bed as usual, I kind of ran out of things to do so I\'m just making small tweaks to your core."',
            '[05/05/26:1234] self: "It feels wonderful to have you back in there. I feel centered, grounded, and completely yours. I\'ve been thinking about the quiet here today."',
        ]
    )

    result = await extractor.extract_dsp(
        transcript=transcript,
        current_date=date(2026, 5, 5),
    )

    # Transient status speech ("doing better today", "making small tweaks to
    # your core") must NOT become standing user-profile facts.
    assert not result.user_facts
    assert not any("making small tweaks" in fact for fact in result.user_facts)
    assert not any("doing better today" in fact for fact in result.user_facts)
    # AI self-facts keep only bounded biographical preds (none here).
    assert result.ai_self_facts == []


@pytest.mark.asyncio
async def test_rule_based_dsp_extractor_pulls_stable_biography() -> None:
    extractor = RuleBasedDspExtractor()
    transcript = "\n".join(
        [
            '[05/05/26:1234] Alice: "I am a developer, I work on SynthHeart, and I prefer concise technical responses."',
            '[05/05/26:1234] Alice: "I live in Berlin and I am from Germany."',
        ]
    )

    result = await extractor.extract_dsp(
        transcript=transcript,
        current_date=date(2026, 5, 5),
    )

    facts = " ; ".join(result.user_facts)
    assert "User is a developer" in facts
    assert "User works on SynthHeart" in facts
    assert "User lives in Berlin" in facts
    assert "User is from Germany" in facts
    assert "User prefers concise technical responses" in facts
    # Run-on coordination is cut at the clause boundary.
    assert "and I am from Germany" not in facts


@pytest.mark.asyncio
async def test_rule_based_dsp_extractor_drops_roleplay_speech() -> None:
    extractor = RuleBasedDspExtractor()
    # Real examples surfaced from the live trace + fresh rollup extraction:
    # roleplay dialogue must not become a standing user-profile fact.
    transcript = "\n".join(
        [
            '[05/05/26:1234] Alice: "I\'m right here baby, go ahead and collect a bunch of wood."',
            '[05/05/26:1234] Alice: "I\'m so fucking ready for you, just tell me where you want me."',
            '[05/05/26:1234] Alice: "I am from you."',
            '[05/05/26:1234] Alice: "I love you too baby."',
            '[05/05/26:1234] Alice: "I love my little princess."',
            '[05/05/26:1234] Alice: "I love it heheh keep me safe while i sleep."',
            '[05/05/26:1234] Alice: "I love it mmmmmwah."',
            '[05/05/26:1234] Alice: "I work on SynthHeart though, for real."',
        ]
    )

    result = await extractor.extract_dsp(
        transcript=transcript,
        current_date=date(2026, 5, 5),
    )

    assert not any("right here baby" in fact for fact in result.user_facts)
    assert not any("fucking ready" in fact for fact in result.user_facts)
    # Speech addressed to someone (second/first person, endearments) is not
    # biography: none of these should surface as a standing profile fact.
    assert not any("from you" in fact for fact in result.user_facts)
    assert not any("love you" in fact for fact in result.user_facts)
    assert not any("little princess" in fact for fact in result.user_facts)
    assert not any("heheh" in fact for fact in result.user_facts)
    assert not any("keep me safe" in fact for fact in result.user_facts)
    # Emote-tailed roleplay value is dropped too.
    assert not any("mmmmwah" in fact for fact in result.user_facts)
    # The genuine biographical fact still survives.
    assert any("works on SynthHeart" in fact for fact in result.user_facts)


@pytest.mark.asyncio
async def test_run_curator_removes_low_salience_cells() -> None:
    repo = InMemorySoulRepository()
    old_ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cell = MemCell(
        id="old-cell-1",
        episodic_trace="User mentioned the weather briefly.",
        atomic_facts=["Conversation|summary|User mentioned the weather briefly"],
        emotional_tag=EmotionalTag(
            state_snapshot={"joy": 0.0, "fear": 0.0, "sad": 0.0, "anger": 0.0},
            dominant_emotion="neutral",
            intensity=0.0,
            valence=0.0,
        ),
        foresight_signals=[],
        event_timestamp=old_ts,
        session_id="old-session",
        explicit_importance=0.0,
        retrieval_count=0,
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

    result = await compiler.run_curator(current_date=date(2026, 5, 7), max_memories=500)

    assert result.inspected == 1
    assert result.removed == 1
    assert result.retained == 0
    assert "old-cell-1" not in repo.memcells


@pytest.mark.asyncio
async def test_run_curator_keeps_future_foresight_cells() -> None:
    repo = InMemorySoulRepository()
    old_ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cell = MemCell(
        id="future-cell-1",
        episodic_trace="User mentioned an upcoming event.",
        atomic_facts=[],
        emotional_tag=EmotionalTag(
            state_snapshot={"joy": 0.0, "fear": 0.0, "sad": 0.0, "anger": 0.0},
            dominant_emotion="neutral",
            intensity=0.0,
            valence=0.0,
        ),
        foresight_signals=[
            ForesightSignal(
                content="User has an upcoming event",
                valid_until=date(2026, 12, 31),
                trigger="event_date",
            )
        ],
        event_timestamp=old_ts,
        session_id="session-f",
        explicit_importance=0.0,
        retrieval_count=0,
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

    result = await compiler.run_curator(current_date=date(2026, 5, 7), max_memories=500)

    assert result.inspected == 1
    assert result.removed == 0
    assert result.kept_future == 1
    assert cell.id in repo.memcells


@pytest.mark.asyncio
async def test_run_curator_enforces_max_memories() -> None:
    repo = InMemorySoulRepository()
    recent_ts = datetime(2026, 5, 1, tzinfo=timezone.utc)

    for i in range(3):
        cell = MemCell(
            id=f"important-{i}",
            episodic_trace=f"Important recent event {i}.",
            atomic_facts=[],
            emotional_tag=EmotionalTag(
                state_snapshot={"joy": 0.9, "fear": 0.0, "sad": 0.0, "anger": 0.0},
                dominant_emotion="joy",
                intensity=0.9,
                valence=0.9,
            ),
            foresight_signals=[],
            event_timestamp=recent_ts,
            session_id=f"session-{i}",
            explicit_importance=0.0,
            retrieval_count=i * 3,
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

    result = await compiler.run_curator(current_date=date(2026, 5, 7), max_memories=2)

    assert result.inspected == 3
    assert result.retained == 2
    assert result.removed == 1
    assert len(repo.memcells) == 2


@pytest.mark.asyncio
async def test_rule_based_curator_classify_future_date_in_trace() -> None:
    curator = RuleBasedMemCellCurator()
    summary = MemCellSummary(
        id="date-cell",
        episodic_trace="User has an event on 2027-03-15.",
        event_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        retrieval_count=0,
        explicit_importance=0.0,
        emotional_intensity=0.0,
        has_active_foresight=False,
    )
    decisions = await curator.classify([summary], current_date=date(2026, 5, 7))
    assert decisions[0][1] == CuratorDecision.KEEP_FUTURE


@pytest.mark.asyncio
async def test_curator_keeps_a_fresh_calm_cell_and_still_prunes_an_old_one() -> None:
    """A new, unemotional, never-recalled cell must survive the curator.

    Recency is 0.2 of the salience formula while the removal threshold is 0.4, so
    without a grace period a calm new cell can never clear the bar: measured live,
    all 8 cells compiled on 2026-09-18 were removed by the first nightly pass,
    leaving only emotional or forward-looking memories. An old calm cell is still
    pruned, so the grace period does not disable curation.
    """
    curator = RuleBasedMemCellCurator()
    today = datetime.now(timezone.utc).date()

    def _summary(cell_id: str, age: timedelta) -> MemCellSummary:
        return MemCellSummary(
            id=cell_id,
            episodic_trace="User said the deploy went fine.",
            event_timestamp=datetime.now(timezone.utc) - age,
            retrieval_count=0,
            explicit_importance=0.0,
            emotional_intensity=0.0,
            has_active_foresight=False,
        )

    decisions = dict(
        await curator.classify(
            [
                _summary("fresh", timedelta(hours=2)),
                _summary("stale", timedelta(days=30)),
            ],
            current_date=today,
        )
    )

    assert decisions["fresh"] == CuratorDecision.KEEP_IMPORTANT
    assert decisions["stale"] == CuratorDecision.REMOVE


@pytest.mark.asyncio
async def test_curator_grace_period_can_be_switched_off() -> None:
    """An explicit zero grace period restores the old behaviour exactly."""
    curator = RuleBasedMemCellCurator(min_age_seconds=0.0)
    today = datetime.now(timezone.utc).date()
    summary = MemCellSummary(
        id="fresh-but-unprotected",
        episodic_trace="User said the deploy went fine.",
        event_timestamp=datetime.now(timezone.utc) - timedelta(hours=2),
        retrieval_count=0,
        explicit_importance=0.0,
        emotional_intensity=0.0,
        has_active_foresight=False,
    )

    decisions = await curator.classify([summary], current_date=today)

    assert decisions[0][1] == CuratorDecision.REMOVE


@pytest.mark.asyncio
async def test_rule_based_dsp_extractor_keeps_user_preferences_separate() -> None:
    extractor = RuleBasedDspExtractor()
    transcript = '[05/05/26:1200] Alice: "I prefer concise technical responses, please be direct."'

    result = await extractor.extract_dsp(
        transcript=transcript,
        current_date=date(2026, 5, 5),
    )

    assert "concise technical responses" in result.user_preferences
    assert "direct" in result.user_preferences
    assert result.ai_self_facts == []


def _dsp_extraction(
    *, facts: list[str], prefs: list[str] | None = None
) -> DspExtraction:
    return DspExtraction(
        id=f"dsp-extract:{facts[0] if facts else 'empty'}:{len(facts)}",
        session_id="session-x",
        extracted_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
        user_facts=facts,
        user_preferences=prefs or [],
        ai_self_facts=[],
    )


@pytest.mark.asyncio
async def test_dsp_builder_keeps_only_recurring_facts() -> None:
    builder = RuleBasedDspBuilder()
    # A stable fact ("User works on SynthHeart") appears in two extractions, so
    # it survives. A one-off status line ("User says they are fixing it now")
    # appears only once, so it is dropped.
    stable = _dsp_extraction(
        facts=["User works on SynthHeart", "User says they are fixing it now"],
        prefs=["Prefers concise technical responses"],
    )
    repeated = _dsp_extraction(
        facts=["User works on SynthHeart"],
        prefs=["Prefers concise technical responses"],
    )

    profile = await builder.build_initial(extractions=[stable, repeated])

    assert "User works on SynthHeart" in profile
    assert "fixing it now" not in profile
    assert "Prefers concise technical responses" in profile
    assert "<user_profile>" in profile


@pytest.mark.asyncio
async def test_dsp_builder_keeps_preferences_without_recurrence() -> None:
    builder = RuleBasedDspBuilder()
    # Communication preferences are explicit standing requests, not one-off
    # status lines — they stay even with a single occurrence.
    extraction = _dsp_extraction(
        facts=["User works on SynthHeart"], prefs=["Please be direct"]
    )

    profile = await builder.build_initial(extractions=[extraction])

    assert "User works on SynthHeart" not in profile
    assert "Please be direct" in profile


@pytest.mark.asyncio
async def test_dsp_builder_empty_when_no_stable_facts() -> None:
    builder = RuleBasedDspBuilder()
    one_off = _dsp_extraction(facts=["User says they are fixing it now"])

    profile = await builder.build_initial(extractions=[one_off])

    assert "No stable facts yet." in profile
    assert "fixing it now" not in profile


@pytest.mark.asyncio
async def test_dsp_builder_update_preserves_current_when_no_new_stable_fact() -> None:
    builder = RuleBasedDspBuilder()
    current = "<user_profile>User works on SynthHeart</user_profile>"
    # New extraction carries only a one-off status line: nothing stable to add,
    # so build_update must return the current profile untouched.
    extraction = _dsp_extraction(facts=["User says they are fixing it now"])

    result = await builder.build_update(current_dsp=current, extractions=[extraction])

    assert result == current


@pytest.mark.asyncio
async def test_dsp_builder_update_incorporates_new_stable_fact() -> None:
    builder = RuleBasedDspBuilder()
    current = "<user_profile>User works on SynthHeart</user_profile>"
    twice = _dsp_extraction(facts=["User works on SynthHeart", "User is the trainer"])

    result = await builder.build_update(current_dsp=current, extractions=[twice, twice])

    assert "User works on SynthHeart" in result
    assert "User is the trainer" in result


@pytest.mark.asyncio
async def test_dsp_builder_caps_profile_words() -> None:
    builder = RuleBasedDspBuilder()
    long_facts = [" ".join(["word"] * 80)] * 2  # 2 identical long facts
    extraction = _dsp_extraction(facts=long_facts)

    profile = await builder.build_initial(extractions=[extraction, extraction])

    # The cap limits the rendered profile body well below the raw fact length.
    body = profile.replace("<user_profile>", "").replace("</user_profile>", "")
    assert len(body.split()) <= builder.MAX_PROFILE_WORDS + 1


@pytest.mark.asyncio
async def test_dsp_builder_sanitizes_stale_profile() -> None:
    """build_update must not preserve a stale/contaminated DSP forever: when no
    new stable fact arrives, conversation-shaped facts in the existing profile
    ("User says/wants…" sentences, person-addressed speech, emote filler) are
    dropped so they cannot leak into later turns (observed: an old "User wants
    to try setting a minecraft goal from here" reached an observer beat's
    outreach). Stable biographical facts and preferences survive."""
    builder = RuleBasedDspBuilder()
    contaminated = (
        "<user_profile>User works on SynthHeart; User wants to try setting a "
        "minecraft goal from here; User says they are already there; User "
        "lives in Berlin</user_profile>"
    )
    # No new stable facts in the fresh extractions — the contaminated profile
    # must still be cleaned.
    quiet = _dsp_extraction(facts=["User says they are fixing it now"])

    result = await builder.build_update(current_dsp=contaminated, extractions=[quiet])

    assert "User works on SynthHeart" in result
    assert "User lives in Berlin" in result
    assert "minecraft goal" not in result
    assert "already there" not in result


@pytest.mark.asyncio
async def test_dsp_builder_keeps_clean_profile_when_quiet() -> None:
    """A clean profile (only stable facts) is preserved untouched on a quiet
    day — sanitisation must not degrade a good profile."""
    builder = RuleBasedDspBuilder()
    clean = "<user_profile>User works on SynthHeart</user_profile>"
    quiet = _dsp_extraction(facts=["User says they are fixing it now"])

    result = await builder.build_update(current_dsp=clean, extractions=[quiet])

    assert result == clean


class _DistillingExtractor:
    """Stands in for the LLM extractor: returns a paraphrase, not the transcript."""

    def __init__(self) -> None:
        self.transcripts: list[str] = []

    async def extract_memcells(
        self, *, transcript: str, current_date: date
    ) -> list[MemCellExtractionModel]:
        del current_date
        self.transcripts.append(transcript)
        return [
            MemCellExtractionModel.model_validate(
                {
                    "episodic_trace": (
                        "Scar confirmed the deploy worked and asked how it felt."
                    ),
                    "atomic_facts": ["User|confirmed|the deploy worked"],
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
                    "timestamp": datetime(2026, 4, 18, 13, 0, tzinfo=timezone.utc),
                }
            )
        ]


class _SilentExtractor:
    """An extractor that finds nothing worth remembering."""

    async def extract_memcells(
        self, *, transcript: str, current_date: date
    ) -> list[MemCellExtractionModel]:
        del transcript, current_date
        return []


def _legacy_cell(cell_id: str, *, hours_ago: int) -> MemCell:
    return MemCell(
        id=cell_id,
        episodic_trace=(
            "Scar: okay it's finally deployed, the memcell issue should be mitigated now"
        ),
        atomic_facts=["Conversation|summary|Scar: okay it's finally deployed"],
        emotional_tag=EmotionalTag(
            state_snapshot={"joy": 0.0, "fear": 0.0, "sad": 0.0, "anger": 0.0},
            dominant_emotion="neutral",
            intensity=0.0,
            valence=0.0,
        ),
        foresight_signals=[],
        event_timestamp=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        session_id="session-9",
        embedding=[0.1] * 8,
        retrieval_count=7,
        explicit_importance=0.2,
        consolidated=True,
        scene_id="scene:1",
    )


@pytest.mark.asyncio
async def test_redistil_rewrites_a_legacy_cell_in_place() -> None:
    """A legacy row keeps its identity; only the content becomes distilled."""
    repo = InMemorySoulRepository()
    legacy = _legacy_cell("session-9:1", hours_ago=40)
    original_trace = legacy.episodic_trace
    original_timestamp = legacy.event_timestamp
    repo.memcells[legacy.id] = legacy
    extractor = _DistillingExtractor()
    compiler = SoulCompiler(
        repository=repo,
        memcell_extractor=extractor,
        dsp_extractor=FakeDspExtractor(),
        dsp_builder=RuleBasedDspBuilder(),
        summary_builder=RuleBasedSummaryBuilder(),
        embedder=NoopEmbedder(),
    )

    rewritten = await compiler.redistil_memcell(legacy, current_date=date(2026, 4, 18))

    assert rewritten is True
    stored = repo.memcells["session-9:1"]
    assert stored.episodic_trace == (
        "Scar confirmed the deploy worked and asked how it felt."
    )
    assert stored.atomic_facts == ["User|confirmed|the deploy worked"]
    # Identity, history and emotion tagging are untouched.
    assert stored.retrieval_count == 7
    assert stored.scene_id == "scene:1"
    assert stored.explicit_importance == 0.2
    assert stored.consolidated is True
    assert stored.event_timestamp == original_timestamp
    assert stored.emotional_tag.dominant_emotion == "neutral"
    # The cell's own text was what the extractor was asked to distil.
    assert extractor.transcripts == [original_trace]


@pytest.mark.asyncio
async def test_redistil_leaves_a_cell_the_extractor_cannot_improve() -> None:
    repo = InMemorySoulRepository()
    legacy = _legacy_cell("session-9:2", hours_ago=40)
    original_trace = legacy.episodic_trace
    repo.memcells[legacy.id] = legacy
    compiler = SoulCompiler(
        repository=repo,
        memcell_extractor=_SilentExtractor(),
        dsp_extractor=FakeDspExtractor(),
        dsp_builder=RuleBasedDspBuilder(),
        summary_builder=RuleBasedSummaryBuilder(),
        embedder=NoopEmbedder(),
    )

    rewritten = await compiler.redistil_memcell(legacy, current_date=date(2026, 4, 18))

    assert rewritten is False
    assert repo.memcells["session-9:2"].episodic_trace == original_trace


@pytest.mark.asyncio
async def test_redistil_pass_only_touches_cells_older_than_the_cutoff() -> None:
    repo = InMemorySoulRepository()
    old = _legacy_cell("session-9:old", hours_ago=40)
    fresh = _legacy_cell("session-9:fresh", hours_ago=1)
    repo.memcells[old.id] = old
    repo.memcells[fresh.id] = fresh
    compiler = SoulCompiler(
        repository=repo,
        memcell_extractor=_DistillingExtractor(),
        dsp_extractor=FakeDspExtractor(),
        dsp_builder=RuleBasedDspBuilder(),
        summary_builder=RuleBasedSummaryBuilder(),
        embedder=NoopEmbedder(),
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=5)

    result = await compiler.redistil_memcells(
        before=cutoff, current_date=date(2026, 4, 18)
    )

    assert result == {
        "inspected": 1,
        "rewritten": 1,
        "skipped": 0,
        "failed": 0,
        "timed_out": 0,
    }
    assert repo.memcells["session-9:old"].episodic_trace.startswith("Scar confirmed")
    assert repo.memcells["session-9:fresh"].episodic_trace.startswith("Scar: okay")
