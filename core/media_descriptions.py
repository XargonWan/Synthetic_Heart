"""What Iris saw in a photo, kept by message so a later reply to it can quote it.

A photo sent without a caption leaves no text in the chat history, so when someone
replies to it the quoted message used to be empty and the model got no quote at
all. When Iris describes a photo, the description is stored here under the id of
the message that carried it (per chat); the quote of a reply to that message is
then the description. Rows older than a month are removed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.db import get_conn_ctx
from core.logging_utils import log_debug

RETENTION_DAYS = 30
_table_ready = False


def chat_key(interface_path: str | None) -> str:
    """The chat a message id belongs to: the first two segments of the interface path.

    Same convention as the chat-history lookup: a Telegram reply in a topic has a
    thread suffix the original message does not, and message ids are only unique
    inside a chat (Telegram) or a guild (Discord).
    """
    return "/".join(str(interface_path or "").split("/")[:2])


async def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS media_descriptions (
                    chat_key VARCHAR(512) NOT NULL,
                    message_id VARCHAR(128) NOT NULL,
                    description LONGTEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_key, message_id)
                )
                """
            )
    _table_ready = True


async def remember(
    interface_path: str | None, message_id: Any, description: str | None
) -> bool:
    """Store the description of the media carried by ``message_id``; ``True`` when stored."""
    text = str(description or "").strip()
    if not interface_path or message_id in (None, "") or not text:
        return False
    try:
        await _ensure_table()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM media_descriptions WHERE created_at < %s",
                    (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS),),
                )
                await cur.execute(
                    "DELETE FROM media_descriptions WHERE chat_key = %s AND message_id = %s",
                    (chat_key(interface_path), str(message_id)),
                )
                await cur.execute(
                    "INSERT INTO media_descriptions (chat_key, message_id, description) "
                    "VALUES (%s, %s, %s)",
                    (chat_key(interface_path), str(message_id), text),
                )
        return True
    except Exception as exc:
        log_debug(f"[media_descriptions] could not store a description: {exc}")
        return False


async def lookup(interface_path: str | None, message_id: Any) -> str | None:
    """The stored description of the media carried by ``message_id``, or ``None``."""
    if not interface_path or message_id in (None, ""):
        return None
    try:
        await _ensure_table()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT description FROM media_descriptions "
                    "WHERE chat_key = %s AND message_id = %s",
                    (chat_key(interface_path), str(message_id)),
                )
                row = await cur.fetchone()
    except Exception as exc:
        log_debug(f"[media_descriptions] lookup failed: {exc}")
        return None
    if not row:
        return None
    value = row[0] if not isinstance(row, dict) else next(iter(row.values()), None)
    return str(value).strip() or None if value else None
