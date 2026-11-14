"""Whether a turn is answered by voice.

A voice reply has no writing phase: the avatar stays in THINK while the answer is
generated and goes straight to TALK when the audio starts (the Karada state server fires
TALK from ``broadcast_audio``). Anything that would push the avatar to WRITE for such a turn
asks :func:`is_voice_reply` first.
"""

from __future__ import annotations

from typing import Any


def is_voice_reply(context: Any) -> bool:
    """True when the turn behind ``context`` is answered by voice (voice input, a
    requested voice reply, or a live call)."""
    if not isinstance(context, dict):
        return False
    return bool(
        context.get("is_voice_input")
        or context.get("request_tts")
        or context.get("call")
    )
