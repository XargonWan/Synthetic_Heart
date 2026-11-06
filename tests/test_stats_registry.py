"""Tests for ``core/stats_registry.py``: counters per day, rolling timings, persistence, fail-safety."""

from __future__ import annotations

import json
from datetime import datetime

from core import stats_registry as sr
from core.stats_registry import StatsRegistry, percentile, series_key


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_series_key_is_stable_and_sorted():
    assert series_key("turns.ok") == "turns.ok"
    assert series_key("turns.ok", {"b": 2, "a": 1}) == "turns.ok{a=1,b=2}"


def test_labelled_incr_counts_total_and_series():
    s = StatsRegistry(clock=Clock())
    s.incr("turns.ok", interface="vessel")
    s.incr("turns.ok", interface="vessel")
    s.incr("turns.ok", interface="telegram")
    assert s.count("turns.ok") == 3
    assert s.count("turns.ok", interface="vessel") == 2
    assert s.count("turns.ok", interface="telegram") == 1
    assert s.count("turns.failed") == 0


def test_counters_roll_over_with_the_day():
    clock = Clock()
    s = StatsRegistry(clock=clock)
    first = datetime.fromtimestamp(clock.t).strftime("%Y-%m-%d")
    s.incr("x")
    clock.t += 86400 * 2
    s.incr("x", 5)
    assert s.count("x") == 5
    assert s.count("x", day=first) == 1
    snap = s.snapshot(days=7)
    assert len(snap["days"]) == 2


def test_old_days_are_pruned():
    clock = Clock()
    s = StatsRegistry(clock=clock, keep_days=3)
    for _ in range(6):
        s.incr("x")
        clock.t += 86400
    s.save()  # prune happens on save
    assert len(s.snapshot(days=30)["days"]) <= 3


def test_percentile_nearest_rank():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert percentile(values, 0.5) == 5.0
    assert percentile(values, 0.95) == 10.0
    assert percentile([], 0.5) == 0.0
    assert percentile([7.0], 0.95) == 7.0


def test_timing_summary_and_window():
    s = StatsRegistry(clock=Clock(), window=5)
    for v in [10, 20, 30, 40, 50, 60, 70]:
        s.observe("beat.ask_to_turn_done", v)
    t = s.timing("beat.ask_to_turn_done")
    assert t["count"] == 5  # window
    assert t["max"] == 70 and t["last"] == 70
    assert t["p50"] == 50
    assert s.timing("nothing")["count"] == 0


def test_timing_max_age_ignores_old_samples():
    clock = Clock()
    s = StatsRegistry(clock=clock)
    s.observe("t", 100)
    clock.t += 700
    s.observe("t", 10)
    assert s.timing("t", max_age=600)["count"] == 1
    assert s.timing("t", max_age=600)["p50"] == 10
    assert s.timing("t")["count"] == 2


def test_bad_observations_are_dropped():
    s = StatsRegistry(clock=Clock())
    s.observe("t", -1)
    s.observe("t", float("nan"))
    s.observe("t", "not a number")  # type: ignore[arg-type]
    assert s.timing("t")["count"] == 0


def test_labelled_observe_feeds_total_and_series():
    s = StatsRegistry(clock=Clock())
    s.observe("vessel.verb.seconds", 2.0, verb="move")
    s.observe("vessel.verb.seconds", 1.0, verb="look")
    assert s.timing("vessel.verb.seconds")["count"] == 2
    assert s.timing("vessel.verb.seconds", verb="move")["p50"] == 2.0


def test_gauge_keeps_the_latest():
    clock = Clock()
    s = StatsRegistry(clock=clock)
    s.gauge("queue.depth", 3)
    clock.t += 5
    s.gauge("queue.depth", 1)
    g = s.snapshot()["gauges"]["queue.depth"]
    assert g["value"] == 1.0 and g["age"] == 0.0


def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "stats.json"
    clock = Clock()
    a = StatsRegistry(path, clock=clock)
    a.incr("turns.ok", 4)
    a.observe("beat.cycle", 33.0)
    assert a.save()
    b = StatsRegistry(path, clock=clock)
    assert b.count("turns.ok") == 4
    assert b.timing("beat.cycle")["p50"] == 33.0


def test_save_is_throttled_but_save_forces(tmp_path):
    path = tmp_path / "stats.json"
    clock = Clock()
    s = StatsRegistry(path, clock=clock)
    s.incr("x")
    assert not path.exists()  # inside the throttle window
    clock.t += 61
    s.incr("x")
    assert path.exists()


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "stats.json"
    path.write_text("{not json", encoding="utf-8")
    s = StatsRegistry(path, clock=Clock())
    s.incr("x")
    assert s.count("x") == 1


def test_unknown_future_shapes_do_not_crash(tmp_path):
    path = tmp_path / "stats.json"
    path.write_text(
        json.dumps({"counters": {"2026-01-01": {"a": "x"}}, "timings": []}),
        encoding="utf-8",
    )
    s = StatsRegistry(path, clock=Clock())
    s.incr("y")
    assert s.count("y") == 1


def test_module_helpers_never_persist_under_pytest():
    sr.reset_for_tests()
    sr.incr("helper.test")
    assert sr.get_stats()._path is None
    assert sr.get_stats().count("helper.test") == 1
    sr.reset_for_tests()
