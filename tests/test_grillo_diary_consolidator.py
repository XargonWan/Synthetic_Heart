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


def test_a_day_under_the_chunk_limit_is_sent_whole():
    plugin = _make_plugin()
    plugin.chunk_chars = 1000
    day_text = "first fragment" + "\n\n---\n\n" + "second fragment"

    text, cut = plugin._split_day_text(day_text)

    assert text == day_text
    assert cut == len(day_text)
    assert plugin._count_parts(day_text) == 1


def test_a_long_day_is_split_on_a_fragment_boundary():
    plugin = _make_plugin()
    plugin.chunk_chars = 250
    fragments = ["a" * 100, "b" * 100, "c" * 100, "d" * 100]
    day_text = "\n\n---\n\n".join(fragments)

    text, cut = plugin._split_day_text(day_text)

    # Two fragments fit (100 + separator + 100 = 210), the third would not, and
    # the split lands exactly on the separator so no fragment is cut in half.
    assert text == "\n\n---\n\n".join(fragments[:2])
    assert cut == len(fragments[0]) + len("\n\n---\n\n") + len(fragments[1])
    assert day_text[cut:] == "\n\n---\n\n" + "\n\n---\n\n".join(fragments[2:])
    assert plugin._count_parts(day_text) == 2


def test_a_fragment_longer_than_the_limit_is_still_sent_alone():
    plugin = _make_plugin()
    plugin.chunk_chars = 50
    day_text = "a" * 500

    text, cut = plugin._split_day_text(day_text)

    assert text == day_text[:50]
    assert cut == 50


def test_chunking_can_be_disabled():
    plugin = _make_plugin()
    plugin.chunk_chars = 0
    day_text = "\n\n---\n\n".join(["a" * 100, "b" * 100])

    text, cut = plugin._split_day_text(day_text)

    assert text == day_text
    assert cut == len(day_text)


def test_today_is_offered_only_once_it_has_grown_past_the_chunk_limit():
    plugin = _make_plugin()
    plugin.chunk_chars = 100
    today = date.today()

    assert plugin._is_eligible_day((today, 1, "short", 2)) is False
    assert plugin._is_eligible_day((today, 1, "x" * 200, 2)) is True
    # A completed day is always eligible, however small.
    assert plugin._is_eligible_day((date(2026, 9, 1), 1, "short", 2)) is True


def test_a_merge_that_shrank_the_day_restarts_the_attempt_count():
    plugin = _make_plugin(max_attempts=2)
    day = date(2026, 9, 1)

    assert plugin._record_attempts_and_filter([(day, 1, "x" * 400, 2)])
    assert plugin._record_attempts_and_filter([(day, 1, "x" * 400, 2)])
    # Two failed attempts would normally exhaust the day on the third offer.
    # A day that actually got shorter is making progress, so it continues.
    assert plugin._record_attempts_and_filter([(day, 1, "x" * 200, 2)]) == [
        (day, 1, "x" * 200, 2)
    ]
    assert plugin._consolidation_attempt_counts[day] == 1
    assert day not in plugin._consolidation_exhausted_days


async def test_a_partial_day_never_shows_its_later_fragments_to_the_model(
    monkeypatch,
):
    plugin = _make_plugin()
    plugin.chunk_chars = 250
    day = date(2026, 9, 1)
    fragments = [
        "MORNING-ONE " + "a" * 90,
        "MORNING-TWO " + "b" * 90,
        "AFTERNOON " + "c" * 90,
        "EVENING " + "d" * 90,
    ]
    day_text = "\n\n---\n\n".join(fragments)

    async def fake_find_unmerged_days(max_days):
        return [(day, 51, day_text, 1)]

    monkeypatch.setattr(plugin, "_find_unmerged_days", fake_find_unmerged_days)

    prompt = await plugin.build_prompt()

    assert "MORNING-ONE" in prompt and "MORNING-TWO" in prompt
    # The fragments after the part are preserved for a later run and must not
    # reach the model, which would otherwise rewrite the whole day from part 1.
    assert "AFTERNOON" not in prompt
    assert "EVENING" not in prompt
    assert "PART 1" in prompt
    # The merge still targets the day's entry row, and the write is told where
    # the sent part ended so the rest of the row survives.
    assert '"id": 51' in prompt
    expected_cut = len(fragments[0]) + len("\n\n---\n\n") + len(fragments[1])
    assert plugin.pending_beat_context == {"diary_merge_preserve_from": expected_cut}
    assert expected_cut < len(day_text)


async def test_a_whole_day_hands_no_preserve_offset_to_the_write(monkeypatch):
    plugin = _make_plugin()
    plugin.chunk_chars = 10000
    day = date(2026, 9, 1)

    async def fake_find_unmerged_days(max_days):
        return [(day, 51, "one fragment", 1)]

    monkeypatch.setattr(plugin, "_find_unmerged_days", fake_find_unmerged_days)

    await plugin.build_prompt()

    assert plugin.pending_beat_context is None
