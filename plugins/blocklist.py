"""Blocklist Plugin - User blocking and management functionality."""

from __future__ import annotations

from typing import List, Dict
import aiomysql

from core.db import get_conn_ctx
from core.logging_utils import log_info, log_warning, log_error
from core.core_initializer import register_plugin


async def init_blocklist_table():
    """Initialize the blocklist table if it doesn't exist."""
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS blocklist (
                        user_id BIGINT PRIMARY KEY,
                        reason TEXT,
                        blocked_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                await conn.commit()
        except Exception as e:
            log_error(f"[blocklist] Failed to initialize table: {e}")
            raise


async def block_user(user_id: int, reason: str = None):
    """Block a user with optional reason."""
    await init_blocklist_table()
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    REPLACE INTO blocklist (user_id, reason, blocked_at)
                    VALUES (%s, %s, NOW())
                    """,
                    (user_id, reason),
                )
                await conn.commit()
                log_info(f"[blocklist] Blocked user {user_id}: {reason}")
        except Exception as e:
            log_error(f"[blocklist] Failed to block user {user_id}: {e}")
            raise


async def unblock_user(user_id: int):
    """Unblock a user."""
    await init_blocklist_table()
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    DELETE FROM blocklist WHERE user_id = %s
                    """,
                    (user_id,),
                )
                deleted = cur.rowcount
                await conn.commit()
                if deleted > 0:
                    log_info(f"[blocklist] Unblocked user {user_id}")
                    return True
                else:
                    log_warning(f"[blocklist] User {user_id} was not blocked")
                    return False
        except Exception as e:
            log_error(f"[blocklist] Failed to unblock user {user_id}: {e}")
            raise


async def is_user_blocked(user_id: int) -> bool:
    """Check if a user is blocked."""
    try:
        await init_blocklist_table()
    except Exception as e:
        log_warning(
            f"[blocklist] Blocklist storage unavailable while checking user {user_id}; allowing request: {e}"
        )
        return False

    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT 1 FROM blocklist WHERE user_id = %s
                    """,
                    (user_id,),
                )
                result = await cur.fetchone()
                return result is not None
        except Exception as e:
            log_error(f"[blocklist] Failed to check if user {user_id} is blocked: {e}")
            return False


async def get_blocked_users() -> List[Dict]:
    """Get list of all blocked users."""
    await init_blocklist_table()
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT user_id, reason, blocked_at
                    FROM blocklist
                    ORDER BY blocked_at DESC
                    """
                )
                return await cur.fetchall()
        except Exception as e:
            log_error(f"[blocklist] Failed to get blocked users: {e}")
            return []


class BlocklistPlugin:
    """Plugin for user blocking and management."""

    display_name = "Blocklist"

    def __init__(self):
        register_plugin("blocklist", self)
        log_info("[blocklist] BlocklistPlugin initialized and registered")

    def get_supported_action_types(self):
        return ["block_user", "unblock_user", "is_user_blocked", "get_blocked_users"]

    def get_supported_actions(self):
        return {
            "block_user": {
                "description": "Block a user from using the system",
                "required_fields": ["user_id"],
                "optional_fields": ["reason"],
            },
            "unblock_user": {
                "description": "Unblock a previously blocked user",
                "required_fields": ["user_id"],
                "optional_fields": [],
            },
            "is_user_blocked": {
                "description": "Check if a user is currently blocked",
                "required_fields": ["user_id"],
                "optional_fields": [],
            },
            "get_blocked_users": {
                "description": "Get list of all blocked users",
                "required_fields": [],
                "optional_fields": [],
            },
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        """Provide detailed prompt instructions for LLM on how to use blocklist actions."""
        if action_name == "block_user":
            return {
                "description": "Block a user from using the system. Use this when a user is abusive, spamming, or violating terms of service.",
                "when_to_use": "When you need to prevent a user from accessing the system due to inappropriate behavior.",
                "examples": [
                    {
                        "scenario": "User is spamming messages",
                        "payload": {
                            "user_id": 123456789,
                            "reason": "Spamming messages repeatedly",
                        },
                    },
                    {
                        "scenario": "User is being abusive",
                        "payload": {
                            "user_id": 987654321,
                            "reason": "Abusive language towards other users",
                        },
                    },
                ],
                "notes": [
                    "user_id should be the numeric user ID",
                    "reason is optional but recommended for moderation tracking",
                    "Blocking is immediate and persistent until manually unblocked",
                ],
            }
        elif action_name == "unblock_user":
            return {
                "description": "Remove a user from the blocklist, allowing them to use the system again.",
                "when_to_use": "When a previously blocked user should be given another chance or was blocked by mistake.",
                "examples": [
                    {
                        "scenario": "User appeals their block successfully",
                        "payload": {"user_id": 123456789},
                    }
                ],
                "notes": [
                    "user_id should be the numeric user ID",
                    "Unblocking is immediate",
                    "Returns success/failure status",
                ],
            }
        return {}

    def execute_action(self, action: dict, context: dict, bot, original_message):
        action_type = action.get("type")
        payload = action.get("payload", {}) or {}

        if action_type == "block_user":
            user_id = payload.get("user_id")
            reason = payload.get("reason", "No reason provided")
            if user_id:
                import asyncio

                asyncio.create_task(
                    self._block_user_action(context, original_message, user_id, reason)
                )
            return None

        elif action_type == "unblock_user":
            user_id = payload.get("user_id")
            if user_id:
                import asyncio

                asyncio.create_task(
                    self._unblock_user_action(context, original_message, user_id)
                )
            return None

        elif action_type == "is_user_blocked":
            user_id = payload.get("user_id")
            if user_id:
                import asyncio

                asyncio.create_task(
                    self._check_user_blocked(context, original_message, user_id)
                )
            return None

        elif action_type == "get_blocked_users":
            import asyncio

            asyncio.create_task(
                self._send_blocked_users(context, original_message)
            )
            return None

        return None

    async def _block_user_action(self, context, original_message, user_id, reason):
        """Execute block user action and return message action."""
        try:
            await block_user(user_id, reason)
            text = f"✅ User {user_id} has been blocked.\nReason: {reason}"
        except Exception as e:
            text = f"❌ Failed to block user {user_id}: {e}"
        return self._build_message_action(text, context, original_message)

    async def _unblock_user_action(self, context, original_message, user_id):
        """Execute unblock user action and return message action."""
        try:
            success = await unblock_user(user_id)
            if success:
                text = f"✅ User {user_id} has been unblocked."
            else:
                text = f"⚠️ User {user_id} was not in the blocklist."
        except Exception as e:
            text = f"❌ Failed to unblock user {user_id}: {e}"
        return self._build_message_action(text, context, original_message)

    async def _check_user_blocked(self, context, original_message, user_id):
        """Check if user is blocked and return message action."""
        try:
            blocked = await is_user_blocked(user_id)
            status = "🚫 BLOCKED" if blocked else "✅ NOT BLOCKED"
            text = f"User {user_id}: {status}"
        except Exception as e:
            text = f"❌ Failed to check user {user_id}: {e}"
        return self._build_message_action(text, context, original_message)

    async def _send_blocked_users(self, context, original_message):
        """Get list of blocked users and return message action."""
        try:
            blocked_users = await get_blocked_users()
            if blocked_users:
                text = "🚫 Blocked Users:\n"
                for user in blocked_users:
                    text += f"• {user['user_id']}: {user['reason']} (blocked: {user['blocked_at']})\n"
            else:
                text = "✅ No users are currently blocked."
        except Exception as e:
            text = f"❌ Failed to get blocked users: {e}"
        return self._build_message_action(text, context, original_message)

    @staticmethod
    def _build_message_action(text: str, context: dict, original_message) -> dict:
        """Build a message action dict for interface-agnostic delivery."""
        interface_path = None
        if context and isinstance(context, dict):
            interface_path = context.get("interface_path")
        if not interface_path and original_message:
            interface_path = getattr(original_message, "interface_path", None)
        if not interface_path:
            log_warning("[blocklist] Cannot build message action: no interface_path available")
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


PLUGIN_CLASS = BlocklistPlugin
