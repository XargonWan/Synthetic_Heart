"""Structural detection of a *Rift Vessel* turn (keyword-free).

A single shared helper for deciding whether the turn currently being processed
originates from a Vessel embodiment ("SyntH is in the world"). The decision is
made purely from routing metadata — never from message text (project rule: no
keyword logic) — using three independent structural signals:

1. the routing ``interface_path`` starts with ``vessel`` (``vessel/<world>...``);
2. an explicit ``vessel_focus`` flag is set on the context memory;
3. the message's chat type is ``vessel``.

This mirrors the historical inline detection in
:mod:`core.history_engine` so that the history engine, the prompt engine and the
vessel-gated recon plugin all share one identical, fail-safe implementation.
"""

from __future__ import annotations

from typing import Any, Optional


def is_vessel_turn(
    message: Any | None,
    context_memory: Any | None,
    interface_path: Optional[str] = None,
) -> bool:
    """Return ``True`` when the current turn is a Vessel embodiment turn.

    The detection is entirely structural and fully guarded: any unexpected shape
    or attribute-access failure resolves to ``False`` so callers can rely on it
    without their own try/except.

    Args:
        message: The message object being processed (may expose ``chat.type``
            and/or ``interface_path``). May be ``None``.
        context_memory: The context memory dict (may carry a ``vessel_focus``
            flag). May be ``None`` or a non-dict.
        interface_path: Optional explicit routing ``interface_path``. When not
            provided, it is best-effort read from ``message.interface_path``.

    Returns:
        ``True`` if any of the three structural vessel signals is present.
    """
    try:
        path = interface_path
        if path is None and message is not None:
            path = getattr(message, "interface_path", None)
        if isinstance(path, str) and path.startswith("vessel"):
            return True

        if isinstance(context_memory, dict) and context_memory.get("vessel_focus"):
            return True

        chat_obj = getattr(message, "chat", None) if message is not None else None
        if chat_obj is not None and getattr(chat_obj, "type", None) == "vessel":
            return True
    except Exception:
        return False
    return False
