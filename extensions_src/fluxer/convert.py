"""Pure helpers for the Fluxer extension (no network, no Core). Standard library only."""

from __future__ import annotations

from typing import Any

INTERFACE_ID = "fluxer_bot"


def resolve_api_base(raw_base: str, version: Any) -> str:
    """The REST base URL with the ``{v}`` placeholder replaced."""
    base = str(raw_base or "").strip().rstrip("/")
    try:
        ver = str(int(version))
    except (TypeError, ValueError):
        ver = "1"
    return base.replace("{v}", ver)


def rest_auth_header(token: str) -> str:
    """The REST ``Authorization`` value: bots need ``Bot ``, user tokens (``flx_``) go
    verbatim, and a prefix the operator already typed is kept."""
    tok = str(token or "").strip()
    if not tok:
        return ""
    if tok.lower().startswith("bot ") or tok.startswith("flx_"):
        return tok
    return f"Bot {tok}"


def gateway_token(token: str) -> str:
    """The raw token for IDENTIFY/RESUME: the gateway wants no ``Bot `` prefix."""
    tok = str(token or "").strip()
    return tok[4:].strip() if tok.lower().startswith("bot ") else tok


def build_path(guild_id: Any, channel_id: Any) -> str:
    """``fluxer_bot/<guild>/<channel>`` in a server, ``fluxer_bot/<channel>`` in a DM."""
    if guild_id is not None:
        return f"{INTERFACE_ID}/{guild_id}/{channel_id}"
    return f"{INTERFACE_ID}/{channel_id}"


def channel_of(path: str | None) -> str | None:
    """The channel id of a path (the last level; a guild is never the destination)."""
    if not path or not isinstance(path, str):
        return None
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] != INTERFACE_ID or len(parts) not in (2, 3):
        return None
    channel = parts[-1]
    return channel if channel.isdigit() else None


def usable_reply_id(reply_to: Any, channel: str) -> str | None:
    """A message id to quote, or ``None``: a Fluxer id is numeric, and the channel's own
    id is not a message."""
    text = str(reply_to).strip() if reply_to is not None else ""
    return text if text.isdigit() and text != str(channel) else None


def split_text(text: str, limit: int = 2000) -> list[str]:
    """Chunks of at most ``limit`` characters, cut at a line or word end when one is in
    the second half of the chunk."""
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


def author_name(author: dict[str, Any]) -> str:
    return str(author.get("username") or author.get("global_name") or "unknown")


def reply_block(message: dict[str, Any]) -> dict[str, Any] | None:
    """What the person replied to, from the gateway's ``referenced_message``."""
    ref = message.get("referenced_message")
    if isinstance(ref, dict) and ref.get("id") is not None:
        who = ref.get("author") or {}
        info: dict[str, Any] = {
            "sender_name": author_name(who),
            "text": str(ref.get("content") or ""),
            "message_id": str(ref["id"]),
        }
        if who.get("id") is not None:
            info["sender_id"] = str(who["id"])
        return info
    return None
