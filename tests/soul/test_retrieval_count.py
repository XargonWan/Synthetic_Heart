"""retrieval_count: one recall per cell per interval, and the clean-up bound."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.soul.models import EmotionalTag, MemCell
from core.soul.repository import InMemorySoulRepository


def _cell(cell_id: str) -> MemCell:
    return MemCell(
        id=cell_id,
        session_id="telegram_bot_1",
        episodic_trace="Alice drinks jasmine tea.",
        atomic_facts=[],
        emotional_tag=EmotionalTag(
            state_snapshot={}, dominant_emotion="calm", intensity=0.1, valence=0.0
        ),
        foresight_signals=[],
        event_timestamp=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_touch_counts_once_per_interval() -> None:
    repo = InMemorySoulRepository()
    await repo.upsert_memcell(_cell("c1"))

    assert await repo.touch_memcell_retrieval("c1", 86400.0) is True
    assert await repo.touch_memcell_retrieval("c1", 86400.0) is False
    assert (await repo.get_memcell("c1")).retrieval_count == 1  # type: ignore[union-attr]

    # An interval that has already passed counts again.
    repo.last_retrieved_at["c1"] -= timedelta(days=2)
    assert await repo.touch_memcell_retrieval("c1", 86400.0) is True
    assert (await repo.get_memcell("c1")).retrieval_count == 2  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_touch_of_an_unknown_cell_counts_nothing() -> None:
    assert await InMemorySoulRepository().touch_memcell_retrieval("nope", 1.0) is False


def _load_script() -> object:
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "recompute_retrieval_counts.py"
    )
    spec = importlib.util.spec_from_file_location("recompute_retrieval_counts", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_clean_up_bounds_a_count_by_the_age_of_the_cell() -> None:
    script = _load_script()
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

    # A July cell with 2,536 recalls: bounded by its ~127 days.
    july = datetime(2026, 7, 16, 3, 23, tzinfo=timezone.utc)
    assert script.clamped_count(2536, july, now) == 128  # type: ignore[attr-defined]
    # A count already within its age is left alone.
    assert script.clamped_count(20, july, now) == 20  # type: ignore[attr-defined]
    # A cell created minutes ago can have been recalled today: at least 1.
    assert script.max_plausible_count(now - timedelta(minutes=5), now) == 1  # type: ignore[attr-defined]
    # A naive timestamp is read as UTC.
    assert script.max_plausible_count(datetime(2026, 11, 19, 12, 0), now) == 1  # type: ignore[attr-defined]
