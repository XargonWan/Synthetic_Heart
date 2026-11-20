from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.soul.compiler import (
    NoopEmbedder,
    RuleBasedDspBuilder,
    RuleBasedSummaryBuilder,
    SoulCompiler,
)
from core.soul.dedupe import (
    content_words,
    find_near_duplicate,
    overlap,
    union_participants,
)
from core.soul.models import EmotionalTag, MemCell
from core.soul.repository import InMemorySoulRepository
from core.soul.schemas import DspExtractionModel, MemCellExtractionModel
from plugins.soul_plugin import SoulPlugin, _SessionState

_TAG = {
    "state_snapshot": {"joy": 0.0, "fear": 0.0, "sad": 0.0, "anger": 0.0},
    "dominant_emotion": "neutral",
    "intensity": 0.0,
    "valence": 0.0,
}


class _Extractor:
    def __init__(self, trace: str, stamp: datetime) -> None:
        self._trace = trace
        self._stamp = stamp

    async def extract_memcells(
        self, *, transcript: str, current_date: date
    ) -> list[MemCellExtractionModel]:
        return [
            MemCellExtractionModel.model_validate(
                {
                    "episodic_trace": self._trace,
                    "atomic_facts": [],
                    "emotional_tag": _TAG,
                    "foresight_signals": [],
                    "timestamp": self._stamp,
                }
            )
        ]


class _Dsp:
    async def extract_dsp(
        self, *, transcript: str, current_date: date
    ) -> DspExtractionModel:
        return DspExtractionModel(user_facts=[], user_preferences=[], ai_self_facts=[])


def _compiler(
    repo: InMemorySoulRepository, trace: str, stamp: datetime
) -> SoulCompiler:
    return SoulCompiler(
        repository=repo,
        memcell_extractor=_Extractor(trace, stamp),
        dsp_extractor=_Dsp(),
        dsp_builder=RuleBasedDspBuilder(),
        summary_builder=RuleBasedSummaryBuilder(),
        embedder=NoopEmbedder(),
    )


_T1 = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
_T2 = datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc)


def test_word_overlap_does_not_depend_on_language() -> None:
    same_it = content_words("Marco ha deciso di vendere la vecchia barca a vela")
    same_it2 = content_words("Marco ha deciso di vendere la vecchia barca a vela ieri")
    other = content_words("Il giardino ha bisogno di più acqua durante l'estate")
    assert overlap(same_it, same_it2) >= 0.8
    assert overlap(same_it, other) < 0.3
    assert overlap(frozenset(), same_it) == 0.0


def test_a_private_and_a_shared_cell_are_never_repeats_of_each_other() -> None:
    cell = MemCell(
        id="a",
        episodic_trace="Marco decided to sell the old sailing boat this spring",
        atomic_facts=[],
        emotional_tag=EmotionalTag(
            state_snapshot={}, dominant_emotion="neutral", intensity=0.0, valence=0.0
        ),
        foresight_signals=[],
        event_timestamp=_T1,
        session_id="s",
        visibility="private",
    )
    text = "Marco decided to sell the old sailing boat this spring"
    assert find_near_duplicate(text, [cell], visibility="private") is cell
    assert find_near_duplicate(text, [cell], visibility="shared") is None


def test_participants_are_united_once_each() -> None:
    merged = union_participants(
        [{"id": "1", "name": "Ann"}],
        [{"id": "1", "name": "Ann"}, {"id": "2", "name": "Bo"}],
    )
    assert [p["id"] for p in merged] == ["1", "2"]


@pytest.mark.asyncio
async def test_a_repeated_cell_is_absorbed_not_written_again() -> None:
    repo = InMemorySoulRepository()
    trace = "Marco decided to sell the old sailing boat this spring"

    await _compiler(repo, trace, _T1).post_session_compile(
        current_date=date(2026, 5, 1),
        transcript="x",
        session_id="chat_1",
        participants=[{"id": "1", "name": "Ann"}],
    )
    await _compiler(repo, trace + " and again", _T2).post_session_compile(
        current_date=date(2026, 5, 1),
        transcript="y",
        session_id="chat_1",
        participants=[{"id": "2", "name": "Bo"}],
    )

    assert len(repo.memcells) == 1, "the second cell repeats the first and is absorbed"
    cell = next(iter(repo.memcells.values()))
    assert {p["id"] for p in cell.participants} == {"1", "2"}
    # The wording with more in it wins.
    assert "again" in cell.episodic_trace


@pytest.mark.asyncio
async def test_a_shared_notion_repeated_in_another_chat_is_absorbed() -> None:
    """A ``shared`` cell may be recalled anywhere, so its repeat in another chat folds in."""
    repo = InMemorySoulRepository()
    trace = "Marco decided to sell the old sailing boat this spring"
    await _compiler(repo, trace, _T1).post_session_compile(
        current_date=date(2026, 5, 1),
        transcript="x",
        session_id="chat_1",
        participants=[{"id": "1", "name": "Ann"}],
    )
    await _compiler(repo, trace, _T2).post_session_compile(
        current_date=date(2026, 5, 1),
        transcript="y",
        session_id="chat_2",
        participants=[{"id": "2", "name": "Bo"}],
    )
    assert len(repo.memcells) == 1
    cell = next(iter(repo.memcells.values()))
    assert {p["id"] for p in cell.participants} == {"1", "2"}


@pytest.mark.asyncio
async def test_a_private_notion_is_never_folded_into_another_chat() -> None:
    """A private cell's audience is its own chat; another chat's copy stays separate."""
    repo = InMemorySoulRepository()
    trace = "Marco decided to sell the old sailing boat this spring"
    for session, stamp in (("chat_1", _T1), ("chat_2", _T2)):
        await _compiler(repo, trace, stamp).post_session_compile(
            current_date=date(2026, 5, 1),
            transcript=session,
            session_id=session,
            visibility="private",
        )
    assert len(repo.memcells) == 2


@pytest.mark.asyncio
async def test_a_system_session_never_takes_part_in_cross_chat_merging() -> None:
    repo = InMemorySoulRepository()
    trace = "Marco decided to sell the old sailing boat this spring"
    await _compiler(repo, trace, _T1).post_session_compile(
        current_date=date(2026, 5, 1), transcript="x", session_id="chat_1"
    )
    await _compiler(repo, trace, _T2).post_session_compile(
        current_date=date(2026, 5, 1),
        transcript="y",
        session_id="diary_merge:2026-05-01",
    )
    assert len(repo.memcells) == 2


@pytest.mark.asyncio
async def test_a_cell_with_almost_no_content_is_not_stored() -> None:
    repo = InMemorySoulRepository()
    await _compiler(repo, "Greeting. Hi!", _T1).post_session_compile(
        current_date=date(2026, 5, 1), transcript="x", session_id="chat_1"
    )
    assert repo.memcells == {}


@pytest.mark.asyncio
async def test_a_fuller_account_that_adds_real_content_stays_its_own_cell() -> None:
    """Only near-identical text is absorbed; a variant with real extra detail is kept."""
    repo = InMemorySoulRepository()
    base = "Marco decided to sell the old sailing boat this spring"
    await _compiler(repo, base, _T1).post_session_compile(
        current_date=date(2026, 5, 1), transcript="x", session_id="chat_1"
    )
    await _compiler(
        repo, base + " and buy a smaller one before the summer holidays", _T2
    ).post_session_compile(
        current_date=date(2026, 5, 1), transcript="y", session_id="chat_1"
    )
    assert len(repo.memcells) == 2


@pytest.mark.asyncio
async def test_participants_are_recorded_per_window_and_bots_are_ignored() -> None:
    plugin = SoulPlugin()
    iface = "telegram_bot/12"
    session = _SessionState()
    plugin._sessions[iface] = session
    human = SimpleNamespace(
        text="hello",
        caption=None,
        interface_path=iface,
        from_user=SimpleNamespace(id=7, first_name="Ann", username="ann", is_bot=False),
    )
    robot = SimpleNamespace(
        text="beep",
        caption=None,
        interface_path=iface,
        from_user=SimpleNamespace(id=9, first_name="Bot", username="bot", is_bot=True),
    )
    SoulPlugin._note_participant(session, human)
    SoulPlugin._note_participant(session, robot)
    assert list(session.window_participants) == ["7"]

    compiler = SimpleNamespace(
        post_session_compile=AsyncMock(return_value=[]),
        async_consolidate=AsyncMock(return_value=[]),
    )
    plugin._compiler = compiler
    plugin._buffers[iface] = ["Ann: hello"]
    await plugin._compile_interface(iface)
    sent = compiler.post_session_compile.await_args.kwargs["participants"]
    assert [p["id"] for p in sent] == ["7"]
    assert session.window_participants == {}, "the next window starts empty"


@pytest.mark.asyncio
async def test_system_sessions_are_not_compiled_at_all() -> None:
    plugin = SoulPlugin()
    compiler = SimpleNamespace(
        post_session_compile=AsyncMock(return_value=["c"]),
        async_consolidate=AsyncMock(return_value=[]),
    )
    plugin._compiler = compiler
    plugin._buffers["diary_merge/-1"] = ["[diary consolidation] some transcript"]

    assert await plugin._compile_interface("diary_merge/-1") == 0
    compiler.post_session_compile.assert_not_awaited()
    assert plugin._buffers["diary_merge/-1"] == []


@pytest.mark.asyncio
async def test_archived_cells_expire_by_reason_and_age() -> None:
    repo = InMemorySoulRepository()
    for cell_id in ("evicted_old", "copy_old", "copy_new"):
        repo.memcells[cell_id] = MemCell(
            id=cell_id,
            episodic_trace="text " + cell_id,
            atomic_facts=[],
            emotional_tag=EmotionalTag(
                state_snapshot={},
                dominant_emotion="neutral",
                intensity=0.0,
                valence=0.0,
            ),
            foresight_signals=[],
            event_timestamp=_T1,
            session_id="s",
        )
    await repo.delete_memcells(["evicted_old"], reason="evicted")
    await repo.delete_memcells(
        ["copy_old", "copy_new"], reason="duplicate", merged_into="keeper"
    )
    now = datetime.now(timezone.utc)
    repo.evicted_meta["evicted_old"]["evicted_at"] = now - timedelta(days=400)
    repo.evicted_meta["copy_old"]["evicted_at"] = now - timedelta(days=40)

    plugin = SoulPlugin()
    plugin._repo = repo
    removed = await plugin._purge_archive_now(now)

    # Repeats expire after 30 days, everything else after 365.
    assert removed == {"copies": 1, "evicted": 1}
    assert set(repo.evicted_memcells) == {"copy_new"}
    assert repo.evicted_meta["copy_new"]["merged_into"] == "keeper"


def test_the_clean_up_plan_keeps_the_fullest_cell_and_respects_audience() -> None:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "dedupe_soul_cells", Path("scripts/dedupe_soul_cells.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def cell(cell_id: str, text: str, session: str = "s", vis: str = "shared"):
        return {
            "id": cell_id,
            "session_id": session,
            "visibility": vis,
            "words": content_words(text),
            "event_timestamp": datetime(2026, 5, 1, tzinfo=timezone.utc)
            + timedelta(minutes=len(cell_id)),
            "participants": [],
        }

    base = "Marco decided to sell the old sailing boat this spring"
    plan = module.plan_repeats(
        [
            cell("short", base),
            cell("full", base + " and again"),  # the fuller wording survives
            cell("other_chat", base, session="other"),
            cell("secret", base, vis="private"),
        ]
    )
    assert plan == {"full": ["short"]}


def test_every_archive_column_has_an_alter_for_tables_that_already_exist() -> None:
    """``CREATE TABLE IF NOT EXISTS`` does not touch an existing table.

    A column added to the archive's definition without an ``ALTER`` makes every
    archiving fail on a database that already has the table (as the first clean-up of
    a live copy did), and the curator then cannot evict anything.
    """
    import re
    from pathlib import Path

    source = Path("core/soul/repository.py").read_text()
    create = re.search(
        r"CREATE TABLE IF NOT EXISTS mem_cells_archive \((.*?)\n\s*\)\n", source, re.S
    )
    assert create, "the archive definition moved"
    columns = [
        line.split()[0]
        for line in create.group(1).splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    # ``id`` and the columns of the very first version are in the CREATE itself.
    original = {
        "id",
        "session_id",
        "episodic_trace",
        "atomic_facts",
        "emotional_tag",
        "foresight_signals",
        "event_timestamp",
        "retrieval_count",
        "explicit_importance",
        "consolidated",
        "scene_id",
        "distilled_at",
        "created_at",
        "updated_at",
        "evicted_at",
    }
    for column in columns:
        if column in original:
            continue
        assert (
            f"ALTER TABLE mem_cells_archive ADD COLUMN IF NOT EXISTS {column} "
            in source
        ), f"mem_cells_archive.{column} has no ALTER"


@pytest.mark.asyncio
async def test_the_curator_only_counts_and_evicts_conversation_cells() -> None:
    """Diary blocks and legacy memories are not a chat's leftovers; the cap skips them."""
    repo = InMemorySoulRepository()
    for n in range(4):
        repo.memcells[f"journal:d:{n}"] = MemCell(
            id=f"journal:d:{n}",
            episodic_trace=f"diary block number {n} about the garden",
            atomic_facts=[],
            emotional_tag=EmotionalTag(
                state_snapshot={"joy": 0.0, "fear": 0.0, "sad": 0.0, "anger": 0.0},
                dominant_emotion="neutral",
                intensity=0.0,
                valence=0.0,
            ),
            foresight_signals=[],
            event_timestamp=_T1,
            session_id="journal",
            source_kind="journal",
            source_key=f"d:{n}",
        )
    summaries = await repo.list_memcell_summaries(today=date(2026, 5, 2))
    assert summaries == []
