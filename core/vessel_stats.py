"""The Rift Vessel's section of the Logs > Stats page: where the time of the RetroXR idle beat goes.

The Vessel interface registers it when it starts and removes it when it stops (``register``/``unregister``), so with
the Vessel off the page has no trace of it. The series are recorded by the interface and the RetroXR connector
(``beat.*``, ``body.busy_seconds``, ``vessel.verb.*``); the sentences about what the pacing is doing now are made here
from the same ``beat.pacing`` info the scheduler publishes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core import stats_registry
from core.stats_registry import (
    StatsView,
    cards_block,
    counters_block,
    day_table_block,
    note_block,
    timing_row,
    timing_table,
)

SOURCE = "vessel"

#: ``(series, label, hint)``: the phases of one idle beat, in the order they happen.
BEAT_PHASES = (
    (
        "beat.ask_to_first_verb",
        "asked → first verb",
        "how long the model took to answer",
    ),
    (
        "beat.ask_to_turn_done",
        "asked → turn finished",
        "the whole turn, debrief included: the input of the auto wait",
    ),
    (
        "beat.turn_done_to_settled",
        "turn finished → body done",
        "a clip or a voice still playing after the turn",
    ),
    (
        "beat.settled_to_next_ask",
        "done → next ask",
        "the dead time between two beats: what the pacing sets",
    ),
    ("beat.cycle", "ask → next ask", "the whole cycle"),
    ("body.busy_seconds", "body busy", "how long her body is busy after a verb"),
)


def _seconds(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "–"
    return f"{round(n)} s" if n >= 100 else f"{n:.1f} s"


def pacing_note(pacing: Optional[Dict[str, Any]]) -> str:
    """The sentence about what the idle beat is doing now (empty when no beat has been timed)."""
    if not pacing:
        return "No idle beat has been timed yet (no RetroXR session in this run)."
    return (
        f"Now: {pacing.get('mode')} – {pacing.get('reason') or ''}. "
        f"Waits {_seconds(pacing.get('interval'))} after she and her body are done "
        f"(after an action: {_seconds(pacing.get('gap'))}; a beat may go out "
        f"{_seconds(pacing.get('lead'))} before her body is done). "
        f"Next due in {_seconds(pacing.get('due_in'))}."
    )


def holds_note(view: StatsView) -> str:
    """Why beats were held back in the period, and the queue as it is now."""
    holds = [
        f"{labels.get('reason', '?')}: {n}"
        for labels, n in view.counts("beat.hold")
        if n
    ]
    text = f"Beats held ({view.period.label}) – " + (" · ".join(holds) or "none") + "."
    queue, backlog = view.gauge("queue.depth"), view.gauge("queue.vessel_backlog")
    if queue is not None:
        text += f" Queue now: {int(queue)}."
    if backlog is not None:
        text += f" Her turns waiting: {int(backlog)}."
    return text


def build_vessel_section(view: StatsView) -> List[Dict[str, Any]]:
    """Cards, the idle-beat phase table, the pacing sentences and the verbs of the period."""
    verbs_bad = view.count("vessel.verb.failed") + view.count("vessel.verb.timeout")
    cards = cards_block(
        [
            {
                "label": "idle beats asked",
                "value": view.count("beat.asked"),
                "kind": "count",
            },
            {
                "label": "beats that led to an action",
                "value": view.count("beat.acted"),
                "kind": "count",
                "tone": "good",
            },
            {
                "label": "beats that led to nothing",
                "value": view.count("beat.did_nothing"),
                "kind": "count",
            },
            {
                "label": "vessel verbs ok",
                "value": view.count("vessel.verb.ok"),
                "kind": "count",
                "tone": "good",
            },
            {
                "label": "vessel verbs failed / timed out",
                "value": verbs_bad,
                "kind": "count",
                "tone": "bad" if verbs_bad else "",
            },
        ]
    )
    phases = timing_table(
        "Idle beat – where the time goes",
        [
            timing_row(label, view.timing(key), hint=hint, keep_empty=True)
            for key, label, hint in BEAT_PHASES
        ],
        first="phase",
    )
    # The sentences only make sense once the vessel did something in the period.
    active = any(view.timing(key)["count"] for key, _, _ in BEAT_PHASES) or view.count(
        "beat.asked"
    )
    notes: List[Dict[str, Any]] = []
    if active:
        notes = [
            note_block(pacing_note(view.info("beat.pacing"))),
            note_block(holds_note(view)),
        ]
    held = counters_block(
        "Beats held back",
        [(labels.get("reason", "?"), n) for labels, n in view.counts("beat.hold")],
    )
    verb_rows = []
    for labels, ok in view.counts("vessel.verb.ok"):
        verb = labels.get("verb", "?")
        failed = view.count(f"vessel.verb.failed{{verb={verb}}}") + view.count(
            f"vessel.verb.timeout{{verb={verb}}}"
        )
        verb_rows.append(
            timing_row(
                verb,
                {
                    **view.timing(f"vessel.verb.seconds{{verb={verb}}}"),
                    "count": ok + failed,
                },
                failed=failed,
            )
        )
    verbs = timing_table("Vessel verbs", verb_rows, first="verb")
    days = day_table_block(
        "Vessel, last days",
        ["verbs ok", "verbs failed", "beats → action", "beats → nothing"],
        [
            (
                day,
                [values[0], values[1] + values[2], values[3], values[4]],
            )
            for day, values in view.per_day(
                [
                    "vessel.verb.ok",
                    "vessel.verb.failed",
                    "vessel.verb.timeout",
                    "beat.acted",
                    "beat.did_nothing",
                ]
            )
        ],
    )
    return [cards, phases, *notes, held, verbs, days]


def register() -> None:
    """Offer the Vessel's section to the Stats page (the Vessel interface calls this when it starts)."""
    stats_registry.register_section(
        SOURCE, "RetroXR vessel", build_vessel_section, order=50
    )


def unregister() -> None:
    stats_registry.unregister_section(SOURCE)
