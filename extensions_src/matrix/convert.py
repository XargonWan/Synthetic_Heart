"""Pure helpers for the Matrix extension (no network, no Core). Standard library only."""

from __future__ import annotations

from typing import Any

INTERFACE_ID = "matrix"


def extract_username(mxid: str) -> str:
    """``alice`` from ``@alice:server`` (and the input itself when it has no server)."""
    if ":" in mxid:
        return mxid.split(":")[0].lstrip("@")
    return mxid.lstrip("@")


def parse_rooms(raw: Any) -> set[str]:
    """A comma separated setting as a set of trimmed, non-empty entries."""
    if not raw:
        return set()
    return {item.strip() for item in str(raw).split(",") if item.strip()}


def parse_reply_fallback(body: str) -> tuple[str, str] | None:
    """Split a Matrix rich-reply fallback body into ``(reply_text, quoted_text)``.

    Per the Matrix spec a rich reply carries the quoted content only as the plain-text
    fallback: leading lines starting with ``>`` (one per quoted line), then blank separator
    lines, then the real reply. Parsing it structurally is the only way to recover the quoted
    TEXT: ``m.in_reply_to`` carries just the ``event_id``, and resolving it would need a
    paginated room-history fetch. ``None`` when the body is not a reply fallback or nothing
    usable survives; never raises.
    """
    try:
        lines = (body or "").splitlines()
        quote_lines: list[str] = []
        idx = 0
        while idx < len(lines) and lines[idx].startswith(">"):
            quote_lines.append(lines[idx].lstrip(">").strip())
            idx += 1
        if not quote_lines:
            return None
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        reply_text = "\n".join(lines[idx:]).strip()
        quoted_text = "\n".join(line for line in quote_lines if line).strip()
        if not reply_text or not quoted_text:
            return None
        return reply_text, quoted_text
    except Exception:  # noqa: BLE001
        return None


def quoted_sender_from_fallback(quoted_text: str) -> str:
    """Best-effort display name from a fallback quote's first line.

    Element-family clients render it as ``> <@user:server> text`` (MXID form) or
    ``> * Display Name: text``. Structural prefix parsing only: anything unrecognised stays
    ``Unknown``.
    """
    try:
        first = quoted_text.splitlines()[0].strip()
        if first.startswith("<") and ">" in first:
            return extract_username(first[1 : first.index(">")]) or "Unknown"
        if first.startswith("* ") and ": " in first:
            return first[2 : first.index(": ")].strip() or "Unknown"
    except Exception:  # noqa: BLE001
        pass
    return "Unknown"


def build_path(room_id: str, thread_event_id: str | None = None) -> str:
    """``matrix/<room>`` or ``matrix/<room>/<thread event>``."""
    if thread_event_id:
        return f"{INTERFACE_ID}/{room_id}/{thread_event_id}"
    return f"{INTERFACE_ID}/{room_id}"


def split_path(path: str | None) -> tuple[str, str | None] | None:
    """``(room, thread event)`` of one of our paths, or ``None``."""
    if not path or not isinstance(path, str):
        return None
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] != INTERFACE_ID or len(parts) not in (2, 3):
        return None
    return parts[1], (parts[2] if len(parts) == 3 else None)


def relates_to(
    thread_event_id: str | None, reply_to_event_id: str | None
) -> dict[str, Any]:
    """The ``m.relates_to`` block of a message: a thread and/or a rich reply."""
    block: dict[str, Any] = {}
    if thread_event_id:
        block.update(
            {
                "event_id": thread_event_id,
                "rel_type": "m.thread",
                "is_falling_back": True,
            }
        )
    if reply_to_event_id:
        block.setdefault("m.in_reply_to", {"event_id": reply_to_event_id})
    return block


def msgtype_for(kind: str) -> str:
    """The Matrix ``msgtype`` for a media kind, so clients render images inline and give
    audio and video their native players."""
    return {"image": "m.image", "audio": "m.audio", "video": "m.video"}.get(
        kind, "m.file"
    )


def chat_type(member_count: int | None) -> str:
    """A room of two or fewer is a private chat."""
    return "private" if member_count is not None and member_count <= 2 else "group"


def private_allowed(
    policy: str, sender: str, trainer: str | None, trusted: set[str]
) -> bool:
    """Whether a message in a private room is accepted: any policy but ``trainer_only`` lets
    everyone in; ``trainer_only`` lets the trainer and the trusted users in."""
    if policy.lower() != "trainer_only":
        return True
    return bool(sender) and (sender == trainer or sender in trusted)


def invite_allowed(
    policy: str, inviter: str | None, trainer: str | None, trusted: set[str]
) -> bool:
    """Whether an invitation is accepted (same rule as :func:`private_allowed`)."""
    if policy.lower() == "allow_all":
        return True
    return bool(inviter) and (inviter == trainer or inviter in trusted)


def room_allowed(allowed: set[str], room_id: str, alias: str | None) -> bool:
    """An empty allow list means every room."""
    return not allowed or room_id in allowed or (alias is not None and alias in allowed)
