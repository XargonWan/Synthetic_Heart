from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Pattern matches [em], [em_name], [em_name:0.5], [em:0.3]
_EXPR = re.compile(r"\[em(?:_([a-z_]+))?(?::([0-9.]+))?\]")


@dataclass
class FacialExpressionEvent:
    position: int  # character index in cleaned text where this event occurs
    name: Optional[str]  # expression name, None means reset
    intensity: float  # 0.0-1.0


def parse_facial_expressions(text: str) -> Tuple[str, List[FacialExpressionEvent]]:
    """Return (clean_text, events).

    Removes all `[em... ]` tags from *text* and returns the cleaned
    string along with a list of events containing the offset where the
    tag appeared in the cleaned text.

    The name is None for a bare `[em]` reset tag.  Intensity defaults
    to 1.0 when omitted.
    """

    events: List[FacialExpressionEvent] = []
    cleaned_chars: List[str] = []
    last_idx = 0

    for m in _EXPR.finditer(text):
        # append text before the match
        cleaned_chars.append(text[last_idx : m.start()])
        last_idx = m.end()

        name = m.group(1)
        intensity_str = m.group(2)
        intensity = float(intensity_str) if intensity_str is not None else 1.0

        pos = len("".join(cleaned_chars))
        # bare [em] or [em_:x] -> reset
        if name == "" or name is None:
            name = None
        events.append(
            FacialExpressionEvent(position=pos, name=name, intensity=intensity)
        )
    # append remainder
    cleaned_chars.append(text[last_idx:])
    clean_text = "".join(cleaned_chars)
    return clean_text, events
