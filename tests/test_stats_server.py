"""The stat server: registered sections, the period, per-engine metrics, the general turn average, the vessel's section."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from core import stats_registry as sr
from core import vessel_stats
from core.stats_registry import Period, StatsRegistry, cards_block, note_block


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def fresh() -> Any:
    sr.reset_for_tests()
    yield
    sr.reset_for_tests()


def _section(report: Dict[str, Any], source: str) -> Dict[str, Any] | None:
    return next((s for s in report["sections"] if s["source"] == source), None)


def _need(report: Dict[str, Any], source: str) -> Dict[str, Any]:
    found = _section(report, source)
    assert found is not None, source
    return found


def test_period_parsing_and_defaults() -> None:
    assert Period.parse("1h").max_age == 3600
    assert Period.parse("all").max_age is None and Period.parse("all").days is None
    assert Period.parse("nope").key == "7d"
    assert Period.parse(None, "14").key == "14d"
    assert Period.parse(None, "banana").key == "7d"


def test_a_registered_section_appears_and_leaves_with_its_plugin() -> None:
    reg = StatsRegistry(clock=Clock())
    reg.register_section(
        "demo",
        "Demo",
        lambda view: [cards_block([{"label": "x", "value": 3, "kind": "count"}])],
    )
    assert _need(reg.report(), "demo")["title"] == "Demo"
    reg.unregister_section("demo")
    assert _section(reg.report(), "demo") is None


def test_a_section_with_no_data_is_not_sent() -> None:
    reg = StatsRegistry(clock=Clock())
    reg.register_section(
        "quiet",
        "Quiet",
        lambda view: [
            cards_block([{"label": "x", "value": 0, "kind": "count"}]),
            note_block("   "),
        ],
    )
    assert reg.report()["sections"] == []


def test_a_broken_section_does_not_blank_the_page() -> None:
    reg = StatsRegistry(clock=Clock())

    def boom(view: Any) -> Any:
        raise RuntimeError("plugin bug")

    reg.register_section("bad", "Bad", boom)
    reg.register_section("ok", "Ok", lambda v: [note_block("fine")])
    assert [s["source"] for s in reg.report()["sections"]] == ["ok"]


def test_timings_follow_the_period_by_sample_age() -> None:
    clock = Clock()
    reg = StatsRegistry(clock=clock)
    reg.observe("turn.seconds", 10.0)
    clock.t += 2 * 3600  # two hours later
    reg.observe("turn.seconds", 20.0)
    hour = sr.StatsView(reg, Period("1h")).timing("turn.seconds")
    week = sr.StatsView(reg, Period("7d")).timing("turn.seconds")
    assert hour["count"] == 1 and hour["mean"] == 20.0
    assert week["count"] == 2 and week["mean"] == 15.0


def test_counters_follow_the_days_of_the_period() -> None:
    clock = Clock()
    reg = StatsRegistry(clock=clock)
    reg.incr("turns.ok", 2)
    clock.t += 3 * 86400
    reg.incr("turns.ok", 5)
    assert sr.StatsView(reg, Period("24h")).count("turns.ok") == 5
    assert sr.StatsView(reg, Period("7d")).count("turns.ok") == 7
    assert sr.StatsView(reg, Period("all")).count("turns.ok") == 7


def test_track_engine_files_a_call_by_router_engine_route_and_purpose() -> None:
    with sr.scope("vessel"), sr.purpose("debrief"):
        with sr.track_engine("cortex", "zen-llm-engine"):
            pass
    reg = sr.get_stats()
    for key in (
        "engine.seconds",
        "engine.seconds{router=cortex}",
        "engine.seconds{engine=zen-llm-engine,router=cortex}",
        "engine.seconds{router=cortex,scope=vessel}",
        "engine.seconds{purpose=debrief}",
    ):
        assert reg.timing_by_key(key)["count"] == 1, key
    # the total is counted once, whatever the number of groupings
    assert len(reg._timings["engine.seconds"]) == 1


def test_track_engine_counts_exceptions_and_soft_failures_but_not_cancellations() -> (
    None
):
    with pytest.raises(ValueError):
        with sr.track_engine("cortex", "a"):
            raise ValueError
    with sr.track_engine("vox", "b") as call:
        call.fail()

    async def cancelled() -> None:
        with sr.track_engine("cortex", "c"):
            await asyncio.sleep(10)

    async def run() -> None:
        task = asyncio.create_task(cancelled())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    reg = sr.get_stats()
    assert (
        reg.timing_by_key("engine.failed_seconds{engine=a,router=cortex}")["count"] == 1
    )
    assert reg.timing_by_key("engine.failed_seconds{engine=b,router=vox}")["count"] == 1
    assert (
        reg.timing_by_key("engine.failed_seconds{engine=c,router=cortex}")["count"] == 0
    )
    assert reg.timing_by_key("engine.seconds{engine=c,router=cortex}")["count"] == 0


def test_label_values_cannot_break_a_series_key() -> None:
    assert sr.clean_label("a=b,c}{ d") == "a_b_c_d"
    assert sr.clean_label("") == "unknown"
    with sr.track_engine("cortex", "we=ird,name"):
        pass
    assert sr.get_stats().timing_keys("engine.seconds")
    assert sr.parse_key("engine.seconds{engine=we_ird_name,router=cortex}")[1] == {
        "engine": "we_ird_name",
        "router": "cortex",
    }


def test_tracked_engine_decorator_times_the_method_and_empty_is_a_failure() -> None:
    class Eng:
        name = "zen"

        @sr.tracked_engine("cortex", lambda self: self.name)
        async def generate_response(self, text: str) -> str:
            return text

    asyncio.run(Eng().generate_response("hello"))
    asyncio.run(Eng().generate_response(""))
    reg = sr.get_stats()
    assert reg.timing_by_key("engine.seconds{engine=zen,router=cortex}")["count"] == 1
    assert (
        reg.timing_by_key("engine.failed_seconds{engine=zen,router=cortex}")["count"]
        == 1
    )


def test_with_purpose_decorator_sets_the_purpose_for_the_call() -> None:
    @sr.with_purpose("recon")
    async def work() -> None:
        with sr.track_engine("cortex", "e"):
            pass

    asyncio.run(work())
    assert sr.get_stats().timing_by_key("engine.seconds{purpose=recon}")["count"] == 1
    assert sr._purpose_var.get() == "chat"  # and the default is back afterwards


def test_engine_tables_list_only_engines_with_data_in_the_period() -> None:
    clock = Clock()
    reg = StatsRegistry(clock=clock)
    # an engine that was used a week ago and nothing since
    clock.t -= 5 * 86400
    reg.observe_many(
        "engine.seconds",
        2.0,
        [{"router": "cortex"}, {"router": "cortex", "engine": "old"}],
    )
    clock.t += 5 * 86400
    reg.observe_many(
        "engine.seconds",
        4.0,
        [
            {"router": "cortex"},
            {"router": "cortex", "engine": "zen"},
            {"purpose": "chat"},
        ],
    )
    from core.stats_sections import build_engines_section

    def rows(period: str, title: str) -> list[str]:
        view = sr.StatsView(reg, Period(period))
        block = next(b for b in build_engines_section(view) if b["title"] == title)
        return [r["label"] for r in block["rows"]]

    assert rows("24h", "By engine") == ["cortex · zen"]
    assert sorted(rows("7d", "By engine")) == ["cortex · old", "cortex · zen"]


def test_the_general_turn_average_covers_every_kind_including_agent_turns() -> None:
    reg = sr.get_stats()
    reg.observe("turn.seconds", 10.0, interface="telegram", kind="chat")
    reg.observe("turn.seconds", 30.0, interface="telegram", kind="agent")
    report = reg.report(Period("24h"))
    cards = next(
        b for b in _need(report, "core.turns")["blocks"] if b["type"] == "cards"
    )
    average = next(i for i in cards["items"] if i["label"] == "average turn")
    assert average["value"] == 20.0 and average["kind"] == "seconds"


def test_samples_of_engine_and_turn_series_have_a_longer_window() -> None:
    reg = StatsRegistry(clock=Clock())
    for _ in range(500):
        reg.observe("turn.seconds", 1.0)
        reg.observe("beat.cycle", 1.0)
    assert reg.timing("turn.seconds")["count"] == 500
    assert reg.timing("beat.cycle")["count"] == sr.WINDOW


def test_vessel_section_exists_only_while_the_interface_runs() -> None:
    reg = sr.get_stats()
    reg.observe("beat.ask_to_turn_done", 40.0)
    assert _section(reg.report(), "vessel") is None
    vessel_stats.register()
    section = _section(reg.report(), "vessel")
    assert section is not None
    table = next(b for b in section["blocks"] if b["type"] == "timing_table")
    labels = [r["label"] for r in table["rows"]]
    # the phases with no sample yet stay in the table, as before
    assert "asked → turn finished" in labels and "turn finished → body done" in labels
    vessel_stats.unregister()
    assert _section(reg.report(), "vessel") is None


def test_vessel_pacing_note_keeps_the_old_wording() -> None:
    reg = sr.get_stats()
    reg.observe("beat.ask_to_turn_done", 40.0)
    reg.incr("beat.asked")
    reg.incr("beat.hold", reason="engine_busy")
    reg.gauge("queue.depth", 0)
    reg.info(
        "beat.pacing",
        {
            "mode": "fallback",
            "reason": "too few recent turns measured",
            "interval": 40.0,
            "gap": 10.0,
            "lead": 0.0,
            "due_in": 0.0,
        },
    )
    vessel_stats.register()
    section = _need(reg.report(Period("24h")), "vessel")
    notes = [b["text"] for b in section["blocks"] if b["type"] == "note"]
    assert notes[0].startswith(
        "Now: fallback – too few recent turns measured. Waits 40.0 s after she and her body are done "
        "(after an action: 10.0 s; a beat may go out 0.0 s before her body is done). Next due in 0.0 s."
    )
    assert "engine_busy: 1" in notes[1] and "Queue now: 0." in notes[1]
