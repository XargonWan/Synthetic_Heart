"""The sections of the Logs > Stats page that belong to the core: turns, deliveries and engine calls.

Plugins add their own with ``stats_registry.register_section``; these two are registered when the registry is created.
Each builder reads a :class:`~core.stats_registry.StatsView` (the registry seen through the chosen period) and returns
blocks. A block with no data in the period is dropped by the registry, so nothing here has to check for emptiness.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from core.stats_registry import (
    StatsRegistry,
    StatsView,
    cards_block,
    day_table_block,
    timing_row,
    timing_table,
)

#: What the engine tables are grouped by: ``(title, label keys of the series, how a row is named)``.
_ENGINE_GROUPS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("By engine", ("engine", "router"), "{router} · {engine}"),
    ("By route (router and cortex scope)", ("router", "scope"), "{router} · {scope}"),
    ("By purpose", ("purpose",), "{purpose}"),
)


def _percent(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "–"


def build_turns_section(view: StatsView) -> List[Dict[str, Any]]:
    """Turns (any prompt, whatever it was), actions and deliveries."""
    ok, failed = view.count("turns.ok"), view.count("turns.failed")
    total = ok + failed
    turn = view.timing("turn.seconds")
    cards: List[Dict[str, Any]] = [
        {
            "label": "average turn",
            "value": turn.get("mean") if turn["count"] else None,
            "kind": "seconds",
            "sub": f"p50 {turn['p50']} s · p95 {turn['p95']} s · {turn['count']} turns"
            if turn["count"]
            else "",
        },
        {"label": "turns done", "value": total, "kind": "count"},
        {"label": "turns ok", "value": ok, "kind": "count", "tone": "good"},
        {
            "label": f"turns failed ({_percent(failed, total)})",
            "value": failed,
            "kind": "count",
            "tone": "bad" if failed else "",
        },
        {
            "label": "LLM failures",
            "value": view.count("llm.failures"),
            "kind": "count",
            "tone": "bad" if view.count("llm.failures") else "",
        },
        {
            "label": "actions ok",
            "value": view.count("actions.ok"),
            "kind": "count",
            "tone": "good",
        },
        {
            "label": "actions failed",
            "value": view.count("actions.failed"),
            "kind": "count",
            "tone": "bad" if view.count("actions.failed") else "",
        },
        {
            "label": "deliveries ok",
            "value": view.count("delivery.ok"),
            "kind": "count",
            "tone": "good",
        },
        {
            "label": "deliveries failed",
            "value": view.count("delivery.failed"),
            "kind": "count",
            "tone": "bad" if view.count("delivery.failed") else "",
        },
    ]
    rows = [
        timing_row(
            "every turn",
            turn,
            hint="all interfaces and kinds, agent turns included: the general time one prompt takes",
        )
    ]
    for labels, summary in view.timings("turn.seconds"):
        if set(labels) != {"interface", "kind"}:
            continue
        rows.append(timing_row(f"{labels['interface']} · {labels['kind']}", summary))
    wait = view.timing("turn.wait_seconds")
    rows.append(
        timing_row(
            "waiting in the queue",
            wait,
            hint="from the message arriving to its turn starting",
        )
    )
    keys = [
        "turns.ok",
        "turns.failed",
        "llm.failures",
        "actions.failed",
        "delivery.failed",
    ]
    return [
        cards_block(cards),
        timing_table("Time per turn", rows, first="turns"),
        day_table_block(
            "Last days",
            [
                "turns ok",
                "turns failed",
                "LLM failures",
                "actions failed",
                "deliveries failed",
            ],
            view.per_day(keys),
        ),
    ]


def _engine_row(
    label: str, ok: Dict[str, Any], failed: Dict[str, Any]
) -> Dict[str, Any]:
    """A row of the engine tables: ``n`` calls, ``failed`` of them, the times of the ones that worked."""
    n_ok, n_failed = int(ok.get("count", 0)), int(failed.get("count", 0))
    summary = dict(ok) if n_ok else {}
    summary["count"] = n_ok + n_failed
    return timing_row(label, summary, failed=n_failed)


def build_engines_section(view: StatsView) -> List[Dict[str, Any]]:
    """Latency of every engine call, globally and by router, engine, route and purpose.

    An engine that nobody called in the period has no series in it, so it simply does not appear.
    """
    ok_series = view.timings("engine.seconds")
    failed_series = view.timings("engine.failed_seconds")
    blocks: List[Dict[str, Any]] = []

    def by_labels(
        series: List[Tuple[Dict[str, str], Dict[str, float]]], keys: Tuple[str, ...]
    ):
        return {
            tuple(sorted(labels.items())): summary
            for labels, summary in series
            if set(labels) == set(keys)
        }

    rows = [
        _engine_row(
            "all engine calls",
            view.timing("engine.seconds"),
            view.timing("engine.failed_seconds"),
        )
    ]
    routers_ok = by_labels(ok_series, ("router",))
    routers_failed = by_labels(failed_series, ("router",))
    for key in sorted(set(routers_ok) | set(routers_failed)):
        rows.append(
            _engine_row(
                dict(key)["router"],
                routers_ok.get(key, {}),
                routers_failed.get(key, {}),
            )
        )
    blocks.append(timing_table("Engine calls", rows, first="router"))

    for title, keys, template in _ENGINE_GROUPS:
        ok_map, failed_map = by_labels(ok_series, keys), by_labels(failed_series, keys)
        group_rows = [
            _engine_row(
                template.format(**dict(key)),
                ok_map.get(key, {}),
                failed_map.get(key, {}),
            )
            for key in sorted(
                set(ok_map) | set(failed_map),
                key=lambda k: (
                    -(
                        int(ok_map.get(k, {}).get("count", 0))
                        + int(failed_map.get(k, {}).get("count", 0))
                    )
                ),
            )
        ]
        blocks.append(timing_table(title, group_rows))
    return blocks


def register_core_sections(registry: StatsRegistry) -> None:
    """Called when the process-wide registry is created."""
    registry.register_section("core.turns", "Turns", build_turns_section, order=10)
    registry.register_section(
        "core.engines", "LLM and engines", build_engines_section, order=20
    )
