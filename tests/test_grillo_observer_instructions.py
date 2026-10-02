# tests/test_grillo_observer_instructions.py
"""Guard the G.R.I.L.L.O. beat instruction blocks against silent rule loss.

``plugins/grillo/common_instructions.py`` is the beat-specific half of the G.R.I.L.L.O.
instructions. Nearly every sentence in it answers a live incident, and the
incident is not visible in the wording — which makes a well-meaning trim the
easiest way to reintroduce an old bug.

So each obligation gets a marker sentence asserted here. A rewrite that drops one
fails loudly and names it, instead of the loss showing up days later as "the
outreach went quiet again".

The size guard exists because these constants ride in the USER body of every
observer beat: like the shared rule set, they are a per-run cost, so growth
should be a deliberate diff rather than drift.

Note: ``test_grillo_observer.py`` already carries a focused guard for the
awaiting-reply-gate removal (it asserts the restored wording directly). This file
is deliberately broader and cheaper to read — one marker per obligation, so a
trim names the obligation it dropped instead of failing on a sentence.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from plugins.grillo.common_instructions import (  # noqa: E402
    GRILLO_INSTRUCTIONS,
    OBSERVER_PROACTIVE_INSTRUCTIONS,
)

# One marker per obligation in GRILLO_INSTRUCTIONS.
GRILLO_OBLIGATIONS = {
    "actions-only output": "Return ONLY a single JSON object with an 'actions' array",
    "empty-list escape hatch": 'If there is nothing worth proposing, return {"actions": []}.',
    "no canned greetings": "Avoid formulaic openings",
    "unified send_message example": '"type": "send_message"',
    "no invented routing path": "never invented and never a placeholder",
    "no system labels to the user": "Do NOT address or mention the WebUI",
}

# One marker per obligation in OBSERVER_PROACTIVE_INSTRUCTIONS. Grouped by the
# incident each one answers.
OBSERVER_OBLIGATIONS = {
    # A quiet network is the reason the beat runs — it must not read as "stay silent".
    "quiet network is the point": "you are not a passive logger",
    "quiet network framing kept": "exactly the situation this run is for",
    # Content must be genuine, not scripted.
    "content over speaking": "what must stay genuine is the CONTENT",
    "no scripted opener": "never a canned or scripted opener",
    "no repeatable message": "would look the same tomorrow",
    "never announces itself": "checking in",
    "reaching out is the purpose of the beat": (
        "reaching out to it is the purpose of the beat"
    ),
    # The initiative must be grounded in an internal state.
    "grounded in a diary entry": "create_personal_diary_entry",
    # Routing: the snippet's own path, verbatim.
    "reply to the snippet's own path": "copy that snippet's own interface_path verbatim",
    "default target is the first/active chat": "DEFAULT TARGET",
    "no drift to a group or channel": "Do not drift to a group or a channel",
    "eligible list only when not replying": "ELIGIBLE TARGETS list ONLY",
    "no placeholder paths": (
        "never use placeholders like 'internal', 'grillo', 'system', 'main' or '-1'"
    ),
    "unified send_message carries the path": "send with the unified 'send_message' action",
    "never target an unreachable interface": (
        "never target an interface that is not in the snippets or ELIGIBLE TARGETS"
    ),
    # Anti-spam.
    "do not interrupt a live conversation": "LIVE-CONVERSATION",
    "speaking last is not off-limits": "does NOT put it off-limits",
    "silence is not the goal": "silence is not",
    # Stale context.
    "stale context section present": "STALE CONTEXT",
    "no fabricated reply id": "never fabricate a reply_message_id",
    "an old message is not a reason to skip": "is not a reason to skip a target",
    # Grounding: idle time is not a physical-presence fact.
    "grounding section present": "GROUNDING",
    "no 'went out' claim": "'went out'",
    "no 'came home' claim": "'came home'",
    "no 'almost here' claim": "'is almost here'",
}

#: Measured sizes plus ~15% headroom. Raise deliberately, in a commit that says
#: why; do not let them drift up.
GRILLO_INSTRUCTIONS_BUDGET = 1900
OBSERVER_PROACTIVE_INSTRUCTIONS_BUDGET = 5300


def test_grillo_instructions_keep_every_obligation() -> None:
    missing = [
        f"{label} ({marker!r})"
        for label, marker in GRILLO_OBLIGATIONS.items()
        if marker not in GRILLO_INSTRUCTIONS
    ]
    assert not missing, "GRILLO_INSTRUCTIONS lost an obligation: " + "; ".join(missing)


def test_observer_proactive_instructions_keep_every_obligation() -> None:
    """Each marker answers a live outreach incident; none may be trimmed away."""
    missing = [
        f"{label} ({marker!r})"
        for label, marker in OBSERVER_OBLIGATIONS.items()
        if marker not in OBSERVER_PROACTIVE_INSTRUCTIONS
    ]
    assert not missing, (
        "OBSERVER_PROACTIVE_INSTRUCTIONS lost an obligation: " + "; ".join(missing)
    )


def test_grillo_instruction_blocks_stay_within_budget() -> None:
    assert len(GRILLO_INSTRUCTIONS) <= GRILLO_INSTRUCTIONS_BUDGET, (
        f"GRILLO_INSTRUCTIONS is {len(GRILLO_INSTRUCTIONS)} chars, over its "
        f"{GRILLO_INSTRUCTIONS_BUDGET}-char budget"
    )
    assert (
        len(OBSERVER_PROACTIVE_INSTRUCTIONS) <= OBSERVER_PROACTIVE_INSTRUCTIONS_BUDGET
    ), (
        f"OBSERVER_PROACTIVE_INSTRUCTIONS is {len(OBSERVER_PROACTIVE_INSTRUCTIONS)} chars, "
        f"over its {OBSERVER_PROACTIVE_INSTRUCTIONS_BUDGET}-char budget"
    )
