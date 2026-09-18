"""The manual re-distil pass: distillation stamps, the pass, and the button API.

Memories written before the distilling extractor existed carry the session
transcript as their content and no ``distilled_at`` stamp. These tests pin the
three properties that make the WebUI button safe to hand to every deployment:

* a cell is stamped when a distilling extractor writes it, so "unstamped" means
  exactly "written before distillation existed";
* the pass rewrites only unstamped cells and stamps them, so a second press is a
  no-op instead of a re-paraphrase of good memories;
* the plugins starts the pass in the background and refuses a second press while
  it runs, because one press costs a model call per memory.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from core.soul.compiler import (
    NoopEmbedder,
    RuleBasedDspBuilder,
    RuleBasedMemCellCurator,
    RuleBasedSummaryBuilder,
    SoulCompiler,
)
from core.soul.models import EmotionalTag, MemCell
from core.soul.repository import InMemorySoulRepository
from core.soul.schemas import MemCellExtractionModel
from core.soul.strategies import RuleBasedDspExtractor, RuleBasedMemCellExtractor
from plugins.soul_plugin import SoulPlugin

DISTILLED_TRACE = "The user confirmed the memcell deploy worked and asked how it felt."


def _tag() -> EmotionalTag:
    return EmotionalTag(
        state_snapshot={"joy": 0.0, "fear": 0.0, "sad": 0.0, "anger": 0.0},
        dominant_emotion="neutral",
        intensity=0.0,
        valence=0.0,
    )


def _legacy_cell(
    cell_id: str,
    *,
    trace: str = "Scar: okay it's finally deployed, the memcell issue should be mitigated now",
    retrieval_count: int = 0,
    stamped: bool = False,
) -> MemCell:
    return MemCell(
        id=cell_id,
        episodic_trace=trace,
        atomic_facts=["Conversation|summary|Scar: okay it's finally deployed"],
        emotional_tag=_tag(),
        foresight_signals=[],
        event_timestamp=datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc),
        session_id="telegram_bot/999",
        retrieval_count=retrieval_count,
        distilled_at=(
            datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc) if stamped else None
        ),
    )


class _DistillingExtractor:
    """An extractor that distils, and therefore stamps what it writes."""

    distils_content = True

    def __init__(self, *, trace: str = DISTILLED_TRACE) -> None:
        self.trace = trace
        self.transcripts: list[str] = []

    async def extract_memcells(
        self, *, transcript: str, current_date: date
    ) -> list[MemCellExtractionModel]:
        del current_date
        self.transcripts.append(transcript)
        return [
            MemCellExtractionModel.model_validate(
                {
                    "episodic_trace": self.trace,
                    "atomic_facts": ["User|confirmed|the memcell deploy worked"],
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
                    "timestamp": datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc),
                }
            )
        ]


def _compiler(
    repo: InMemorySoulRepository, extractor: Any
) -> SoulCompiler:
    return SoulCompiler(
        repository=repo,
        memcell_extractor=extractor,
        dsp_extractor=RuleBasedDspExtractor(),
        dsp_builder=RuleBasedDspBuilder(),
        summary_builder=RuleBasedSummaryBuilder(),
        embedder=NoopEmbedder(),
        curator=RuleBasedMemCellCurator(),
    )


# ---------------------------------------------------------------------------
# Stamp on write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compiling_with_a_distilling_extractor_stamps_the_cells() -> None:
    repo = InMemorySoulRepository()
    compiler = _compiler(repo, _DistillingExtractor())

    created = await compiler.post_session_compile(
        current_date=date(2026, 4, 18),
        transcript="Scar: okay it's finally deployed",
        session_id="telegram_bot/999",
    )

    assert created
    stored = repo.memcells[created[0]]
    assert stored.distilled_at is not None


@pytest.mark.asyncio
async def test_compiling_with_the_deterministic_extractor_leaves_no_stamp() -> None:
    """Unstamped has to mean "predates distillation", not "wrote no facts"."""
    repo = InMemorySoulRepository()
    compiler = _compiler(repo, RuleBasedMemCellExtractor())

    created = await compiler.post_session_compile(
        current_date=date(2026, 4, 18),
        transcript="Scar: okay it's finally deployed",
        session_id="telegram_bot/999",
    )

    assert created
    for cell_id in created:
        assert repo.memcells[cell_id].distilled_at is None


# ---------------------------------------------------------------------------
# The pass itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redistil_pending_rewrites_only_unstamped_cells() -> None:
    repo = InMemorySoulRepository()
    await repo.upsert_memcell(_legacy_cell("legacy-1"))
    await repo.upsert_memcell(_legacy_cell("legacy-2", retrieval_count=5))
    await repo.upsert_memcell(
        _legacy_cell("already-distilled", trace=DISTILLED_TRACE, stamped=True)
    )
    compiler = _compiler(repo, _DistillingExtractor())

    result = await compiler.redistil_pending(current_date=date(2026, 4, 18))

    assert result == {"inspected": 2, "rewritten": 2, "skipped": 0, "failed": 0}
    assert repo.memcells["legacy-1"].episodic_trace == DISTILLED_TRACE
    assert repo.memcells["legacy-2"].episodic_trace == DISTILLED_TRACE
    # The stamped cell is left exactly as it was.
    assert repo.memcells["already-distilled"].episodic_trace == DISTILLED_TRACE
    assert repo.memcells["already-distilled"].retrieval_count == 0


@pytest.mark.asyncio
async def test_a_second_pass_finds_nothing_to_do() -> None:
    """The button stays idempotent: press it again and no memory is re-written."""
    repo = InMemorySoulRepository()
    await repo.upsert_memcell(_legacy_cell("legacy-1"))
    extractor = _DistillingExtractor()
    compiler = _compiler(repo, extractor)

    first = await compiler.redistil_pending(current_date=date(2026, 4, 18))
    second = await compiler.redistil_pending(current_date=date(2026, 4, 18))

    assert first["rewritten"] == 1
    assert second == {"inspected": 0, "rewritten": 0, "skipped": 0, "failed": 0}
    # One model call in total: the second pass never reached the extractor.
    assert len(extractor.transcripts) == 1


@pytest.mark.asyncio
async def test_the_pass_reports_progress_as_it_goes() -> None:
    repo = InMemorySoulRepository()
    for index in range(3):
        await repo.upsert_memcell(_legacy_cell(f"legacy-{index}"))
    compiler = _compiler(repo, _DistillingExtractor())
    updates: list[dict[str, int]] = []

    result = await compiler.redistil_pending(
        current_date=date(2026, 4, 18), on_progress=updates.append
    )

    assert result["inspected"] == 3
    assert updates
    assert updates[-1]["inspected"] == 3
    assert updates[-1]["rewritten"] == 3


@pytest.mark.asyncio
async def test_a_cell_the_extractor_will_not_paraphrase_is_left_unstamped() -> None:
    """A skipped memory must stay unstamped, so a later press can retry it."""
    repo = InMemorySoulRepository()
    legacy = _legacy_cell("legacy-1")
    await repo.upsert_memcell(legacy)
    compiler = _compiler(repo, _DistillingExtractor(trace=legacy.episodic_trace))

    result = await compiler.redistil_pending(current_date=date(2026, 4, 18))

    assert result == {"inspected": 1, "rewritten": 0, "skipped": 1, "failed": 0}
    assert repo.memcells["legacy-1"].distilled_at is None
    assert repo.memcells["legacy-1"].episodic_trace == legacy.episodic_trace


@pytest.mark.asyncio
async def test_redistil_memcell_stamps_the_cell_it_rewrites() -> None:
    repo = InMemorySoulRepository()
    legacy = _legacy_cell("legacy-1")
    await repo.upsert_memcell(legacy)
    compiler = _compiler(repo, _DistillingExtractor())

    rewritten = await compiler.redistil_memcell(legacy, current_date=date(2026, 4, 18))

    assert rewritten is True
    assert repo.memcells["legacy-1"].distilled_at is not None


# ---------------------------------------------------------------------------
# The store query the pass is built on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_store_lists_unstamped_cells_most_recalled_first() -> None:
    repo = InMemorySoulRepository()
    await repo.upsert_memcell(_legacy_cell("quiet", retrieval_count=1))
    await repo.upsert_memcell(_legacy_cell("popular", retrieval_count=9))
    await repo.upsert_memcell(_legacy_cell("done", stamped=True))
    await repo.upsert_memcell(_legacy_cell("blank", trace="   "))

    pending = await repo.list_memcells_needing_distillation(limit=10)

    assert [cell.id for cell in pending] == ["popular", "quiet"]
    assert await repo.count_memcells_needing_distillation() == 2


# ---------------------------------------------------------------------------
# The plugin side of the button
# ---------------------------------------------------------------------------


class _FakeCompiler:
    """Stands in for SoulCompiler so the plugin's own behaviour is what is tested."""

    def __init__(self, *, result: dict[str, int], blocker: asyncio.Event | None = None) -> None:
        self.result = result
        self.blocker = blocker
        self.calls = 0
        self.limits: list[int] = []
        self.memcell_extractor = SimpleNamespace(distils_content=True)

    async def redistil_pending(
        self,
        *,
        current_date: date,
        limit: int,
        on_progress: Any | None = None,
    ) -> dict[str, int]:
        del current_date
        self.calls += 1
        self.limits.append(limit)
        if on_progress is not None:
            on_progress(
                {"total": 3, "inspected": 1, "rewritten": 1, "skipped": 0, "failed": 0}
            )
        if self.blocker is not None:
            await self.blocker.wait()
        if on_progress is not None:
            on_progress(
                {"total": 3, "inspected": 3, "rewritten": 2, "skipped": 1, "failed": 0}
            )
        return dict(self.result)


@pytest.fixture(autouse=True)
def _memory_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNTH_PRIMARY_DB", raising=False)
    monkeypatch.delenv("SOUL_POSTGRES_DSN", raising=False)
    monkeypatch.setenv("SYNTH_DB_TYPE", "mariadb")


@pytest.mark.asyncio
async def test_start_redistil_runs_in_the_background_and_keeps_counters() -> None:
    plugin = SoulPlugin()
    fake = _FakeCompiler(result={"inspected": 3, "rewritten": 2, "skipped": 1, "failed": 0})
    plugin._compiler = fake

    started = await plugin.start_redistil()

    assert started["started"] is True
    task = plugin._redistil_task
    assert task is not None
    await task

    status = await plugin.redistil_status()
    assert status["running"] is False
    assert status["inspected"] == 3
    assert status["rewritten"] == 2
    assert status["skipped"] == 1
    assert status["failed"] == 0
    assert status["finished_at"] is not None
    assert status["error"] is None
    assert fake.calls == 1
    assert fake.limits  # the configured batch size reached the compiler


@pytest.mark.asyncio
async def test_a_second_press_while_the_pass_runs_is_refused() -> None:
    plugin = SoulPlugin()
    blocker = asyncio.Event()
    fake = _FakeCompiler(
        result={"inspected": 3, "rewritten": 2, "skipped": 1, "failed": 0},
        blocker=blocker,
    )
    plugin._compiler = fake

    await plugin.start_redistil()
    second = await plugin.start_redistil()

    assert second["started"] is False
    assert second["reason"] == "already_running"
    assert (await plugin.redistil_status())["running"] is True

    blocker.set()
    task = plugin._redistil_task
    assert task is not None
    await task
    assert fake.calls == 1
    assert (await plugin.redistil_status())["running"] is False


@pytest.mark.asyncio
async def test_redistil_status_counts_what_is_left_to_do() -> None:
    plugin = SoulPlugin()
    repo = plugin.get_repository()
    await repo.upsert_memcell(_legacy_cell("legacy-1"))
    await repo.upsert_memcell(_legacy_cell("legacy-2"))
    await repo.upsert_memcell(_legacy_cell("done", stamped=True))

    status = await plugin.redistil_status()

    assert status["pending"] == 2
    assert status["running"] is False
    assert status["distilling_extractor"] is True


@pytest.mark.asyncio
async def test_the_limit_is_bounded_by_the_hard_cap() -> None:
    plugin = SoulPlugin()
    fake = _FakeCompiler(result={"inspected": 0, "rewritten": 0, "skipped": 0, "failed": 0})
    plugin._compiler = fake

    await plugin.start_redistil(limit=10**9)
    task = plugin._redistil_task
    assert task is not None
    await task

    assert fake.limits == [20000]


@pytest.mark.asyncio
async def test_a_failing_pass_is_reported_instead_of_lost() -> None:
    class _Boom:
        memcell_extractor = SimpleNamespace(distils_content=True)

        async def redistil_pending(self, **_: Any) -> dict[str, int]:
            raise RuntimeError("engine unavailable")

    plugin = SoulPlugin()
    plugin._compiler = _Boom()

    await plugin.start_redistil()
    task = plugin._redistil_task
    assert task is not None
    await task

    status = await plugin.redistil_status()
    assert status["running"] is False
    assert status["error"] == "engine unavailable"
    assert status["finished_at"] is not None
