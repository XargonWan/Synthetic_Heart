"""Shared helpers for classifying Grillo autonomous beats.

A Grillo beat is either *internal* (introspection: diary, emotion, memory —
must NOT emit ``message_*`` actions) or *outbound* (user-facing: it targets a
real interface and IS allowed to send a message).

Historically only the ``outreach`` beat was outbound, so the distinction was
scattered across ~11 call sites as the literal ``beat_type == "outreach"`` (or
``not in ("outreach", None)``). The ``outreach`` beat was later folded into the
proactive ``observer`` beat, which is likewise outbound. Centralising the set
here keeps every gate consistent and avoids divergent literals drifting apart.
"""

from __future__ import annotations

# Beat types that are user-facing: they target a real interface and ARE
# permitted to emit ``message_*`` actions (and receive TTS, survive
# concurrent-user-message cancellation, and be excluded from language
# detection on their English system prompt).
OUTBOUND_BEAT_TYPES: frozenset[str] = frozenset({"observer"})


def is_outbound_beat(beat_type: object) -> bool:
    """Return True if ``beat_type`` denotes a user-facing (outbound) Grillo beat.

    Outbound beats are allowed to send ``message_*`` actions. Internal beats
    (any other Grillo ``beat_type``, or ``None``) are introspection-only.
    """
    return isinstance(beat_type, str) and beat_type in OUTBOUND_BEAT_TYPES
