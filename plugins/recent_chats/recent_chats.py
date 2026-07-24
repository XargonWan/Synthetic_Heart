"""Recent Chats Plugin - Tracks active chat sessions."""

from __future__ import annotations

import time
import json
from typing import Dict, List, Optional
import aiomysql

from core.db import get_conn_ctx
from core.logging_utils import log_info, log_error, log_warning
from core.core_initializer import register_plugin


_recent_chats_table_initialized = False


async def init_recent_chats_table():
    """Initialize the recent_chats table if it doesn't exist."""
    global _recent_chats_table_initialized
    if _recent_chats_table_initialized:
        return
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                # Use VARCHAR(255) for chat_id to support both int and UUID string formats
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recent_chats (
                        chat_id VARCHAR(255) PRIMARY KEY,
                        last_active DOUBLE NOT NULL,
                        metadata TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_last_active (last_active)
                    )
                    """
                )
                await conn.commit()
                _recent_chats_table_initialized = True
        except Exception as e:
            log_error(f"[recent_chats] Failed to initialize table: {e}")
            raise


async def update_chat_activity(chat_id: int | str, metadata: Optional[Dict] = None):
    """Update the last activity time for a chat."""
    await init_recent_chats_table()
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                # Convert chat_id to string to handle both int and UUID formats
                chat_id_str = str(chat_id)
                metadata_json = json.dumps(metadata) if metadata else None
                await cur.execute(
                    """
                    REPLACE INTO recent_chats (chat_id, last_active, metadata)
                    VALUES (%s, %s, %s)
                    """,
                    (chat_id_str, time.time(), metadata_json),
                )
                await conn.commit()
        except Exception as e:
            log_error(
                f"[recent_chats] Failed to update activity for chat {chat_id}: {e}"
            )


async def get_recent_chats(limit: int = 10) -> List[Dict]:
    """Get the most recently active chats."""
    await init_recent_chats_table()
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT chat_id, last_active, metadata, created_at
                    FROM recent_chats
                    ORDER BY last_active DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = await cur.fetchall()
                result = []
                for row in rows:
                    metadata = None
                    if row["metadata"]:
                        try:
                            metadata = json.loads(row["metadata"])
                        except Exception:
                            pass
                    result.append(
                        {
                            "chat_id": row["chat_id"],
                            "last_active": row["last_active"],
                            "metadata": metadata,
                            "created_at": row["created_at"],
                        }
                    )
                return result
        except Exception as e:
            log_error(f"[recent_chats] Failed to get recent chats: {e}")
            return []


async def cleanup_old_chats(older_than_days: int = 30):
    """Remove chats older than specified days."""
    await init_recent_chats_table()
    cutoff_time = time.time() - (older_than_days * 24 * 60 * 60)
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    DELETE FROM recent_chats
                    WHERE last_active < %s
                    """,
                    (cutoff_time,),
                )
                deleted_count = cur.rowcount
                await conn.commit()
                log_info(f"[recent_chats] Cleaned up {deleted_count} old chat records")
        except Exception as e:
            log_error(f"[recent_chats] Failed to cleanup old chats: {e}")


class RecentChatsPlugin:
    """Plugin for tracking recent chat activity."""

    display_name = "Recent Chats"

    def __init__(self):
        register_plugin("recent_chats", self)
        # Expose module-level helpers as instance methods to let core access them via PLUGIN_REGISTRY
        self.update_chat_activity = update_chat_activity
        self.get_recent_chats = get_recent_chats
        log_info("[recent_chats] RecentChatsPlugin initialized and registered")

    def get_supported_action_types(self):
        return ["get_recent_chats"]

    def get_supported_actions(self):
        return {
            # NOTE: update_chat_activity is NOT exposed to LLM - it's called automatically by interfaces
            # when messages are received. This is a mechanical action that doesn't need LLM reasoning.
            "get_recent_chats": {
                "description": "Get the most recently active chats",
                "required_fields": [],
                "optional_fields": ["limit"],
            },
        }

    def execute_action(self, action: dict, context: dict, bot, original_message):
        action_type = action.get("type")
        payload = action.get("payload", {}) or {}

        # NOTE: update_chat_activity is handled automatically by interfaces, not via LLM actions

        if action_type == "get_recent_chats":
            limit = payload.get("limit", 10)
            import asyncio

            asyncio.create_task(
                self._send_recent_chats(context, original_message, limit)
            )

    async def _send_recent_chats(self, context, original_message, limit):
        """Return recent chats list as a message action."""
        try:
            chats = await get_recent_chats(limit)
            if chats:
                text = "Recent chats:\n"
                for chat in chats:
                    text += (
                        f"• Chat {chat['chat_id']}: {time.ctime(chat['last_active'])}\n"
                    )
            else:
                # Fallback: if no record in recent_chats, use global chat_history_cache
                # to avoid misleading "No recent chats found" when global history exists.
                try:
                    from core.chat_history_cache import load_global_chat_history

                    global_msgs = await load_global_chat_history(limit=limit)
                    if global_msgs:
                        text = "Recent chats from global history:\n"
                        for msg in list(global_msgs)[:limit]:
                            ts = msg.get("timestamp", "unknown")
                            i_path = msg.get("interface_path", "unknown")
                            sender = msg.get(
                                "sender_name", msg.get("sender_id", "unknown")
                            )
                            msg_text = msg.get("text", "").strip().replace("\n", " ")
                            if len(msg_text) > 120:
                                msg_text = msg_text[:117] + "..."
                            text += f"• [{ts}] {i_path} {sender}: {msg_text}\n"
                    else:
                        text = "No recent chats found."
                except Exception as db_e:
                    log_error(
                        f"[recent_chats] Failed to load global fallback history: {db_e}"
                    )
                    text = "No recent chats found."
        except Exception as e:
            log_error(f"[recent_chats] Failed to send recent chats: {e}")
            text = f"❌ Failed to get recent chats: {e}"
        return self._build_message_action(text, context, original_message)

    @staticmethod
    def _build_message_action(
        text: str, context: dict, original_message
    ) -> dict | None:
        """Build a message action dict for interface-agnostic delivery."""
        interface_path = None
        if context and isinstance(context, dict):
            interface_path = context.get("interface_path")
        if not interface_path and original_message:
            interface_path = getattr(original_message, "interface_path", None)
        if not interface_path:
            log_warning(
                "[recent_chats] Cannot build message action: no interface_path available"
            )
            return None

        interface_name = interface_path.split("/")[0] if interface_path else None
        if not interface_name:
            return None

        action_type = f"message_{interface_name}"
        return {
            "type": action_type,
            "payload": {
                "text": text,
                "interface_path": interface_path,
            },
        }


PLUGIN_CLASS = RecentChatsPlugin
