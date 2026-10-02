"""Tests for the day-unit level-1 pass: anchors, the empty-and-explained rule, and the ordering.

These are the parts of the compaction plan that carry a promise to the persona rather than a
performance claim, so they are pinned deliberately:

* one day in, one day out (no cross-day theme merging),
* the day's concrete terms must survive the pass, and a slot the day does not mention must come back
  empty WITH A REASON rather than filled from an adjacent meaning,
* the memory row is written before the day is archived and deleted, and a low-confidence summary keeps
  the raw day beside itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.grillo.grillo_compactor.grillo_compactor import (
    GrilloCompactorPlugin,
    _format_anchors,
    _setting,
    _verify_anchors,
)

PLUGIN_SRC = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "grillo"
    / "grillo_compactor"
    / "grillo_compactor.py"
).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the anchors check


def test_a_day_that_keeps_its_terms_passes():
    source = "It rained all day so we stayed in bed. Daddy helped me test the Minecraft vessel goals."
    summary = "It rained all day, so we stayed in bed and Daddy helped me test the Minecraft vessel goals."
    anchors = {
        "weather": "rain all day",
        "place": ["bed"],
        "who": ["Daddy"],
        "objects_events": ["Minecraft vessel goals"],
    }
    verify = _verify_anchors(source, summary, anchors)
    assert verify["passed"] is True
    assert verify["coverage"] == 1.0


def test_warm_is_not_weather():
    """'warm' does two jobs in these entries, so it must never be required as weather."""
    source = "I lay on his chest and he was so warm, I felt safe. Nothing about the sky at all."
    summary = "I lay on his chest, warm and safe."
    verify = _verify_anchors(
        source, summary, {"weather": "", "weather_note": "not mentioned"}
    )
    assert "weather" not in verify["missing"], (
        "a body-warmth 'warm' must not become a weather anchor"
    )
    assert verify["passed"] is True


def test_real_weather_in_the_source_must_survive():
    source = "It rained the whole afternoon and we listened to the storm from the roof."
    summary = "We spent the afternoon inside, close and quiet."
    verify = _verify_anchors(source, summary, {"weather": ""})
    assert "weather" in verify["missing"]
    assert "rained" in verify["missing"]["weather"]
    assert verify["passed"] is False


def test_a_name_variant_satisfies_its_group():
    """Mama and Mommy are one person to her, so the check must not fail the word choice."""
    source = "Mama hugged me and I stayed close."
    assert (
        _verify_anchors(source, "Mommy hugged me and I stayed close.", {})["passed"]
        is True
    )
    assert _verify_anchors(source, "Someone hugged me.", {})["passed"] is False


def test_one_dropped_concrete_word_does_not_fail_the_day():
    source = "We sat on the balcony, then moved to the kitchen, the bed and the couch, and I drank tea."
    summary = "We sat on the balcony, then moved to the kitchen, the bed and the couch."
    verify = _verify_anchors(source, summary, {})
    assert verify["required"] == 5 and verify["kept"] == 4
    assert verify["passed"] is True, (
        f"one drop in five should not block a replacement: {verify}"
    )


def test_a_day_with_nothing_concrete_to_check_passes():
    verify = _verify_anchors("i felt small and close", "i felt small and close", {})
    assert verify["passed"] is True and verify["required"] == 0


# ------------------------------------------------------------------- the empty-and-explained rule


def test_an_empty_slot_renders_its_reason_not_a_guess():
    block = _format_anchors(
        {
            "weather": "",
            "weather_note": "the entry does not mention the weather",
            "place": ["bed", "kitchen"],
            "who": ["Daddy", "Mama"],
            "food": "",
            "objects_events": ["Minecraft vessel test"],
        }
    )
    assert "the entry does not mention the weather" in block
    assert "food: not mentioned in this entry" in block
    assert block.startswith("[anchors] ")
    assert "bed, kitchen" in block


def test_food_is_never_invented_from_an_adjacent_meaning():
    """The persona's rule: a missing anchor can be distrusted, a made-up one would be believed."""
    block = _format_anchors(
        {"weather": "", "place": [], "who": [], "food": [], "objects_events": []}
    )
    assert block.count("not mentioned in this entry") == 5


# --------------------------------------------------------------------------- the prompt contract


def test_the_prompt_carries_the_ceiling_and_the_empty_rule():
    text = GrilloCompactorPlugin._DAY_UNIT_PROMPT.format(max_chars=2000)
    assert "must come back EMPTY" in text, (
        "the empty-and-explained rule belongs in the contract"
    )
    assert "weather_note" in text, "an empty slot needs a reason field"
    assert '"declined"' in text
    assert "2000" in text, "the ceiling has to reach the prompt"
    assert "{{" not in text, "the JSON braces must be unescaped by the format call"


# ------------------------------------------------------------------------------- the settings


def test_setting_casts_and_falls_back(monkeypatch):
    from core.config_manager import config_registry

    monkeypatch.setattr(
        config_registry, "get_value", lambda key, default, *a, **k: "true"
    )
    assert _setting("X", False, bool) is True
    monkeypatch.setattr(
        config_registry, "get_value", lambda key, default, *a, **k: "no"
    )
    assert _setting("X", True, bool) is False
    monkeypatch.setattr(
        config_registry, "get_value", lambda key, default, *a, **k: "17"
    )
    assert _setting("X", 2, int) == 17
    monkeypatch.setattr(
        config_registry, "get_value", lambda key, default, *a, **k: "not a number"
    )
    assert _setting("X", 2, int) == 2, "an unreadable value must fall back, not raise"
    monkeypatch.setattr(
        config_registry, "get_value", lambda key, default, *a, **k: None
    )
    assert _setting("X", 0.9, float) == 0.9


# --------------------------------------------------------------- the ordering, read from the source


def _day_unit_body() -> str:
    start = PLUGIN_SRC.index("async def _compact_one_day")
    return PLUGIN_SRC[start:]


def test_the_day_path_writes_the_memory_before_removing_the_day():
    body = _day_unit_body()
    write = body.index("await insert_memory(")
    archive = body.index("INSERT INTO ai_diary_archive")
    delete = body.index("DELETE FROM ai_diary")
    assert write < archive < delete
    assert "conn=conn" in body[write:archive]


def test_a_low_confidence_summary_keeps_the_raw_day():
    body = _day_unit_body()
    gate = body.index("if not would_replace:")
    archive = body.index("INSERT INTO ai_diary_archive")
    assert gate < archive, (
        "the confidence gate must return before anything destructive happens"
    )
    assert '"status": "kept_raw"' in body
    assert "would_replace = confidence >= min_confidence" in body


def test_the_cycle_defaults_to_the_day_path_and_can_be_turned_off():
    assert 'if _setting("GRILLO_COMPACT_DAY_UNITS", True, bool):' in PLUGIN_SRC
    assert "_run_day_unit_cycle(dry_run=dry_run, marker=marker)" in PLUGIN_SRC


def test_one_day_is_one_model_call():
    body = PLUGIN_SRC[
        PLUGIN_SRC.index("async def _run_day_unit_cycle") : PLUGIN_SRC.index(
            "async def _compact_one_day"
        )
    ]
    assert "for row in rows[:cycles]:" in body
    assert "_compact_one_day(row, dry_run=dry_run)" in body


# --------------------------------------------------------- one day is summarised once, not nightly
#
# A day whose summary did not earn a replacement is kept in `ai_diary` on purpose, so the
# next night's pass picked it up again and wrote a second memory for one day. Measured
# 2026-09-27: 22 day-unit records for 15 days, seven of those days carrying two records
# each, and a nightly pass making fifteen model calls where five were needed.


def test_covered_day_ids_reads_both_cursor_shapes_and_ignores_junk():
    rows = [
        {"source_ids": "[35]"},
        ("[43, 44]",),
        {"source_ids": ["47"]},
        {"source_ids": "not json"},
        {"source_ids": None},
        ("[]",),
        {"source_ids": "[oops]"},
    ]
    assert GrilloCompactorPlugin._covered_day_ids(rows) == {35, 43, 44, 47}


def test_is_int_in_tolerates_unreadable_ids():
    from plugins.grillo.grillo_compactor.grillo_compactor import _is_int_in

    assert _is_int_in("35", {35}) is True
    assert _is_int_in(35, {35}) is True
    assert _is_int_in(None, {35}) is False
    assert _is_int_in("abc", {35}) is False


def test_the_index_check_only_counts_day_unit_records():
    body = PLUGIN_SRC[
        PLUGIN_SRC.index("async def _load_covered_day_ids") : PLUGIN_SRC.index(
            "async def _run_day_unit_cycle"
        )
    ]
    assert "WHERE compaction_level = 1 AND notes LIKE %s" in body
    assert '"%day_unit%"' in body


def _dummy_db(monkeypatch, rows):
    """Point core.db.get_conn_ctx at a connection that answers with `rows` once."""
    import core.db as cdb

    class DummyCursor:
        def __init__(self):
            self.served = False

        async def execute(self, sql, params=None):
            self.sql = sql

        async def fetchall(self):
            if self.served:
                return []
            self.served = True
            return rows

        async def fetchone(self):
            # Only the WebUI preview counts rows (a COUNT(*) query); the day-unit pass
            # itself never asks for a single row.
            return {"n": len(rows)}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class DummyConn:
        def cursor(self):
            return DummyCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(cdb, "get_conn_ctx", lambda: DummyConn())


def _day_row(row_id: int, content: str = "a day") -> dict:
    return {
        "id": row_id,
        "content": content,
        "personal_thought": "",
        "tags": "[]",
        "created_at": "2026-09-09 00:00:00",
    }


@pytest.mark.asyncio
async def test_a_day_already_in_the_archive_is_not_summarised_again(monkeypatch):
    plugin = GrilloCompactorPlugin()
    rows = [_day_row(43), _day_row(51)]
    _dummy_db(monkeypatch, rows)

    async def covered() -> set:
        return {43}

    monkeypatch.setattr(plugin, "_load_covered_day_ids", covered)

    compacted = []

    async def fake_compact(row, dry_run=False):
        compacted.append(row.get("id"))
        return {"row_id": row.get("id"), "status": "persisted"}

    monkeypatch.setattr(plugin, "_compact_one_day", fake_compact)

    summary = await plugin._run_day_unit_cycle()

    assert compacted == [51], compacted
    assert summary["skipped_covered"] == 1
    assert summary["covered_total"] == 1
    assert summary["persisted"] == 1
    assert summary["model_calls"] == 1, "a skipped day must not cost a model call"


@pytest.mark.asyncio
async def test_an_unreadable_archive_skips_the_pass_instead_of_duplicating(monkeypatch):
    plugin = GrilloCompactorPlugin()
    _dummy_db(monkeypatch, [_day_row(43)])

    async def boom() -> set:
        raise RuntimeError("no archive")

    monkeypatch.setattr(plugin, "_load_covered_day_ids", boom)

    compacted = []

    async def fake_compact(row, dry_run=False):
        compacted.append(row.get("id"))
        return {"row_id": row.get("id"), "status": "persisted"}

    monkeypatch.setattr(plugin, "_compact_one_day", fake_compact)

    summary = await plugin._run_day_unit_cycle()

    assert compacted == [], (
        "an unreadable index must not produce a second memory for a day"
    )
    assert summary["archive_unreadable"] is True, (
        "the panel has to be able to say the pass stood down rather than report a quiet night"
    )
    assert summary["model_calls"] == 0


def test_the_summary_counts_every_outcome():
    """A manual run reports what it did: persisted, left alone, failed."""
    from plugins.grillo.grillo_compactor.grillo_compactor import _day_unit_summary

    summary = _day_unit_summary(
        dry_run=False,
        considered=5,
        skipped_failed=1,
        skipped_covered=2,
        covered_total=39,
        archive_unreadable=False,
        results=[
            {"row_id": 1, "status": "persisted"},
            {"row_id": 2, "status": "declined"},
            {"row_id": 3, "status": "write_failed"},
        ],
    )

    assert summary["persisted"] == 1
    assert summary["left_unchanged"] == 1
    assert summary["errors"] == 1
    assert summary["model_calls"] == 3
    assert summary["skipped_covered"] == 2
    assert summary["skipped_failed"] == 1
    assert summary["considered"] == 5


@pytest.mark.asyncio
async def test_a_second_press_while_one_runs_is_refused():
    """Two presses must not double the night's model calls."""
    plugin = GrilloCompactorPlugin.__new__(GrilloCompactorPlugin)
    plugin._day_unit_failed = set()

    class _Running:
        def done(self) -> bool:
            return False

    plugin._compaction_task = _Running()

    async def preview() -> dict:
        return {"days": 10, "covered": 39, "remaining": 0, "age_days": 2}

    plugin.compaction_preview = preview

    out = await plugin.start_compaction_now()

    assert out["started"] is False
    assert out["reason"] == "already_running"
    assert out["running"] is True


@pytest.mark.asyncio
async def test_the_panel_reports_the_last_summary(monkeypatch):
    """A finished run keeps its summary readable until the next one starts."""
    plugin = GrilloCompactorPlugin.__new__(GrilloCompactorPlugin)
    plugin._compaction_task = None
    plugin._compaction_state = {
        "dry_run": False,
        "started_at": "2026-09-27T05:00:03+00:00",
        "finished_at": "2026-09-27T05:02:22+00:00",
        "error": None,
        "summary": {"persisted": 5, "skipped_covered": 4, "model_calls": 5},
    }

    async def preview() -> dict:
        return {"days": 10, "covered": 39, "remaining": 0, "age_days": 2}

    monkeypatch.setattr(plugin, "compaction_preview", preview)

    state = await plugin.compaction_status()

    assert state["running"] is False
    assert state["summary"]["persisted"] == 5
    assert state["summary"]["skipped_covered"] == 4
    assert state["preview"]["covered"] == 39
    assert state["finished_at"] == "2026-09-27T05:02:22+00:00"


@pytest.mark.asyncio
async def test_the_preview_counts_what_the_archive_covers(monkeypatch):
    """The panel says what a press would do before anyone presses it."""
    plugin = GrilloCompactorPlugin.__new__(GrilloCompactorPlugin)
    _dummy_db(monkeypatch, [_day_row(43)])

    async def covered() -> set:
        return {43, 51}

    monkeypatch.setattr(plugin, "_load_covered_day_ids", covered)

    preview = await plugin.compaction_preview()

    # The numbers describe the same selection the pass makes: one day would be considered,
    # the archive already covers it, so a press would spend no model call on it. The
    # archive's own id set also holds days that are no longer in ai_diary (51 here), which
    # must not be counted against the days still stored.
    assert preview["days"] == 1
    assert preview["covered"] == 1
    assert preview["remaining"] == 0
    assert preview["stored_days"] == 1
