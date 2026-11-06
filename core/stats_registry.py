"""Small in-process statistics: daily counters, rolling timing series and gauges.

Why it exists: the WebUI's Logs > Stats page, and the Rift Vessel's *auto pacing* (``core.vessel_beat.auto_pacing``)
which times the next "what do you want to do?" from how long the last turns really took. So it is an input of the
program, not only a dashboard, and every read is cheap and in memory.

* ``incr(name, n=1, **labels)``: a counter per local day. A labelled call counts both the bare ``name`` and the
  labelled ``name{k=v}`` series, so a total never needs summing.
* ``observe(name, seconds, **labels)``: a timing series, the last ``WINDOW`` samples, with their times.
* ``gauge(name, value)``: the latest value of something (queue depth).

Nothing here ever raises: a statistic must not be able to break a turn. Counters of the last ``KEEP_DAYS`` days and
the timing windows are written to ``state_path("stats.json")`` now and then and at exit, so "today" survives a restart.
Tests (pytest) never touch the file.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional

#: Samples kept per timing series.
WINDOW = 200
#: Days of counters kept (and written).
KEEP_DAYS = 14
#: At most one write to disk per this many seconds.
SAVE_EVERY_SEC = 60.0


def series_key(name: str, labels: Dict[str, Any] | None = None) -> str:
    """``name`` or ``name{a=1,b=2}`` (labels sorted, so the same labels always make the same key)."""
    if not labels:
        return name
    inner = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
    return f"{name}{{{inner}}}"


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an already sorted list (``q`` in 0..1). 0.0 for an empty list."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(q * len(sorted_values)))
    return float(sorted_values[min(rank, len(sorted_values)) - 1])


class StatsRegistry:
    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        clock: Callable[[], float] = time.time,
        window: int = WINDOW,
        keep_days: int = KEEP_DAYS,
    ) -> None:
        self._path = path
        self._clock = clock
        self._window = int(window)
        self._keep_days = int(keep_days)
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[str, int]] = {}  # day -> series key -> n
        self._timings: Dict[
            str, Deque[tuple[float, float]]
        ] = {}  # series key -> (time, seconds)
        self._gauges: Dict[str, tuple[float, float]] = {}  # name -> (time, value)
        self._info: Dict[
            str, tuple[float, Dict[str, Any]]
        ] = {}  # name -> (time, latest small dict)
        self._dirty = False
        self._last_save = clock()
        if path is not None:
            self.load()

    # -- writing ----------------------------------------------------------------------------------------------

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d")

    def incr(self, name: str, n: int = 1, **labels: Any) -> None:
        try:
            with self._lock:
                day = self._counters.setdefault(self._today(), {})
                for key in {name, series_key(name, labels)}:
                    day[key] = day.get(key, 0) + int(n)
                self._dirty = True
            self._maybe_save()
        except Exception:  # pragma: no cover - a statistic never breaks anything
            pass

    def observe(self, name: str, seconds: float, **labels: Any) -> None:
        try:
            value = float(seconds)
            if value != value or value < 0:  # NaN or negative: a clock that went back
                return
            now = self._clock()
            with self._lock:
                for key in {name, series_key(name, labels)}:
                    series = self._timings.get(key)
                    if series is None:
                        series = self._timings[key] = deque(maxlen=self._window)
                    series.append((now, value))
                self._dirty = True
            self._maybe_save()
        except Exception:  # pragma: no cover
            pass

    def gauge(self, name: str, value: float) -> None:
        try:
            with self._lock:
                self._gauges[name] = (self._clock(), float(value))
        except Exception:  # pragma: no cover
            pass

    def info(self, name: str, value: Dict[str, Any]) -> None:
        """Keep the latest small JSON-able dict under ``name`` (what the pacing decided last). Not persisted."""
        try:
            with self._lock:
                self._info[name] = (self._clock(), dict(value))
        except Exception:  # pragma: no cover
            pass

    # -- reading ----------------------------------------------------------------------------------------------

    def count(self, name: str, *, day: Optional[str] = None, **labels: Any) -> int:
        with self._lock:
            return int(
                self._counters.get(day or self._today(), {}).get(
                    series_key(name, labels), 0
                )
            )

    def timing(
        self, name: str, *, max_age: Optional[float] = None, **labels: Any
    ) -> Dict[str, float]:
        """``{count, mean, p50, p95, max, last, age}`` of a series; ``max_age`` ignores older samples. Empty: ``count`` 0."""
        key = series_key(name, labels)
        now = self._clock()
        with self._lock:
            samples = list(self._timings.get(key, ()))
        if max_age is not None:
            samples = [s for s in samples if now - s[0] <= max_age]
        if not samples:
            return {
                "count": 0,
                "mean": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "max": 0.0,
                "last": 0.0,
                "age": float("inf"),
            }
        values = sorted(v for _, v in samples)
        return {
            "count": len(values),
            "mean": sum(values) / len(values),
            "p50": percentile(values, 0.5),
            "p95": percentile(values, 0.95),
            "max": values[-1],
            "last": samples[-1][1],
            "age": max(0.0, now - samples[-1][0]),
        }

    def snapshot(self, days: int = 7) -> Dict[str, Any]:
        """Everything the Stats page shows: counters per day (newest first), timing summaries and gauges."""
        days = max(1, min(int(days), self._keep_days))
        with self._lock:
            ordered = sorted(self._counters, reverse=True)[:days]
            per_day = {d: dict(self._counters[d]) for d in ordered}
            names = list(self._timings)
            gauges = {
                k: {"value": v, "age": max(0.0, self._clock() - t)}
                for k, (t, v) in self._gauges.items()
            }
            info = {
                k: {**v, "age": round(max(0.0, self._clock() - t), 1)}
                for k, (t, v) in self._info.items()
            }
        timings: Dict[str, Any] = {}
        for key in names:
            summary = self.timing_by_key(key)
            if summary["count"]:
                timings[key] = summary
        return {
            "today": self._today(),
            "days": per_day,
            "timings": timings,
            "gauges": gauges,
            "info": info,
        }

    def timing_by_key(self, key: str) -> Dict[str, float]:
        """:meth:`timing` for an already built series key (``name{a=1}``)."""
        now = self._clock()
        with self._lock:
            samples = list(self._timings.get(key, ()))
        if not samples:
            return {"count": 0}
        values = sorted(v for _, v in samples)
        return {
            "count": len(values),
            "mean": round(sum(values) / len(values), 3),
            "p50": round(percentile(values, 0.5), 3),
            "p95": round(percentile(values, 0.95), 3),
            "max": round(values[-1], 3),
            "last": round(samples[-1][1], 3),
            "age": round(max(0.0, now - samples[-1][0]), 1),
        }

    # -- keeping ----------------------------------------------------------------------------------------------

    def _prune_locked(self) -> None:
        for day in sorted(self._counters, reverse=True)[self._keep_days :]:
            del self._counters[day]

    def _maybe_save(self) -> None:
        if self._path is None or not self._dirty:
            return
        if self._clock() - self._last_save < SAVE_EVERY_SEC:
            return
        self.save()

    def save(self) -> bool:
        if self._path is None:
            return False
        try:
            with self._lock:
                self._prune_locked()
                payload = {
                    "version": 1,
                    "counters": self._counters,
                    "timings": {k: list(v) for k, v in self._timings.items()},
                }
                text = json.dumps(payload)
                self._dirty = False
                self._last_save = self._clock()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self._path)
            return True
        except Exception:
            return False

    def load(self) -> bool:
        if self._path is None:
            return False
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            counters = raw.get("counters") or {}
            timings = raw.get("timings") or {}
            with self._lock:
                for day, series in counters.items():
                    self._counters[str(day)] = {
                        str(k): int(v) for k, v in dict(series).items()
                    }
                for key, samples in timings.items():
                    self._timings[str(key)] = deque(
                        ((float(t), float(v)) for t, v in samples), maxlen=self._window
                    )
                self._prune_locked()
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False


# -- the process-wide registry ---------------------------------------------------------------------------------

_registry: Optional[StatsRegistry] = None
_registry_lock = threading.Lock()


def get_stats() -> StatsRegistry:
    """The one registry of this process (persisted, except under pytest or ``SYNTH_STATS_PERSIST=0``)."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                path: Optional[Path] = None
                persist = os.getenv("SYNTH_STATS_PERSIST", "1").strip().lower() not in (
                    "0",
                    "false",
                    "no",
                )
                if persist and not os.getenv("PYTEST_CURRENT_TEST"):
                    try:
                        from core.app_paths import state_path

                        path = state_path("stats.json")
                    except Exception:
                        path = None
                _registry = StatsRegistry(path)
                if path is not None:
                    atexit.register(_registry.save)
    return _registry


def incr(name: str, n: int = 1, **labels: Any) -> None:
    get_stats().incr(name, n, **labels)


def observe(name: str, seconds: float, **labels: Any) -> None:
    get_stats().observe(name, seconds, **labels)


def gauge(name: str, value: float) -> None:
    get_stats().gauge(name, value)


def info(name: str, value: Dict[str, Any]) -> None:
    get_stats().info(name, value)


def reset_for_tests() -> None:
    """Drop the process-wide registry (a test starts from nothing)."""
    global _registry
    with _registry_lock:
        _registry = None
