"""Pure helpers for the Telegram user-account extension.

Nothing here touches the network, Telethon or the Core: it reads attributes of the
objects it is given, so it is tested without any of them. Standard library only.
"""

from __future__ import annotations

import re
from typing import Any

INTERFACE_ID = "telegram"
#: Telegram refuses text longer than this, and captions longer than the second.
MAX_TEXT = 4096
MAX_CAPTION = 1024

_DIGITS = re.compile(r"^-?\d+$")
#: A public Telegram username (5-32 chars, letters/digits/underscore, starts with a letter).
USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")

_BOLD_RE = re.compile(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])")
_ITALIC_RE = re.compile(r"(?<![_\w])_(?!\s)([^_\n]+?)(?<!\s)_(?![_\w])")


def build_path(chat_id: Any, thread_id: Any = None) -> str:
    """``telegram/<chat>[/<thread>]``."""
    path = f"{INTERFACE_ID}/{chat_id}"
    return f"{path}/{thread_id}" if thread_id else path


def parse_path(path: str | None) -> tuple[int | str, int | None] | None:
    """``(chat, thread)`` from a path, or ``None`` if it is not ours.

    ``chat`` is a number for an id and a string for a public ``@username`` (the model
    is told to prefer that form); a non-numeric thread is dropped, not fatal.
    """
    if not path:
        return None
    parts = path.split("/")
    if parts[0] != INTERFACE_ID or len(parts) < 2:
        return None
    chat: int | str
    if _DIGITS.match(parts[1]):
        chat = int(parts[1])
    elif USERNAME_RE.match(parts[1]):
        chat = "@" + parts[1].lstrip("@")
    else:
        return None
    thread = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    return chat, thread


def usable_reply_id(reply_to: Any, chat: Any) -> int | None:
    """A reply target, or ``None`` when the value is not a real message id.

    Models often put the chat id or a placeholder here; quoting a message that does
    not exist fails the whole send, so those are dropped."""
    try:
        value = int(str(reply_to))
    except (TypeError, ValueError):
        return None
    return None if str(value) == str(chat) or value <= 0 else value


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Chunks of at most ``limit`` characters, cut at a line or word end when one is
    in the second half of the chunk."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", limit // 2, limit)
        if cut < 0:
            cut = rest.rfind(" ", limit // 2, limit)
        cut = cut + 1 if cut >= 0 else limit
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def parse_mode_for(chat_id: Any) -> str | None:
    """Groups get plain text: Markdown would eat ``*action*`` markers that other
    readers of the chat need to see. Private chats keep Markdown."""
    return None if str(chat_id).startswith("-") else "Markdown"


def legacy_markdown_to_telethon(text: str) -> str:
    """Bot-API legacy Markdown (``*b*``/``_i_``) to Telethon's ``**b**``/``__i__``."""
    text = _BOLD_RE.sub(r"**\1**", text)
    return _ITALIC_RE.sub(r"__\1__", text)


# ------------------------------------------------------------------- errors

#: Kinds of failure the sender reacts to.
REPLY_GONE = "reply_gone"
THREAD_GONE = "thread_gone"
CHAT_GONE = "chat_gone"
BAD_MARKUP = "bad_markup"
FLOOD = "flood"
OTHER = "other"

_RPC_KINDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("MSGIDINVALID", "MESSAGEIDINVALID", "REPLYMESSAGE", "REPLYTOINVALID"),
        REPLY_GONE,
    ),
    (("TOPICIDINVALID", "TOPICDELETED", "TOPICCLOSED"), THREAD_GONE),
    (
        (
            "PEERIDINVALID",
            "CHATIDINVALID",
            "CHANNELINVALID",
            "USERIDINVALID",
            "INPUTUSERDEACTIVATED",
        ),
        CHAT_GONE,
    ),
    (("ENTITIESTOOLONG", "ENTITYBOUNDSINVALID"), BAD_MARKUP),
)


def classify_error(exc: BaseException) -> tuple[str, float]:
    """``(kind, retry_after_seconds)`` for a Telethon or network failure.

    Telethon keeps the RPC code in the class name (``MsgIdInvalidError``), so this
    reads names and attributes and needs no Telethon import.
    """
    name = type(exc).__name__
    if name == "FloodWaitError":
        return FLOOD, float(getattr(exc, "seconds", 0) or 0)
    squashed = re.sub(r"Error$", "", name).upper()
    message = str(getattr(exc, "message", "") or "").upper().replace("_", "")
    for needles, kind in _RPC_KINDS:
        if any(n in squashed or n in message for n in needles):
            return kind, 0.0
    if isinstance(exc, ValueError) and "input entity" in str(exc).lower():
        return CHAT_GONE, 0.0
    return OTHER, 0.0


# ------------------------------------------------------------------ inbound


def chat_kind(entity: Any) -> str:
    """``private`` for a user, ``group`` otherwise (the Core knows only the two)."""
    return "private" if type(entity).__name__ == "User" else "group"


def thread_of(tl_message: Any) -> int | None:
    """The forum topic of a message, or ``None`` outside forum topics."""
    reply = getattr(tl_message, "reply_to", None)
    if reply is None or not getattr(reply, "forum_topic", False):
        return None
    top = getattr(reply, "reply_to_top_id", None)
    if top:
        return int(top)
    base = getattr(reply, "reply_to_msg_id", None)
    return int(base) if base else None


def full_name(entity: Any) -> str:
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    return " ".join(p for p in parts if p) or getattr(entity, "username", None) or ""


def title_of(entity: Any) -> str | None:
    if type(entity).__name__ == "User":
        return full_name(entity) or None
    return getattr(entity, "title", None) or None


def is_backlog(sent: Any, now: Any, max_age: float) -> bool:
    """True for a message that is history delivered late, not a live one.

    When the account is added to a chat Telegram sends its latest messages, and after
    downtime what was missed. Answering them one by one would be absurd. Purely the
    message's own timestamp against the clock."""
    if max_age <= 0 or sent is None:
        return False
    try:
        if sent.tzinfo is None:
            from datetime import timezone

            sent = sent.replace(tzinfo=timezone.utc)
        return (now - sent).total_seconds() > max_age
    except Exception:  # noqa: BLE001 - an odd timestamp is not backlog
        return False


def reply_block(replied: Any, sender: Any) -> dict[str, Any] | None:
    """The ``reply_to`` description of the message being answered."""
    if replied is None:
        return None
    info: dict[str, Any] = {
        "sender_name": full_name(sender) or "unknown",
        "text": getattr(replied, "raw_text", None) or "",
        "message_id": str(getattr(replied, "id", "")),
    }
    if sender is not None and getattr(sender, "id", None) is not None:
        info["sender_id"] = str(sender.id)
    return info


def media_name(doc: Any, default: str) -> str:
    for attr in getattr(doc, "attributes", None) or []:
        name = getattr(attr, "file_name", None)
        if name:
            return str(name)
    return default


def media_kinds(tl_message: Any) -> list[tuple[str, str, str]]:
    """``[(kind, default file name, mime)]`` of what a message carries.

    ``kind`` is one of photo, voice, video_note, video, audio, sticker, document; a
    voice note or video note also makes the message *spoken* (see :func:`is_spoken`).
    """
    found: list[tuple[str, str, str]] = []
    if getattr(tl_message, "photo", None):
        found.append(("photo", "photo.jpg", "image/jpeg"))
    for kind, name, mime in (
        ("voice", "voice.ogg", "audio/ogg"),
        ("video_note", "video_note.mp4", "video/mp4"),
        ("video", "video.mp4", "video/mp4"),
        ("audio", "audio.mp3", "audio/mpeg"),
        ("sticker", "sticker.webp", "image/webp"),
    ):
        doc = getattr(tl_message, kind, None)
        if doc:
            found.append(
                (kind, media_name(doc, name), getattr(doc, "mime_type", None) or mime)
            )
    if not found and getattr(tl_message, "document", None):
        doc = tl_message.document
        found.append(
            (
                "document",
                media_name(doc, "document"),
                getattr(doc, "mime_type", None) or "application/octet-stream",
            )
        )
    return found


def is_spoken(tl_message: Any) -> bool:
    return bool(
        getattr(tl_message, "voice", None) or getattr(tl_message, "video_note", None)
    )
