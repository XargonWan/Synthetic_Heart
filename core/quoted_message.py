"""The text shown to the model for the message a user is replying to.

Every interface hands the core a ``reply_to_message`` carrying the quoted message's
text or caption. A reply to a photo, sticker or voice note carries none, and the
quote used to be dropped, so the model answered "that message" without knowing what
it was. Here a text-less quote becomes what Iris saw in it (stored by
``core.media_descriptions`` when the media was described), or at least what kind of
media it was. The framing is static English and the kind comes from attribute reads
on the wrapper: no word lists, no language detection.
"""

from __future__ import annotations

from typing import Any

from core import media_descriptions

# Long enough for any chat message; the model is told when it is cut.
QUOTE_MAX_CHARS = 2000

_MEDIA_ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("photo", "photo"),
    ("sticker", "sticker"),
    ("voice", "voice message"),
    ("video_note", "video message"),
    ("video", "video"),
    ("animation", "animation"),
    ("audio", "audio file"),
    ("document", "file"),
    ("attachments", "attachment"),
)


def media_kind(reply: Any) -> str | None:
    """What kind of media the quoted message carries, from the wrapper's attributes."""
    for attribute, label in _MEDIA_ATTRIBUTES:
        value = getattr(reply, attribute, None)
        if value:
            return label
    return None


def clip(text: str, limit: int = QUOTE_MAX_CHARS) -> str:
    """``text`` cut at ``limit`` characters on a word boundary, with an ellipsis."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind(" ")
    if boundary > limit * 0.8:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


async def quote_text(reply: Any, interface_path: str | None) -> str:
    """The quote to show for ``reply``; ``""`` when there is nothing to show."""
    text = str(
        getattr(reply, "text", None) or getattr(reply, "caption", None) or ""
    ).strip()
    if text and text != "[Non-text content]":
        return clip(text)
    kind = media_kind(reply)
    described = await media_descriptions.lookup(
        interface_path, getattr(reply, "message_id", None)
    )
    if described:
        return clip(f"[{kind or 'media'}; what you saw in it: {described}]")
    if kind:
        return f"[{kind}; its content is not available]"
    return ""
