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


# Every facial-expression tag form observed from the model, including the
# drifted colon-separated ``[em:grin:0.5]``. ``_EXPR`` above recognises only the
# documented ``[em_NAME:INTENSITY]`` form, so anything that must not SPEAK a tag
# cannot rely on that parse. Deliberately requires a tag-shaped tail (``em``
# followed by ``_name`` / ``:number`` and nothing else), so ordinary bracketed
# prose such as ``[email]`` or ``[3 hours earlier]`` is left alone.
_SPEECH_TAG_RE = re.compile(
    r"\s*\[\s*em(?:[_:][A-Za-z_]+)?(?:\s*:\s*\d+(?:\.\d+)?)*\s*\]",
    re.IGNORECASE,
)


def strip_speech_tags(text: str) -> str:
    """Remove facial-expression tags from text that will be SPOKEN (TTS).

    Kept separate from :func:`parse_facial_expressions` on purpose: the chat text
    keeps its tags (the owner wants to see them), but a synthesiser must never
    read them aloud, and the chat path's parse only matches the documented
    ``[em_NAME:INTENSITY]`` form while the model routinely emits
    ``[em:NAME:INTENSITY]``. Both forms, and the bare ``[em]`` reset, are
    removed here.
    """
    if not text:
        return text
    cleaned = _SPEECH_TAG_RE.sub("", text)
    return re.sub(r" {2,}", " ", cleaned).strip()
