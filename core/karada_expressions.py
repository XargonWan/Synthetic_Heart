"""Facial expressions: the ``[em_NAME:INTENSITY]`` tags the model writes, played on the face.

This used to be a plugin; the face is Karada's, so it lives with the Karada state server
(``KaradaStateServer.expression_guidance``, ``schedule_expressions``,
``apply_expression_tags`` and ``play_expression_timeline`` delegate here).

* **Guidance** teaches the model the tag protocol, with the expression names of the active
  persona (a skin may rename or add its own).
* A **timeline** is the tags of one reply placed on the length of its speech: each
  expression starts where its tag stood in the text, lasts until the next tag, and the last
  one is held until the end of the audio, when the face returns to the base emotional state.
  The length is the real audio duration when there is audio, else an estimate from the
  persona's ``facial_expression_chars_per_sec``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.facial_expression_parser import (
    FacialExpressionEvent,
    parse_facial_expressions,
)
from core.logging_utils import log_debug

if TYPE_CHECKING:
    from core.animation_handler import KaradaStateServer

_LOG_PREFIX = "[karada_expressions]"
DEFAULT_CHARS_PER_SEC = 12
#: The expressions guidance names when the persona declares none.
DEFAULT_EXPRESSIONS = ("smile", "grin", "sad", "blush", "surprised", "angry")


@dataclass
class _TimelineEvent:
    delay: float
    name: str | None
    intensity: float


def persona_expressions() -> tuple[float, dict[str, Any]]:
    """``(characters per second, expression catalogue)`` of the active persona.

    The catalogue is ``{name: {"targets": {blendshape: value}}}``; both fall back to
    defaults (never raise) when there is no persona or it declares nothing.
    """
    persona_json: dict[str, Any] | None = None
    try:
        from core.persona_manager import get_persona_manager

        manager = get_persona_manager()
        current = getattr(manager, "_current_persona", None) if manager else None
        if manager and current is not None and getattr(current, "name", None):
            persona_json = manager._load_persona_json(current.name)
    except Exception:  # noqa: BLE001 - a missing persona is the default face
        persona_json = None
    if not isinstance(persona_json, dict):
        return float(DEFAULT_CHARS_PER_SEC), {}
    rate = persona_json.get("facial_expression_chars_per_sec", DEFAULT_CHARS_PER_SEC)
    section = persona_json.get("facial_expressions", {})
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = float(DEFAULT_CHARS_PER_SEC)
    return rate, section if isinstance(section, dict) else {}


def expression_guidance() -> str:
    """The prompt block that teaches the tag protocol, with the persona's own names."""
    _, section = persona_expressions()
    names = ", ".join(section.keys() or DEFAULT_EXPRESSIONS) or "<none>"
    return (
        "You can embed facial expression tags in your message text: [em_NAME:INTENSITY]"
        f"\nAvailable: {names}"
        "\nINTENSITY: float 0.0-1.0."
        "\nEach expression persists until the next [em_...] tag or the end of the audio message."
        "\nAt the end of the audio, the face automatically returns to the current base emotional state."
        "\nUse [em] (bare) to return to base emotional state mid-sentence."
        "\nDo NOT add a reset tag at the end of your message — it happens automatically."
        '\nThese tags are invisible to users. Example: "Ciao! [em_grin:0.9] Come va?"'
        "\nOnly use these when responding to interfaces that support face rendering."
    )


async def play_expression_timeline(
    karada: KaradaStateServer,
    events: list[FacialExpressionEvent],
    total_chars: int,
    chars_per_sec: float,
    expr_section: dict[str, Any] | None = None,
    audio_duration_s: float | None = None,
) -> None:
    """Drive the face through ``events``.

    ``expr_section`` is the persona's catalogue: each event's blendshape targets are
    resolved here and sent already scaled by the intensity, so a client applies the right
    morphs without knowing the catalogue. ``audio_duration_s`` replaces the character-based
    estimate with the real length of the speech.
    """
    total_duration = (
        audio_duration_s
        if audio_duration_s is not None and audio_duration_s > 0
        else (total_chars / chars_per_sec if chars_per_sec > 0 else 1.0)
    )
    timeline = [
        _TimelineEvent(
            (event.position / total_chars if total_chars > 0 else 0.0) * total_duration,
            event.name,
            event.intensity,
        )
        for event in events
    ]
    log_debug(
        f"{_LOG_PREFIX} timeline start: {len(timeline)} events, duration={total_duration:.2f}s"
    )
    loop = asyncio.get_event_loop()
    start = loop.time()
    for item in timeline:
        wait = item.delay - (loop.time() - start)
        if wait > 0:
            await asyncio.sleep(wait)
        targets: dict[str, float] | None = None
        clear = False
        entry = (expr_section or {}).get(item.name) if item.name else None
        if entry is not None:
            if not isinstance(entry, dict):
                log_debug(
                    f"{_LOG_PREFIX} ignoring malformed expression '{item.name}': "
                    f"expected dict, got {type(entry).__name__}"
                )
            else:
                raw = entry.get("targets", {})
                if isinstance(raw, dict) and raw:
                    targets = {
                        str(k): float(v) * item.intensity for k, v in raw.items()
                    }
                else:
                    clear = True  # an expression with no targets ("neutral") clears
        if clear:
            await karada.push_face_expression(None, 0)
        else:
            await karada.push_face_expression(
                item.name, item.intensity, targets=targets
            )
    remaining = total_duration - (loop.time() - start)
    if remaining > 0:
        await asyncio.sleep(remaining)
    log_debug(f"{_LOG_PREFIX} timeline done, back to the base emotional state")
    await karada.push_face_expression(None, 0)


def schedule_expressions(
    karada: KaradaStateServer,
    events: list[FacialExpressionEvent],
    total_chars: int,
    audio_duration_s: float | None = None,
) -> bool:
    """Play ``events`` in the background, when somebody is watching. Whether it was scheduled."""
    if not events or not karada.has_connected_clients():
        return False
    chars_per_sec, section = persona_expressions()
    asyncio.get_running_loop().create_task(
        play_expression_timeline(
            karada,
            events,
            total_chars,
            chars_per_sec,
            expr_section=section,
            audio_duration_s=audio_duration_s,
        )
    )
    return True


def apply_expression_tags(
    karada: KaradaStateServer, text: str, audio_duration_s: float | None = None
) -> str:
    """The text without its tags; the tags it held are played on the face."""
    clean, events = parse_facial_expressions(text)
    schedule_expressions(karada, events, len(clean), audio_duration_s)
    return clean
