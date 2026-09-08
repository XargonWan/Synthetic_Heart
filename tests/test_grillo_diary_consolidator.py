"""Tests for the diary-consolidation beat's attempt cap.

Without a cap, _find_unmerged_days re-selects the same stuck diary day on
every beat cycle forever whenever its update_diary_entry action never
actually executes (e.g. an engine that keeps emitting an unrelated/invalid
action instead) - see GrilloDiaryConsolidatorPlugin._record_attempts_and_filter.
"""

from datetime import date

from plugins.grillo.grillo_diary_consolidator.grillo_diary_consolidator import (
    GrilloDiaryConsolidatorPlugin,
)


def _make_plugin(max_attempts: int = 3) -> GrilloDiaryConsolidatorPlugin:
    plugin = GrilloDiaryConsolidatorPlugin()
    plugin.max_consolidation_attempts = max_attempts
    return plugin


def test_record_attempts_and_filter_keeps_day_under_cap():
    plugin = _make_plugin(max_attempts=3)
    day = date(2026, 9, 1)
    entry = (day, 1, "fragment", 2)

    kept = plugin._record_attempts_and_filter([entry])

    assert kept == [entry]
    assert plugin._consolidation_attempt_counts[day] == 1
    assert day not in plugin._consolidation_exhausted_days


def test_record_attempts_and_filter_gives_up_after_max_attempts():
    plugin = _make_plugin(max_attempts=2)
    day = date(2026, 9, 1)
    entry = (day, 1, "fragment", 2)

    # Two attempts stay under/at the cap.
    assert plugin._record_attempts_and_filter([entry]) == [entry]
    assert plugin._record_attempts_and_filter([entry]) == [entry]
    # The third attempt exceeds max_consolidation_attempts=2 and is dropped.
    kept = plugin._record_attempts_and_filter([entry])

    assert kept == []
    assert day in plugin._consolidation_exhausted_days


async def test_build_prompt_skips_exhausted_day_and_surfaces_next_candidate(
    monkeypatch,
):
    plugin = _make_plugin(max_attempts=1)
    stuck_day = date(2026, 9, 1)
    next_day = date(2026, 8, 31)
    plugin._consolidation_exhausted_days.add(stuck_day)

    calls = []

    async def fake_find_unmerged_days(max_days):
        calls.append(max_days)
        # Widened pool includes the already-exhausted day plus the next one.
        return [
            (stuck_day, 1, "fragment", 2),
            (next_day, 2, "fragment", 2),
        ][:max_days]

    monkeypatch.setattr(plugin, "_find_unmerged_days", fake_find_unmerged_days)

    prompt = await plugin.build_prompt()

    # Pool size grew by len(exhausted_days)=1 beyond MAX_DAYS_PER_RUN=1.
    assert calls == [plugin.MAX_DAYS_PER_RUN + 1]
    assert prompt is not None
    assert str(next_day) in prompt
    assert str(stuck_day) not in prompt


async def test_build_prompt_returns_none_when_all_candidates_exhausted(
    monkeypatch,
):
    plugin = _make_plugin(max_attempts=1)
    stuck_day = date(2026, 9, 1)
    plugin._consolidation_exhausted_days.add(stuck_day)

    async def fake_find_unmerged_days(max_days):
        return [(stuck_day, 1, "fragment", 2)][:max_days]

    monkeypatch.setattr(plugin, "_find_unmerged_days", fake_find_unmerged_days)

    prompt = await plugin.build_prompt()

    assert prompt is None
