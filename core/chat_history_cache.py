# core/chat_history_cache.py

"""
Chat history persistence module for surviving container restarts.

This module provides functions to save and load chat history from the database,
ensuring that the chat context is preserved across restarts. The history is
cached in a database table and limited to the configured CHAT_HISTORY_LIMIT.
"""

import asyncio

from datetime import datetime
from collections import deque
from typing import Any
from core.db import get_conn_ctx
from core.logging_utils import log_debug, log_error, log_info
from core.config_manager import config_registry


def _get_history_limit(default: int = 50) -> int:
    """Return the unified history limit.

    Prefers the new global `CONTEXT_VERBOSITY` setting; falls back to legacy
    settings (`CHAT_HISTORY`, `CHAT_HISTORY_LIMIT`) for backward compatibility.
    """
    for key in ("CONTEXT_VERBOSITY", "CHAT_HISTORY", "CHAT_HISTORY_LIMIT"):
        try:
            val = config_registry.get_value(key, None, value_type=int)
            if val is not None:
                return max(1, int(val))
        except Exception:
            continue
    return max(1, int(default))


async def init_chat_history_table() -> None:
    """Create the chat_history_cache table if it doesn't exist."""
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS chat_history_cache (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        interface_path VARCHAR(512) NOT NULL,
                        sender_name VARCHAR(255),
                        sender_id VARCHAR(255),
                        message_text LONGTEXT NOT NULL,
                        metadata JSON DEFAULT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_interface_path (interface_path),
                        INDEX idx_timestamp (timestamp),
                        UNIQUE KEY uniq_message (interface_path, timestamp)
                    )
                """)
                # Migration: add metadata column for pre-existing tables.
                try:
                    await cur.execute(
                        "ALTER TABLE chat_history_cache "
                        "ADD COLUMN metadata JSON DEFAULT NULL"
                    )
                    log_debug("[chat_history_cache] Added metadata column")
                except Exception:
                    # Column already exists — ignore.
                    pass
                log_debug("[chat_history_cache] chat_history_cache table initialized")
    except Exception as e:
        log_error(f"[chat_history_cache] Failed to create table: {e}")


async def save_chat_message(
    interface_path: str,
    message_text: str,
    sender_name: str | None = None,
    sender_id: str | None = None,
    timestamp: datetime | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Save a message to the chat history cache.

    Args:
        interface_path: The interface path (e.g., telegram_bot/123456/2)
        message_text: The message text
        sender_name: Optional sender name
        sender_id: Optional sender ID
        timestamp: Optional message timestamp (datetime object or ISO string)
                   If provided, will be converted to UTC before storing.
                   If tzinfo is None, assumes local time and converts to UTC.
        metadata: Optional dict of extra metadata (e.g. ``{"tts_url": ...}``).
    """
    if not interface_path or not message_text:
        return False

    try:
        from datetime import timezone

        # Parse timestamp if it's a string
        if timestamp and isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except Exception:
                timestamp = None

        # Convert timestamp to UTC for storage; only proceed when we have a datetime
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                # If no timezone info, assume it's UTC (already passed as UTC from interfaces)
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            # Convert to UTC if in different timezone
            timestamp = timestamp.astimezone(timezone.utc)

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                history_limit = _get_history_limit(50)

                # Deduplication: Check for identical message text within last 5 seconds for this interface
                # This prevents double-logging from different pipeline stages (e.g. generation vs dispatch)
                try:
                    await cur.execute(
                        """
                        SELECT id FROM chat_history_cache 
                        WHERE interface_path = %s 
                        AND message_text = %s 
                        AND timestamp > DATE_SUB(UTC_TIMESTAMP(), INTERVAL 5 SECOND)
                        LIMIT 1
                        """,
                        (interface_path, message_text),
                    )
                    if await cur.fetchone():
                        log_debug(
                            f"[chat_history_cache] Skipping duplicate message for {interface_path}: {message_text[:30]}..."
                        )
                        return True
                except Exception as e:
                    log_debug(f"[chat_history_cache] Deduplication check failed: {e}")

                # Serialise metadata dict to JSON for storage.
                import json as _json

                metadata_json = _json.dumps(metadata) if metadata else None

                # Insert message with timestamp (always in UTC)
                if timestamp:
                    await cur.execute(
                        """
                        INSERT INTO chat_history_cache 
                        (interface_path, sender_name, sender_id, message_text, metadata, timestamp)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE timestamp=VALUES(timestamp), metadata=VALUES(metadata)
                    """,
                        (
                            interface_path,
                            sender_name,
                            sender_id,
                            message_text,
                            metadata_json,
                            timestamp,
                        ),
                    )
                else:
                    await cur.execute(
                        """
                        INSERT INTO chat_history_cache 
                        (interface_path, sender_name, sender_id, message_text, metadata, timestamp)
                        VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP())
                        ON DUPLICATE KEY UPDATE timestamp=UTC_TIMESTAMP(), metadata=VALUES(metadata)
                    """,
                        (
                            interface_path,
                            sender_name,
                            sender_id,
                            message_text,
                            metadata_json,
                        ),
                    )

                # Clean up old messages beyond CHAT_HISTORY_LIMIT
                await cur.execute(
                    """
                    DELETE FROM chat_history_cache
                    WHERE interface_path = %s
                    AND id NOT IN (
                        SELECT id FROM (
                            SELECT id FROM chat_history_cache
                            WHERE interface_path = %s
                            ORDER BY timestamp DESC
                            LIMIT %s
                        ) AS temp
                    )
                """,
                    (interface_path, interface_path, history_limit),
                )

                log_debug(
                    f"[chat_history_cache] Saved message for interface_path {interface_path}, sender={sender_name}, timestamp={timestamp}"
                )

                # Notify live sessions about this new message, unless it's the
                # live-sync path itself (to avoid echo loops).
                if not interface_path.startswith("discord_live_"):
                    try:
                        from core.live_session_manager import LiveSessionManager

                        mgr = LiveSessionManager._instance  # type: ignore[attr-defined]
                        if not mgr:
                            # no live manager has been created yet, nothing to do
                            pass
                        else:
                            context_text = (
                                f"[context update from {interface_path}] {message_text}"
                            )
                            for gid in mgr.get_active_sessions():
                                asyncio.create_task(
                                    mgr.send_context_update(gid, context_text)
                                )
                    except Exception as e:
                        log_debug(
                            f"[chat_history_cache] live context update failed: {e}"
                        )

                return True
    except Exception as e:
        log_debug(f"[chat_history_cache] Failed to save message: {e}")
        return False


async def load_chat_history(interface_path: str) -> deque:
    """Load chat history from cache for a specific interface path.

    Args:
        interface_path: The interface path (e.g., telegram_bot/123456/2)

    Returns:
        deque of message objects in chronological order
    """
    if not interface_path:
        return deque()

    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                history_limit = _get_history_limit(10)
                # Load messages in chronological order
                await cur.execute(
                    """
                    SELECT sender_name, sender_id, message_text, timestamp, interface_path, metadata
                    FROM chat_history_cache
                    WHERE interface_path = %s
                    ORDER BY timestamp ASC, id ASC
                    LIMIT %s
                """,
                    (interface_path, history_limit),
                )

                rows = await cur.fetchall()

                # Convert rows to message objects
                import json as _json

                messages = deque()
                for row in rows:
                    try:
                        (
                            sender_name,
                            sender_id,
                            message_text,
                            timestamp,
                            ipath,
                            raw_meta,
                        ) = row
                        # Store as dict for flexibility
                        msg: dict[str, Any] = {
                            "sender_name": sender_name,
                            "sender_id": sender_id,
                            "text": message_text,
                            "timestamp": timestamp.isoformat()
                            if isinstance(timestamp, datetime)
                            else str(timestamp),
                            "interface_path": ipath,
                        }
                        # Deserialise metadata JSON.
                        if raw_meta:
                            try:
                                parsed = (
                                    _json.loads(raw_meta)
                                    if isinstance(raw_meta, str)
                                    else raw_meta
                                )
                                if isinstance(parsed, dict):
                                    msg["metadata"] = parsed
                            except Exception:
                                pass
                        messages.append(msg)
                    except Exception as e:
                        log_debug(
                            f"[chat_history_cache] Error parsing message row: {e}"
                        )

                log_debug(
                    f"[chat_history_cache] Loaded {len(messages)} messages for interface_path {interface_path}"
                )
                for msg in messages:
                    log_debug(
                        f"[chat_history_cache]   - {msg.get('timestamp')}: {msg.get('sender_name')} [{msg.get('sender_id')}]: {msg.get('text')[:50]}..."
                    )
                return messages

    except Exception as e:
        log_error(
            f"[chat_history_cache] Failed to load chat history for {interface_path}: {e}"
        )
        return deque()


async def load_global_chat_history(limit: int = 10) -> deque:
    """Load global chat history from cache across all interface paths.

    Args:
        limit: Max number of messages to retrieve

    Returns:
        deque of message objects in chronological order
    """
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT sender_name, sender_id, message_text, timestamp, interface_path
                    FROM chat_history_cache
                    ORDER BY timestamp DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = list(await cur.fetchall())
                rows.reverse()

                messages = deque()
                for row in rows:
                    try:
                        sender_name, sender_id, message_text, timestamp, ipath = row
                        msg = {
                            "sender_name": sender_name,
                            "sender_id": sender_id,
                            "text": message_text,
                            "timestamp": timestamp.isoformat()
                            if isinstance(timestamp, datetime)
                            else str(timestamp),
                            "interface_path": ipath,
                        }
                        messages.append(msg)
                    except Exception as e:
                        log_debug(
                            f"[chat_history_cache] Error parsing global message row: {e}"
                        )

                log_debug(
                    f"[chat_history_cache] Loaded {len(messages)} global messages"
                )
                return messages
    except Exception as e:
        log_error(f"[chat_history_cache] Failed to load global chat history: {e}")
        return deque()


async def load_global_chat_history_since(
    since: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Load recent global chat history excluding live-session paths.

    Returns messages from all interfaces *except* ``discord_live_*`` paths
    (those are already replicated into the live session context).  The result
    is ordered chronologically (oldest first) and optionally filtered to
    messages newer than ``since``.

    Args:
        since: Optional ISO timestamp string. Only messages strictly newer than
            this value are returned.
        limit: Maximum number of messages to retrieve.

    Returns:
        List of message dicts with keys: sender_name, sender_id, text,
        timestamp, interface_path.
    """
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                query = """
                    SELECT sender_name, sender_id, message_text, timestamp, interface_path
                    FROM chat_history_cache
                    WHERE interface_path NOT LIKE 'discord_live_%%'
                """
                params: list[Any] = []
                if since:
                    query += "\n AND timestamp > %s"
                    params.append(since)
                query += "\n ORDER BY timestamp ASC, id ASC\n LIMIT %s"
                params.append(limit)

                await cur.execute(query, tuple(params))
                rows = await cur.fetchall()

                messages: list[dict[str, Any]] = []
                for row in rows:
                    try:
                        sender_name, sender_id, message_text, timestamp, ipath = row
                        messages.append(
                            {
                                "sender_name": sender_name,
                                "sender_id": sender_id,
                                "text": message_text,
                                "timestamp": (
                                    timestamp.isoformat()
                                    if hasattr(timestamp, "isoformat")
                                    else str(timestamp)
                                ),
                                "interface_path": ipath,
                            }
                        )
                    except Exception as e:
                        log_debug(
                            f"[chat_history_cache] Error parsing global since-row: {e}"
                        )

                log_debug(
                    f"[chat_history_cache] load_global_chat_history_since: "
                    f"{len(messages)} messages (since={since})"
                )
                return messages
    except Exception as e:
        log_error(
            f"[chat_history_cache] Failed to load global history since {since}: {e}"
        )
        return []


async def load_chat_history_for_guild(
    guild_id: int,
    since: str | None = None,
    limit: int = 100,
) -> deque:
    """Load chat history for all interface paths belonging to a Discord guild.

    This is used by the live sync feature to fetch *text* messages posted in
    the guild, regardless of channel. The returned messages are ordered
    chronologically (oldest first) and share the same format as
    :func:`load_chat_history`.

    Args:
        guild_id: Discord guild ID
        since: Optional ISO timestamp string. When provided, only messages
            newer than ``since`` are returned.
        limit: Maximum number of messages to retrieve.
    """
    if guild_id is None:
        return deque()

    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Build the base query
                query = """
                    SELECT sender_name, sender_id, message_text, timestamp, interface_path
                    FROM chat_history_cache
                    WHERE interface_path LIKE %s
                """
                params: list[Any] = [f"discord_{guild_id}_%"]
                if since:
                    query += "\n AND timestamp > %s"
                    params.append(since)
                query += "\n ORDER BY timestamp ASC, id ASC\n LIMIT %s"
                params.append(limit)

                await cur.execute(query, tuple(params))
                rows = await cur.fetchall()

                messages = deque()
                for row in rows:
                    try:
                        sender_name, sender_id, message_text, timestamp, ipath = row
                        msg = {
                            "sender_name": sender_name,
                            "sender_id": sender_id,
                            "text": message_text,
                            "timestamp": timestamp.isoformat()
                            if hasattr(timestamp, "isoformat")
                            else str(timestamp),
                            "interface_path": ipath,
                        }
                        messages.append(msg)
                    except Exception as e:
                        log_debug(
                            f"[chat_history_cache] Error parsing guild message row: {e}"
                        )

                log_debug(
                    f"[chat_history_cache] Loaded {len(messages)} messages for guild {guild_id} since={since} limit={limit}"
                )
                return messages
    except Exception as e:
        log_error(
            f"[chat_history_cache] Failed to load history for guild {guild_id}: {e}"
        )
        return deque()


async def clear_chat_history(interface_path: str) -> None:
    """Clear all messages for a specific interface path.

    Args:
        interface_path: The interface path to clear (e.g., telegram_bot/123456/2)
    """
    if not interface_path:
        return

    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM chat_history_cache WHERE interface_path = %s",
                    (interface_path,),
                )
                log_info(
                    f"[chat_history_cache] Cleared chat history for {interface_path}"
                )
    except Exception as e:
        log_error(
            f"[chat_history_cache] Failed to clear chat history for {interface_path}: {e}"
        )


async def get_cache_stats() -> dict:
    """Get statistics about the chat history cache.

    Returns:
        dict with cache statistics
    """
    try:
        history_limit = _get_history_limit(10)
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Total messages in cache
                await cur.execute("SELECT COUNT(*) FROM chat_history_cache")
                total_messages = (await cur.fetchone())[0]

                # Number of unique interface paths
                await cur.execute(
                    "SELECT COUNT(DISTINCT interface_path) FROM chat_history_cache"
                )
                unique_paths = (await cur.fetchone())[0]

                # Oldest and newest messages
                await cur.execute("""
                    SELECT MIN(timestamp), MAX(timestamp) FROM chat_history_cache
                """)
                result = await cur.fetchone()
                oldest, newest = result if result else (None, None)

                return {
                    "total_messages": total_messages,
                    "unique_paths": unique_paths,
                    "oldest": oldest.isoformat() if oldest else None,
                    "newest": newest.isoformat() if newest else None,
                    "history_limit": history_limit,
                }
    except Exception as e:
        log_error(f"[chat_history_cache] Failed to get cache stats: {e}")
        return {}


async def get_last_message(interface_path: str):
    """Return the last message for an interface_path, preferring in-memory context and falling back to persisted cache.

    Returns a dict with keys similar to load_chat_history rows (sender_name, sender_id, text, timestamp, interface_path) or None.
    """
    if not interface_path:
        return None

    try:
        # Check in-memory context first
        from core.chat_context_manager import get_or_create_chat_context

        ctx = get_or_create_chat_context(interface_path)
        # If context has entries, prefer the last one
        if ctx and len(ctx) > 0:
            last = ctx[-1]
            # If it's already a dict from cached history, return as-is
            if isinstance(last, dict):
                return last
            # Otherwise try to extract common fields from message-like object
            try:
                text = getattr(last, "text", None) or (
                    last.get("text") if isinstance(last, dict) else None
                )
                user = getattr(last, "from_user", None) or (
                    last.get("from_user") if isinstance(last, dict) else None
                )
                sender_name = None
                sender_id = None
                if user:
                    sender_name = getattr(user, "username", None) or getattr(
                        user, "full_name", None
                    )
                    sender_id = getattr(user, "id", None)
                # Fallback fields
                if not sender_name and isinstance(last, dict):
                    sender_name = last.get("sender_name") or last.get("username")
                    sender_id = (
                        sender_id or last.get("sender_id") or last.get("user_id")
                    )

                return {
                    "sender_name": sender_name,
                    "sender_id": sender_id,
                    "text": text,
                    "timestamp": getattr(last, "timestamp", None)
                    or (last.get("timestamp") if isinstance(last, dict) else None),
                    "interface_path": interface_path,
                }
            except Exception:
                pass
    except Exception:
        pass

    # Fallback: persisted cache
    try:
        hist = await load_chat_history(interface_path)
        if hist and len(hist) > 0:
            return list(hist)[-1]
    except Exception:
        pass

    return None
