# core/chat_history_cache.py

"""
Chat history persistence module for surviving container restarts.

This module provides functions to save and load chat history from the database,
ensuring that the chat context is preserved across restarts. The history is
cached in a database table and limited to the configured CHAT_HISTORY_LIMIT.
"""

from datetime import datetime
import json
from collections import deque
from core.db import get_conn_ctx
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.config_manager import config_registry

# Get chat history limit from config
try:
    CHAT_HISTORY_LIMIT = config_registry.get_value('CHAT_HISTORY', 10, value_type=int)
except Exception:
    CHAT_HISTORY_LIMIT = 10


async def init_chat_history_table() -> None:
    """Create the chat_history_cache table if it doesn't exist."""
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS chat_history_cache (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        chat_id VARCHAR(255) NOT NULL,
                        interface VARCHAR(100),
                        thread_id VARCHAR(255),
                        sender_name VARCHAR(255),
                        sender_id VARCHAR(255),
                        message_text LONGTEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_chat_id (chat_id),
                        INDEX idx_timestamp (timestamp),
                        UNIQUE KEY uniq_message (chat_id, thread_id, timestamp)
                    )
                """)
                log_debug("[chat_history_cache] chat_history_cache table initialized")
    except Exception as e:
        log_error(f"[chat_history_cache] Failed to create table: {e}")


async def save_chat_message(
    chat_id: str,
    message_text: str,
    sender_name: str = None,
    sender_id: str = None,
    interface: str = None,
    thread_id: str = None
) -> None:
    """Save a message to the chat history cache.
    
    Args:
        chat_id: The chat ID
        message_text: The message text
        sender_name: Optional sender name
        sender_id: Optional sender ID
        interface: Optional interface name
        thread_id: Optional thread ID
    """
    if not chat_id or not message_text:
        return
    
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Insert message
                await cur.execute("""
                    INSERT INTO chat_history_cache 
                    (chat_id, interface, thread_id, sender_name, sender_id, message_text)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE timestamp=CURRENT_TIMESTAMP
                """, (chat_id, interface, thread_id, sender_name, sender_id, message_text))
                
                # Clean up old messages beyond CHAT_HISTORY_LIMIT
                await cur.execute("""
                    DELETE FROM chat_history_cache
                    WHERE chat_id = %s
                    AND id NOT IN (
                        SELECT id FROM (
                            SELECT id FROM chat_history_cache
                            WHERE chat_id = %s
                            ORDER BY timestamp DESC
                            LIMIT %s
                        ) AS temp
                    )
                """, (chat_id, chat_id, CHAT_HISTORY_LIMIT))
                
                log_debug(f"[chat_history_cache] Saved message for chat {chat_id}")
    except Exception as e:
        log_debug(f"[chat_history_cache] Failed to save message: {e}")


async def load_chat_history(chat_id: str) -> deque:
    """Load chat history from cache for a specific chat.
    
    Args:
        chat_id: The chat ID
        
    Returns:
        deque of message objects in chronological order
    """
    if not chat_id:
        return deque()
    
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Load messages in chronological order
                await cur.execute("""
                    SELECT sender_name, sender_id, message_text, timestamp, interface, thread_id
                    FROM chat_history_cache
                    WHERE chat_id = %s
                    ORDER BY timestamp ASC
                    LIMIT %s
                """, (chat_id, CHAT_HISTORY_LIMIT))
                
                rows = await cur.fetchall()
                
                # Convert rows to message objects
                messages = deque()
                for row in rows:
                    try:
                        sender_name, sender_id, message_text, timestamp, interface, thread_id = row
                        # Store as dict for flexibility
                        msg = {
                            "sender_name": sender_name,
                            "sender_id": sender_id,
                            "text": message_text,
                            "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp),
                            "interface": interface,
                            "thread_id": thread_id,
                        }
                        messages.append(msg)
                    except Exception as e:
                        log_debug(f"[chat_history_cache] Error parsing message row: {e}")
                
                log_debug(f"[chat_history_cache] Loaded {len(messages)} messages for chat {chat_id}")
                return messages
                
    except Exception as e:
        log_error(f"[chat_history_cache] Failed to load chat history for {chat_id}: {e}")
        return deque()


async def clear_chat_history(chat_id: str) -> None:
    """Clear all messages for a specific chat.
    
    Args:
        chat_id: The chat ID to clear
    """
    if not chat_id:
        return
    
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM chat_history_cache WHERE chat_id = %s", (chat_id,))
                log_info(f"[chat_history_cache] Cleared chat history for {chat_id}")
    except Exception as e:
        log_error(f"[chat_history_cache] Failed to clear chat history for {chat_id}: {e}")


async def get_cache_stats() -> dict:
    """Get statistics about the chat history cache.
    
    Returns:
        dict with cache statistics
    """
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Total messages in cache
                await cur.execute("SELECT COUNT(*) FROM chat_history_cache")
                total_messages = (await cur.fetchone())[0]
                
                # Number of unique chats
                await cur.execute("SELECT COUNT(DISTINCT chat_id) FROM chat_history_cache")
                unique_chats = (await cur.fetchone())[0]
                
                # Oldest and newest messages
                await cur.execute("""
                    SELECT MIN(timestamp), MAX(timestamp) FROM chat_history_cache
                """)
                result = await cur.fetchone()
                oldest, newest = result if result else (None, None)
                
                return {
                    "total_messages": total_messages,
                    "unique_chats": unique_chats,
                    "oldest_message": oldest.isoformat() if oldest else None,
                    "newest_message": newest.isoformat() if newest else None,
                    "history_limit": CHAT_HISTORY_LIMIT,
                }
    except Exception as e:
        log_error(f"[chat_history_cache] Failed to get cache stats: {e}")
        return {}
