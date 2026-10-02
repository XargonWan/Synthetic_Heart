"""Guards for the reference-clarity instruction and the no-Italian invariant.

Both assertions used to live in this file as source-greps against
``core/prompt_engine.py``, because the wording they guarded was inside
``load_unminified_chat_instruction()`` — a variant with no production caller,
deleted with the instruction-budget work. The obligations themselves are still
real, so they are re-pointed at the live rule instead of being dropped:

- the reference-clarity obligation now lives in
  ``core/prompt_instructions/rules.py`` and is asserted through the rendered
  instruction block, not through the file that happens to contain the text;
- the "no leftover Italian" invariant was never about that function. It is a
  guard on the whole prompt engine, so it stays a source check.
"""

from __future__ import annotations

import io
import os

from core.prompt_instructions import ROUTE_CHAT, build_instructions


def test_prompt_engine_has_no_leftover_italian_fragments() -> None:
    """The prompt engine's instruction text must be English only."""
    path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), os.pardir, "core", "prompt_engine.py")
    )
    with io.open(path, "r", encoding="utf-8") as handle:
        src_low = handle.read().lower()

    assert "ho visto" not in src_low and "qualcuno" not in src_low, (
        "Found Italian text in the prompt engine; instructions should be English only"
    )


def test_reference_clarity_obligation_is_on_the_live_rule() -> None:
    """Name the author rather than phrasing it vaguely.

    The obligation is rendered on the shared chat route, so it reaches every
    turn — it is no longer parked in a variant nothing called.
    """
    rendered = build_instructions(ROUTE_CHAT)

    assert "REFERENCE CLARITY" in rendered
    assert "name its author or speaker plainly" in rendered
