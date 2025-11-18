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
                        interface_path VARCHAR(512) NOT NULL,
                        sender_name VARCHAR(255),
                        sender_id VARCHAR(255),
                        message_text LONGTEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_interface_path (interface_path),
                        INDEX idx_timestamp (timestamp),
                        UNIQUE KEY uniq_message (interface_path, timestamp)
                    )
                """)
                log_debug("[chat_history_cache] chat_history_cache table initialized")
    except Exception as e:
        log_error(f"[chat_history_cache] Failed to create table: {e}")


async def save_chat_message(
    interface_path: str,
    message_text: str,
    sender_name: str = None,
    sender_id: str = None
) -> None:
    """Save a message to the chat history cache.
    
    Args:
        interface_path: The interface path (e.g., telegram_bot/123456/2)
        message_text: The message text
        sender_name: Optional sender name
        sender_id: Optional sender ID
    """
    if not interface_path or not message_text:
        return
    
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Insert message
                await cur.execute("""
                    INSERT INTO chat_history_cache 
                    (interface_path, sender_name, sender_id, message_text)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE timestamp=CURRENT_TIMESTAMP
                """, (interface_path, sender_name, sender_id, message_text))
                
                # Clean up old messages beyond CHAT_HISTORY_LIMIT
                await cur.execute("""
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
                """, (interface_path, interface_path, CHAT_HISTORY_LIMIT))
                
                log_debug(f"[chat_history_cache] Saved message for interface_path {interface_path}")
    except Exception as e:
        log_debug(f"[chat_history_cache] Failed to save message: {e}")


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
                # Load messages in chronological order
                await cur.execute("""
                    SELECT sender_name, sender_id, message_text, timestamp, interface_path
                    FROM chat_history_cache
                    WHERE interface_path = %s
                    ORDER BY timestamp ASC
                    LIMIT %s
                """, (interface_path, CHAT_HISTORY_LIMIT))
                
                rows = await cur.fetchall()
                
                # Convert rows to message objects
                messages = deque()
                for row in rows:
                    try:
                        sender_name, sender_id, message_text, timestamp, ipath = row
                        # Store as dict for flexibility
                        msg = {
                            "sender_name": sender_name,
                            "sender_id": sender_id,
                            "text": message_text,
                            "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp),
                            "interface_path": ipath,
                        }
                        messages.append(msg)
                    except Exception as e:
                        log_debug(f"[chat_history_cache] Error parsing message row: {e}")
                
                log_debug(f"[chat_history_cache] Loaded {len(messages)} messages for interface_path {interface_path}")
                return messages
                
    except Exception as e:
        log_error(f"[chat_history_cache] Failed to load chat history for {interface_path}: {e}")
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
                await cur.execute("DELETE FROM chat_history_cache WHERE interface_path = %s", (interface_path,))
                log_info(f"[chat_history_cache] Cleared chat history for {interface_path}")
    except Exception as e:
        log_error(f"[chat_history_cache] Failed to clear chat history for {interface_path}: {e}")


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
                
                # Number of unique interface paths
                await cur.execute("SELECT COUNT(DISTINCT interface_path) FROM chat_history_cache")
                unique_paths = (await cur.fetchone())[0]
                
                # Oldest and newest messages
                await cur.execute("""
                    SELECT MIN(timestamp), MAX(timestamp) FROM chat_history_cache
                """)
                result = await cur.fetchone()
                oldest, newest = result if result else (None, None)
                
                return {
                    "total_messages": total_messages,
                    "unique_interface_paths": unique_paths,
                    "oldest_message": oldest.isoformat() if oldest else None,
                    "newest_message": newest.isoformat() if newest else None,
                    "history_limit": CHAT_HISTORY_LIMIT,
                }
    except Exception as e:
        log_error(f"[chat_history_cache] Failed to get cache stats: {e}")
        return {}
