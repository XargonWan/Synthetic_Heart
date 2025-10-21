"""Message Map Plugin - Persistent mapping between trainer forwarded messages and original targets."""

from __future__ import annotations

import time
from typing import Optional, Tuple, Dict, Any

from core.db import get_conn
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.core_initializer import core_initializer, register_plugin


async def init_message_map_table():
    """Initialize the message_map table if it doesn't exist."""
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            # Check if table exists and has correct structure
            await cur.execute("SHOW TABLES LIKE 'message_map'")
            table_exists = await cur.fetchone()
            
            if table_exists:
                # Check column types
                await cur.execute("DESCRIBE message_map")
                columns = await cur.fetchall()
                chat_id_type = None
                for col in columns:
                    if col[0] == 'chat_id':
                        chat_id_type = col[1]
                        break
                
                # If chat_id is not BIGINT, recreate table
                if chat_id_type and 'bigint' not in chat_id_type.lower():
                    log_warning(f"[message_map] chat_id column type is {chat_id_type}, recreating table")
                    await cur.execute("DROP TABLE message_map")
                    table_exists = None
            
            if not table_exists:
                # Create table with correct structure
                await cur.execute(
                    """
                    CREATE TABLE message_map (
                        trainer_message_id INTEGER PRIMARY KEY,
                        chat_id BIGINT NOT NULL,
                        message_id INTEGER NOT NULL,
                        timestamp REAL
                    )
                    """
                )
                log_info("[message_map] Created message_map table with correct structure")
            else:
                log_debug("[message_map] message_map table already exists with correct structure")
            
            await conn.commit()
            log_debug("[message_map] message_map table initialized")
    except Exception as e:
        log_error(f"[message_map] Failed to initialize message_map table: {e}")
        raise
    finally:
        conn.close()


async def store_message_mapping(trainer_message_id: int, chat_id: int, message_id: int):
    """Store a mapping between trainer message and original message."""
    # Validate inputs: trainer_message_id is required (primary key). If it's
    # None, skip storing the mapping and log a warning instead of raising a
    # DB error (this happens when forwarding isn't available).
    if trainer_message_id is None:
        log_warning(f"[message_map] trainer_message_id is None, skipping store for chat={chat_id}, msg={message_id}")
        return False

    # Log the values being stored for debugging
    log_debug(f"[message_map] Storing mapping: trainer_msg={trainer_message_id} (type: {type(trainer_message_id)}), chat_id={chat_id} (type: {type(chat_id)}), message_id={message_id} (type: {type(message_id)})")

    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                REPLACE INTO message_map 
                (trainer_message_id, chat_id, message_id, timestamp)
                VALUES (%s, %s, %s, %s)
                """,
                (trainer_message_id, chat_id, message_id, time.time())
            )
            await conn.commit()
            log_debug(f"[message_map] Stored mapping: trainer_msg={trainer_message_id} -> chat={chat_id}, msg={message_id}")
            return True
    except Exception as e:
        log_error(f"[message_map] Failed to store mapping: {e}")
        raise
    finally:
        conn.close()


async def get_original_message(trainer_message_id: int) -> Optional[Tuple[int, int]]:
    """Get the original chat_id and message_id for a trainer message."""
    await init_message_map_table()
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT chat_id, message_id 
                FROM message_map 
                WHERE trainer_message_id = %s
                """,
                (trainer_message_id,)
            )
            result = await cur.fetchone()
            if result:
                log_debug(f"[message_map] Found mapping: trainer_msg={trainer_message_id} -> chat={result[0]}, msg={result[1]}")
                return (result[0], result[1])
            else:
                log_debug(f"[message_map] No mapping found for trainer_message_id={trainer_message_id}")
                return None
    except Exception as e:
        log_error(f"[message_map] Failed to get original message: {e}")
        return None
    finally:
        conn.close()


async def cleanup_old_mappings(older_than_hours: int = 24):
    """Remove old message mappings to prevent table bloat."""
    await init_message_map_table()
    cutoff_time = time.time() - (older_than_hours * 3600)
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM message_map 
                WHERE timestamp < %s
                """,
                (cutoff_time,)
            )
            deleted_count = cur.rowcount
            await conn.commit()
            log_info(f"[message_map] Cleaned up {deleted_count} old message mappings")
    except Exception as e:
        log_error(f"[message_map] Failed to cleanup old mappings: {e}")
    finally:
        conn.close()


async def get_mapping_stats() -> Dict[str, int]:
    """Get statistics about message mappings."""
    await init_message_map_table()
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM message_map")
            total_count = (await cur.fetchone())[0]
            
            # Count mappings from last 24 hours
            cutoff_time = time.time() - (24 * 3600)
            await cur.execute(
                "SELECT COUNT(*) FROM message_map WHERE timestamp > %s",
                (cutoff_time,)
            )
            recent_count = (await cur.fetchone())[0]
            
            return {
                "total_mappings": total_count,
                "recent_mappings": recent_count
            }
    except Exception as e:
        log_error(f"[message_map] Failed to get mapping stats: {e}")
        return {"total_mappings": 0, "recent_mappings": 0}
    finally:
        conn.close()


class MessageMapPlugin:
    """Plugin for mapping trainer forwarded messages to original messages."""
    
    display_name = "Message Map"

    def __init__(self):
        register_plugin("message_map", self)
        log_info("[message_map] MessageMapPlugin initialized and registered")

    def get_supported_action_types(self):
        return ["store_message_mapping", "get_original_message", "cleanup_old_mappings", "get_mapping_stats"]

    def get_supported_actions(self):
        return {
            "store_message_mapping": {
                "description": "Store a mapping between trainer message and original message",
                "required_fields": ["trainer_message_id", "chat_id", "message_id"],
                "optional_fields": [],
            },
            "get_original_message": {
                "description": "Get the original chat_id and message_id for a trainer message",
                "required_fields": ["trainer_message_id"],
                "optional_fields": [],
            },
            "cleanup_old_mappings": {
                "description": "Remove old message mappings",
                "required_fields": [],
                "optional_fields": ["older_than_hours"],
            },
            "get_mapping_stats": {
                "description": "Get statistics about message mappings",
                "required_fields": [],
                "optional_fields": [],
            },
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        """Provide detailed prompt instructions for LLM on how to use message mapping actions."""
        if action_name == "store_message_mapping":
            return {
                "description": "Store a mapping between a trainer's forwarded message and the original message it refers to. This enables replying to the correct conversation.",
                "when_to_use": "When the trainer forwards a message and you need to track which original message it corresponds to.",
                "examples": [
                    {
                        "scenario": "Trainer forwards message #123 from chat -100123456, original message #789",
                        "payload": {
                            "trainer_message_id": 123,
                            "chat_id": -100123456,
                            "message_id": 789
                        }
                    }
                ],
                "notes": [
                    "trainer_message_id is the ID of the message the trainer sent",
                    "chat_id is the original chat where the message came from",
                    "message_id is the original message ID in that chat"
                ]
            }
        elif action_name == "get_original_message":
            return {
                "description": "Retrieve the original chat and message ID for a trainer's forwarded message. Use this to reply to the correct conversation.",
                "when_to_use": "When you need to respond to a trainer's forwarded message and want to send the reply to the original chat.",
                "examples": [
                    {
                        "scenario": "Trainer replies to forwarded message #123",
                        "payload": {
                            "trainer_message_id": 123
                        }
                    }
                ],
                "notes": [
                    "Returns the original chat_id and message_id",
                    "Returns null if no mapping exists for that trainer message"
                ]
            }
        return {}

    def execute_action(self, action: dict, context: dict, bot, original_message):
        action_type = action.get("type")
        payload = action.get("payload", {}) or {}
        
        if action_type == "store_message_mapping":
            trainer_message_id = payload.get("trainer_message_id")
            chat_id = payload.get("chat_id")
            message_id = payload.get("message_id")
            if all([trainer_message_id, chat_id, message_id]):
                import asyncio
                asyncio.create_task(store_message_mapping(trainer_message_id, chat_id, message_id))
                
        elif action_type == "get_original_message":
            trainer_message_id = payload.get("trainer_message_id")
            if trainer_message_id:
                import asyncio
                asyncio.create_task(self._send_original_message(bot, original_message, trainer_message_id))
                
        elif action_type == "cleanup_old_mappings":
            older_than_hours = payload.get("older_than_hours", 24)
            import asyncio
            asyncio.create_task(cleanup_old_mappings(older_than_hours))
            
        elif action_type == "get_mapping_stats":
            import asyncio
            asyncio.create_task(self._send_mapping_stats(bot, original_message))

    async def _send_original_message(self, bot, original_message, trainer_message_id):
        """Send the original message info to the user."""
        try:
            result = await get_original_message(trainer_message_id)
            if result:
                chat_id, message_id = result
                response = f"Original message: Chat {chat_id}, Message {message_id}"
            else:
                response = f"No mapping found for trainer message {trainer_message_id}"
            
            await bot.send_message(original_message.chat_id, response)
        except Exception as e:
            log_error(f"[message_map] Failed to send original message info: {e}")

    async def _send_mapping_stats(self, bot, original_message):
        """Send message mapping statistics."""
        try:
            stats = await get_mapping_stats()
            response = f"Message Mapping Stats:\n• Total mappings: {stats['total_mappings']}\n• Recent (24h): {stats['recent_mappings']}"
            await bot.send_message(original_message.chat_id, response)
        except Exception as e:
            log_error(f"[message_map] Failed to send mapping stats: {e}")


PLUGIN_CLASS = MessageMapPlugin
