"""Small in-process statistics: daily counters, rolling timing series and gauges.

Why it exists: the WebUI's Logs > Stats page, and the Rift Vessel's *auto pacing* (``core.vessel_beat.auto_pacing``)
which times the next "what do you want to do?" from how long the last turns really took. So it is an input of the
program, not only a dashboard, and every read is cheap and in memory.

* ``incr(name, n=1, **labels)``: a counter per local day. A labelled call counts both the bare ``name`` and the
  labelled ``name{k=v}`` series, so a total never needs summing.
* ``observe(name, seconds, **labels)``: a timing series, the last ``WINDOW`` samples, with their times.
* ``gauge(name, value)``: the latest value of something (queue depth).

* ``register_section(source, title, build)``: the *stat server*. A plugin that wants a block on the Logs > Stats page
  registers a ``build(view) -> list[block]`` callable when it starts and calls ``unregister_section`` when it stops.
  The page knows no plugin: it draws the blocks it gets, and a block (or a whole section) with no data in the chosen
  period is not sent at all, so a disabled plugin or an engine nobody used leaves no empty entry behind.
* ``track_engine(router, engine, ...)``: times one call of an engine (a cortex endpoint, a vox/auris/iris engine) and
  files it by router, engine, scope and purpose.

Nothing here ever raises: a statistic must not be able to break a turn. Counters of the last ``KEEP_DAYS`` days and
the timing windows are written to ``state_path("stats.json")`` now and then and at exit, so "today" survives a restart.
Tests (pytest) never touch the file.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import functools
import json
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
)

#: Samples kept per timing series.
WINDOW = 200
#: Samples kept by the series that are read over days (engine latencies, the turn time): a window of 200 would
#: cover minutes on a busy engine.
LONG_WINDOW = 1000
#: Name prefixes that use :data:`LONG_WINDOW`.
LONG_WINDOW_PREFIXES = ("engine.", "turn.seconds")
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
        self._sections: Dict[str, Section] = {}  # source -> section (in memory only)
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

    def _window_for(self, key: str) -> int:
        if key.startswith(LONG_WINDOW_PREFIXES):
            return max(self._window, LONG_WINDOW)
        return self._window

    def observe(self, name: str, seconds: float, **labels: Any) -> None:
        self.observe_many(name, seconds, [labels])

    def observe_many(
        self, name: str, seconds: float, label_sets: Iterable[Dict[str, Any]]
    ) -> None:
        """One sample filed under the bare ``name`` and under each of ``label_sets`` (each set is its own series).

        ``observe`` files under the bare name and one labelled series; this is for a sample that must also be
        readable by *other* groupings (per router, per engine, per purpose) without being counted twice in the total.
        """
        try:
            value = float(seconds)
            if value != value or value < 0:  # NaN or negative: a clock that went back
                return
            now = self._clock()
            keys = {name}
            keys.update(series_key(name, labels) for labels in label_sets)
            with self._lock:
                for key in keys:
                    series = self._timings.get(key)
                    if series is None:
                        series = self._timings[key] = deque(
                            maxlen=self._window_for(key)
                        )
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

    def timing_by_key(
        self, key: str, *, max_age: Optional[float] = None
    ) -> Dict[str, float]:
        """:meth:`timing` for an already built series key (``name{a=1}``), rounded for display."""
        now = self._clock()
        with self._lock:
            samples = list(self._timings.get(key, ()))
        if max_age is not None:
            samples = [s for s in samples if now - s[0] <= max_age]
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

    def timing_keys(self, name: str) -> List[str]:
        """Series keys of timing ``name`` that carry labels (``name{...}``)."""
        prefix = f"{name}{{"
        with self._lock:
            return [k for k in self._timings if k.startswith(prefix)]

    def day_names(self, days: Optional[int]) -> List[str]:
        """The local days of a period, today first: the last ``days`` calendar days, or every day kept (``None``)."""
        if days is None:
            with self._lock:
                return sorted(self._counters, reverse=True)
        today = datetime.fromtimestamp(self._clock())
        return [
            (today - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(max(1, min(int(days), self._keep_days)))
        ]

    def count_days(self, key: str, days: Iterable[str]) -> int:
        with self._lock:
            return sum(int(self._counters.get(d, {}).get(key, 0)) for d in days)

    def counter_keys(self, name: str, days: Iterable[str]) -> List[str]:
        """Labelled series keys of counter ``name`` seen on any of ``days``."""
        prefix = f"{name}{{"
        found: set[str] = set()
        with self._lock:
            for d in days:
                found.update(
                    k for k in self._counters.get(d, {}) if k.startswith(prefix)
                )
        return sorted(found)

    def gauge_value(self, name: str) -> Optional[float]:
        with self._lock:
            item = self._gauges.get(name)
        return None if item is None else item[1]

    def info_value(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._info.get(name)
        if item is None:
            return None
        return {**item[1], "age": round(max(0.0, self._clock() - item[0]), 1)}

    # -- sections (the stat server) ---------------------------------------------------------------------------

    def register_section(
        self,
        source: str,
        title: str,
        build: Callable[[StatsView], List[Dict[str, Any]]],
        *,
        order: int = 100,
    ) -> None:
        """Offer a section to the Stats page. Registering the same ``source`` again replaces it."""
        with self._lock:
            self._sections[str(source)] = Section(str(source), title, build, int(order))

    def unregister_section(self, source: str) -> None:
        with self._lock:
            self._sections.pop(str(source), None)

    def sections(self) -> List[str]:
        with self._lock:
            return sorted(self._sections)

    def report(self, period: Period | None = None) -> Dict[str, Any]:
        """What the Stats page draws: the blocks of every registered section that has data in ``period``."""
        period = period or Period()
        view = StatsView(self, period)
        with self._lock:
            registered = sorted(
                self._sections.values(), key=lambda x: (x.order, x.source)
            )
        out: List[Dict[str, Any]] = []
        for section in registered:
            try:
                blocks = [b for b in map(_prune_block, section.build(view)) if b]
            except Exception:  # a broken plugin section must not blank the page
                continue
            if blocks:
                out.append(
                    {"source": section.source, "title": section.title, "blocks": blocks}
                )
        return {
            "period": {"key": period.key, "label": period.label},
            "today": self._today(),
            "sections": out,
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
                        ((float(t), float(v)) for t, v in samples),
                        maxlen=self._window_for(str(key)),
                    )
                self._prune_locked()
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False


# -- periods, views and sections (the stat server) ------------------------------------------------------------


def parse_key(key: str) -> Tuple[str, Dict[str, str]]:
    """``name{a=1,b=2}`` -> ``("name", {"a": "1", "b": "2"})``; the inverse of :func:`series_key`."""
    if key.endswith("}") and "{" in key:
        name, _, inner = key[:-1].partition("{")
        labels: Dict[str, str] = {}
        for part in inner.split(","):
            k, _, v = part.partition("=")
            if k:
                labels[k] = v
        return name, labels
    return key, {}


class Period:
    """What the Stats page looks at: ``max_age`` seconds of timing samples and the counters of ``days`` local days."""

    #: ``key -> (label, max_age seconds or None, counter days or None)``; ``None`` days = everything kept.
    CHOICES: Dict[str, Tuple[str, Optional[float], Optional[int]]] = {
        "1h": ("last hour", 3600.0, 1),
        "24h": ("last 24 hours", 86400.0, 1),
        "3d": ("last 3 days", 3 * 86400.0, 3),
        "7d": ("last 7 days", 7 * 86400.0, 7),
        "14d": ("last 14 days", 14 * 86400.0, 14),
        "all": ("everything kept", None, None),
    }
    DEFAULT = "7d"

    def __init__(self, key: str = DEFAULT) -> None:
        if key not in self.CHOICES:
            key = self.DEFAULT
        self.key = key
        self.label, self.max_age, self.days = self.CHOICES[key]

    @classmethod
    def parse(cls, value: Any = None, days: Any = None) -> "Period":
        """From ``?period=`` (``1h``, ``24h``, ``3d``, ``7d``, ``14d``, ``all``) or the older ``?days=N``."""
        text = str(value or "").strip().lower()
        if text in cls.CHOICES:
            return cls(text)
        if days not in (None, ""):
            try:
                n = max(1, min(int(days), KEEP_DAYS))
                return cls(f"{n}d" if f"{n}d" in cls.CHOICES else cls.DEFAULT)
            except (TypeError, ValueError):
                pass
        return cls()


class StatsView:
    """The registry seen through one :class:`Period`: what a section's ``build`` reads."""

    def __init__(self, registry: "StatsRegistry", period: Period) -> None:
        self.registry = registry
        self.period = period
        self._days = registry.day_names(period.days)

    # counters
    def count(self, key: str) -> int:
        """A counter summed over the days of the period."""
        return self.registry.count_days(key, self._days)

    def counts(self, name: str) -> List[Tuple[Dict[str, str], int]]:
        """Every labelled series of counter ``name`` with its sum over the period (biggest first)."""
        out = []
        for key in self.registry.counter_keys(name, self._days):
            _, labels = parse_key(key)
            out.append((labels, self.count(key)))
        return sorted(out, key=lambda item: -item[1])

    def per_day(self, keys: List[str]) -> List[Tuple[str, List[int]]]:
        """``(day, [n per key])`` newest first, for the days of the period."""
        return [
            (day, [self.registry.count_days(k, [day]) for k in keys])
            for day in sorted(self._days, reverse=True)
        ]

    # timings
    def timing(self, key: str) -> Dict[str, float]:
        """Summary of a series over the period (``count`` 0 when it has no sample in it)."""
        return self.registry.timing_by_key(key, max_age=self.period.max_age)

    def timings(self, name: str) -> List[Tuple[Dict[str, str], Dict[str, float]]]:
        """Every labelled series of timing ``name`` that has samples in the period (most used first)."""
        out = []
        for key in self.registry.timing_keys(name):
            summary = self.timing(key)
            if summary["count"]:
                out.append((parse_key(key)[1], summary))
        return sorted(out, key=lambda item: -item[1]["count"])

    # latest values
    def gauge(self, name: str) -> Optional[float]:
        return self.registry.gauge_value(name)

    def info(self, name: str) -> Optional[Dict[str, Any]]:
        return self.registry.info_value(name)


def cards_block(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Big numbers. An item is ``{label, value, kind: count|seconds|text, tone: good|bad|""}``; dropped if all are 0."""
    return {"type": "cards", "items": items}


def timing_row(
    label: str,
    summary: Dict[str, float],
    *,
    hint: str = "",
    keep_empty: bool = False,
    **extra: Any,
) -> Dict[str, Any]:
    """One row of a :func:`timing_table`; ``extra`` (e.g. ``ok``/``failed``) become extra columns."""
    row: Dict[str, Any] = {
        "label": label,
        "hint": hint,
        "keep_empty": keep_empty,
        "n": int(summary.get("count", 0)),
    }
    for field in ("mean", "p50", "p95", "max", "last"):
        row[field] = summary.get(field)
    row.update(extra)
    return row


def timing_table(
    title: str, rows: List[Dict[str, Any]], *, first: str = ""
) -> Dict[str, Any]:
    """A table of timing rows; shown only if a row has samples (empty ``keep_empty`` rows then show "no samples yet")."""
    return {"type": "timing_table", "title": title, "first": first, "rows": rows}


def counters_block(title: str, items: List[Tuple[str, int]]) -> Dict[str, Any]:
    """``label: n`` pairs (e.g. why the idle beat was held back); shown only if some count is above 0."""
    return {
        "type": "counters",
        "title": title,
        "items": [{"label": label, "value": n} for label, n in items],
    }


def note_block(text: str) -> Dict[str, Any]:
    return {"type": "note", "text": text}


def day_table_block(
    title: str, columns: List[str], rows: List[Tuple[str, List[int]]]
) -> Dict[str, Any]:
    """Counts per day (newest first); days that are all zero are dropped."""
    return {
        "type": "day_table",
        "title": title,
        "columns": columns,
        "rows": [{"day": day, "values": values} for day, values in rows],
    }


def _prune_block(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Drop what has no data in the period; ``None`` when nothing of the block is left."""
    kind = block.get("type")
    if kind == "cards":
        items = [i for i in block.get("items", []) if i.get("value") is not None]
        return {**block, "items": items} if any(i.get("value") for i in items) else None
    if kind == "timing_table":
        rows = block.get("rows", [])
        if not any(r.get("n") for r in rows):
            return None
        return {**block, "rows": [r for r in rows if r.get("n") or r.get("keep_empty")]}
    if kind == "counters":
        items = [i for i in block.get("items", []) if i.get("value")]
        return {**block, "items": items} if items else None
    if kind == "day_table":
        rows = [r for r in block.get("rows", []) if any(r.get("values", []))]
        return {**block, "rows": rows} if rows else None
    if kind == "note":
        return block if str(block.get("text") or "").strip() else None
    return None


class Section:
    __slots__ = ("source", "title", "order", "build")

    def __init__(
        self,
        source: str,
        title: str,
        build: Callable[[StatsView], List[Dict[str, Any]]],
        order: int,
    ) -> None:
        self.source = source
        self.title = title
        self.order = order
        self.build = build


# -- engine calls ------------------------------------------------------------------------------------------------

#: Why an LLM/engine call is made: ``chat`` (the default), ``brief``, ``debrief``, ``agent_round``, ``grillo``...
_purpose_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "synth_stats_purpose", default="chat"
)
#: Which cortex scope the turn runs under (base, vessel, grillo, trainer, agent...): the "route" of the call.
_scope_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "synth_stats_scope", default=""
)

_LABEL_UNSAFE = re.compile(r"[=,{}\s]+")


def clean_label(value: Any) -> str:
    """A label value that cannot break a ``name{k=v}`` key."""
    text = _LABEL_UNSAFE.sub("_", str(value or "").strip())[:60].strip("_")
    return text or "unknown"


@contextlib.contextmanager
def purpose(name: str) -> Iterator[None]:
    """Everything that calls an engine inside this block is filed under ``name`` (``with purpose("debrief"): ...``)."""
    token = _purpose_var.set(clean_label(name))
    try:
        yield
    finally:
        _purpose_var.reset(token)


def with_purpose(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for an async function: the engine calls it makes are filed under purpose ``name``."""

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            with purpose(name):
                return await func(*args, **kwargs)

        return wrapper

    return decorate


@contextlib.contextmanager
def scope(name: Optional[str]) -> Iterator[None]:
    """The engine calls made inside this block carry ``name`` as their route (the cortex scope of the turn)."""
    token = _scope_var.set(clean_label(name) if name else "")
    try:
        yield
    finally:
        _scope_var.reset(token)


class _EngineCall:
    """Handed to the ``with track_engine(...)`` body: call :meth:`fail` for a failure that is not an exception."""

    __slots__ = ("failed",)

    def __init__(self) -> None:
        self.failed = False

    def fail(self) -> None:
        self.failed = True


@contextlib.contextmanager
def track_engine(
    router: str,
    engine: str,
    *,
    scope: Optional[str] = None,
    purpose_name: Optional[str] = None,
) -> Iterator[_EngineCall]:
    """Time one call of ``engine`` on ``router`` (cortex, vox, auris, iris) and file it.

    A successful call is a sample of ``engine.seconds``, a failed one of ``engine.failed_seconds`` (time to the
    failure), each filed under the router, the router+engine, the router+scope (the route) and the purpose, so the
    page can show a global, a per-engine, a per-route and a per-purpose figure with real sample times (a period
    shorter than a day still counts right). An exception counts as a failure and propagates; a cancelled call is not
    the engine's doing and is not counted. Never raises by itself.
    """
    call = _EngineCall()
    start = time.monotonic()
    cancelled = False
    try:
        yield call
    except BaseException as exc:
        if isinstance(exc, (GeneratorExit, KeyboardInterrupt)) or type(
            exc
        ).__name__ in ("CancelledError",):
            cancelled = True
        else:
            call.failed = True
        raise
    finally:
        if not cancelled:
            try:
                elapsed = time.monotonic() - start
                r, e = clean_label(router), clean_label(engine)
                sc = clean_label(scope) if scope else _scope_var.get()
                pu = clean_label(purpose_name) if purpose_name else _purpose_var.get()
                label_sets: List[Dict[str, Any]] = [
                    {"router": r},
                    {"router": r, "engine": e},
                    {"purpose": pu},
                ]
                if sc:
                    label_sets.append({"router": r, "scope": sc})
                name = "engine.failed_seconds" if call.failed else "engine.seconds"
                get_stats().observe_many(name, elapsed, label_sets)
            except Exception:  # pragma: no cover - a statistic never breaks anything
                pass


def tracked_engine(
    router: str, engine: Any, *, purpose_name: Optional[str] = None
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for an async engine method that returns its text/audio: time it with :func:`track_engine`.

    ``engine`` is the engine's name, or a callable ``self -> name``. A falsy/empty result counts as a failure.
    """

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                name = engine(self) if callable(engine) else engine
            except Exception:
                name = "unknown"
            with track_engine(
                router, name or "unknown", purpose_name=purpose_name
            ) as call:
                result = await func(self, *args, **kwargs)
                if not result:
                    call.fail()
                return result

        return wrapper

    return decorate


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
                try:
                    from core.stats_sections import register_core_sections

                    register_core_sections(_registry)
                except (
                    Exception
                ):  # pragma: no cover - the page then simply has no core sections
                    pass
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


def register_section(
    source: str,
    title: str,
    build: Callable[[StatsView], List[Dict[str, Any]]],
    *,
    order: int = 100,
) -> None:
    get_stats().register_section(source, title, build, order=order)


def unregister_section(source: str) -> None:
    get_stats().unregister_section(source)


def reset_for_tests() -> None:
    """Drop the process-wide registry (a test starts from nothing)."""
    global _registry
    with _registry_lock:
        _registry = None
