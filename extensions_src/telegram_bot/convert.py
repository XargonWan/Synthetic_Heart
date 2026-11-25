"""Pure conversion between Telegram objects and the Core's interface API.

Nothing here talks to the network or to the Core: it only reads attributes of
the objects it is given (python-telegram-bot messages, or anything shaped like
them) so it can be tested without either. Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

INTERFACE_ID = "telegram_bot"
#: Telegram refuses text longer than this, and captions longer than the second.
MAX_TEXT = 4096
MAX_CAPTION = 1024

_DIGITS = re.compile(r"^-?\d+$")


def build_path(chat_id: Any, thread_id: Any = None) -> str:
    """``telegram_bot/<chat>[/<thread>]``."""
    path = f"{INTERFACE_ID}/{chat_id}"
    return f"{path}/{thread_id}" if thread_id else path


def parse_path(path: str | None) -> tuple[int, int | None] | None:
    """Return ``(chat_id, thread_id)`` from a path, or ``None`` if it is not ours.

    A non-numeric thread (a placeholder the model invented) is dropped rather
    than failing the whole send; a non-numeric chat makes the path unusable.
    """
    if not path:
        return None
    parts = path.split("/")
    if parts[0] != INTERFACE_ID or len(parts) < 2 or not _DIGITS.match(parts[1]):
        return None
    thread = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    return int(parts[1]), thread


def message_thread(message: Any) -> int | None:
    """The forum topic of ``message``, or ``None`` outside forum topics.

    Telegram sets ``message_thread_id`` on plain replies in ordinary groups too;
    only ``is_topic_message`` says it is a real topic.
    """
    thread = getattr(message, "message_thread_id", None)
    if thread and getattr(message, "is_topic_message", False):
        return int(thread)
    return None


def reply_info(message: Any) -> dict[str, Any] | None:
    """Describe the message being replied to, or ``None``.

    In a forum every message points at the topic opener; that is not a reply.
    """
    original = getattr(message, "reply_to_message", None)
    if original is None:
        return None
    if getattr(message, "is_topic_message", False) and getattr(
        original, "message_id", None
    ) == getattr(message, "message_thread_id", None):
        return None
    sender = getattr(original, "from_user", None)
    name = getattr(sender, "full_name", None) or getattr(sender, "username", None) or ""
    info: dict[str, Any] = {
        "sender_name": name or "unknown",
        "text": getattr(original, "text", None)
        or getattr(original, "caption", None)
        or "",
        "message_id": str(getattr(original, "message_id", "")),
    }
    if sender is not None and getattr(sender, "id", None) is not None:
        info["sender_id"] = str(sender.id)
    return info


@dataclass
class MediaRef:
    """A file attached to an incoming message, not downloaded yet."""

    file_id: str
    filename: str
    mime_type: str
    size: int | None = None


def media_refs(message: Any) -> list[MediaRef]:
    """Every downloadable file of ``message`` (the largest photo size only)."""
    refs: list[MediaRef] = []
    photos = getattr(message, "photo", None)
    if photos:
        best = max(photos, key=lambda p: getattr(p, "file_size", 0) or 0)
        refs.append(
            MediaRef(
                best.file_id,
                "photo.jpg",
                "image/jpeg",
                getattr(best, "file_size", None),
            )
        )
    for attr, default_name, default_mime in (
        ("document", "document", "application/octet-stream"),
        ("voice", "voice.ogg", "audio/ogg"),
        ("audio", "audio.mp3", "audio/mpeg"),
        ("video", "video.mp4", "video/mp4"),
        ("video_note", "video_note.mp4", "video/mp4"),
        ("animation", "animation.mp4", "video/mp4"),
        ("sticker", "sticker.webp", "image/webp"),
    ):
        item = getattr(message, attr, None)
        if item is None:
            continue
        refs.append(
            MediaRef(
                item.file_id,
                getattr(item, "file_name", None) or default_name,
                getattr(item, "mime_type", None) or default_mime,
                getattr(item, "file_size", None),
            )
        )
    return refs


@dataclass
class Inbound:
    """The arguments of ``ctx.interfaces.inbound`` for one Telegram message."""

    path: str
    text: str
    sender_id: str
    sender_name: str
    sender_full_name: str | None
    sender_username: str | None
    chat_username: str | None
    message_id: str
    chat_id: str
    thread_id: str | None
    chat_type: str
    chat_title: str | None
    reply_to: dict[str, Any] | None
    media: list[MediaRef] = field(default_factory=list)
    #: The message was spoken (voice note or video note).
    is_voice_input: bool = False

    def kwargs(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "sender_full_name": self.sender_full_name,
            "sender_username": self.sender_username,
            "chat_username": self.chat_username,
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "chat_type": self.chat_type,
            "chat_title": self.chat_title,
            "reply_to": self.reply_to,
            "is_voice_input": self.is_voice_input,
        }


def inbound_from_message(message: Any) -> Inbound | None:
    """Convert a Telegram message; ``None`` for messages we ignore (no sender,
    service messages, bots talking to themselves)."""
    sender = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    if sender is None or chat is None or getattr(sender, "is_bot", False):
        return None
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    media = media_refs(message)
    if not text and not media:
        return None
    thread = message_thread(message)
    chat_type = "private" if str(getattr(chat, "type", "")) == "private" else "group"
    return Inbound(
        path=build_path(chat.id, thread),
        text=text,
        sender_id=str(sender.id),
        sender_name=getattr(sender, "full_name", None)
        or getattr(sender, "username", None)
        or str(sender.id),
        sender_full_name=getattr(sender, "full_name", None),
        sender_username=getattr(sender, "username", None) or None,
        chat_username=getattr(chat, "username", None) or None,
        message_id=str(message.message_id),
        chat_id=str(chat.id),
        thread_id=str(thread) if thread else None,
        chat_type=chat_type,
        chat_title=getattr(chat, "title", None)
        or getattr(chat, "username", None)
        or None,
        reply_to=reply_info(message),
        media=media,
        is_voice_input=bool(
            getattr(message, "voice", None) or getattr(message, "video_note", None)
        ),
    )


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Cut ``text`` into chunks of at most ``limit`` characters, at line or word
    boundaries when there is one in the second half of the chunk."""
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


def parse_mode_for(chat_id: int | str) -> str | None:
    """Groups get plain text: Markdown would eat ``*action*`` markers that other
    readers of the chat need to see (see the Core's history of this rule)."""
    return None if str(chat_id).startswith("-") else "Markdown"


def usable_reply_id(reply_to: Any, chat_id: int) -> int | None:
    """A reply target, or ``None`` when the value is not a real message id.

    Models often put the chat id or a placeholder here; quoting a message that
    does not exist fails the whole send, so those are dropped."""
    try:
        value = int(str(reply_to))
    except (TypeError, ValueError):
        return None
    return None if value == chat_id or value <= 0 else value
