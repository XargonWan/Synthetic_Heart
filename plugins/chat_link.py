"""Chat Link Plugin - Store and resolve mappings between external chats and ChatGPT conversations."""

from __future__ import annotations

from typing import Optional, Dict, Any, Callable, Awaitable, List
import aiomysql
import json

from core.db import get_conn
from core.logging_utils import log_debug, log_error, log_warning, log_info
from core.core_initializer import register_plugin


class ChatLinkError(Exception):
    """Base error for chat link operations."""


class ChatLinkNotFound(ChatLinkError):
    """Raised when a chat link cannot be uniquely resolved."""


class ChatLinkMultipleMatches(ChatLinkError):
    """Raised when more than one chat link matches a lookup."""


class ChatLinkStore:
    """Persistence layer for chat -> ChatGPT conversation links.

    Supports optional tracking of chat and thread names to allow lookup by
    human-readable identifiers. A resolver callback can be registered to
    automatically fetch chat/thread names from interfaces.
    """

    _name_resolvers: Dict[
        str,
        Callable[[int | str, Optional[int | str], Any], Awaitable[Dict[str, Optional[str]]]],
    ] = {}

    def __init__(self) -> None:
        self._table_ensured = False

    # ------------------------------------------------------------------
    # Resolver management
    @classmethod
    def set_name_resolver(
        cls,
        interface: str,
        resolver: Callable[[int | str, Optional[int | str], Any], Awaitable[Dict[str, Optional[str]]]],
    ) -> None:
        """Register a callback used to resolve chat and thread names for an interface."""
        cls._name_resolvers[interface] = resolver

    @classmethod
    def get_name_resolver(cls, interface: str):
        """Get the name resolver for an interface."""
        return cls._name_resolvers.get(interface)

    # ------------------------------------------------------------------
    # Table management
    async def _ensure_table(self) -> None:
        """Create the chatlink table if it doesn't exist with all required columns."""
        if self._table_ensured:
            return
        
        conn = await get_conn()
        async with conn.cursor() as cursor:
            # Create table with base structure (chatgpt_link will be added by selenium_chatgpt.py)
            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chatlink (
                    int_id INT AUTO_INCREMENT PRIMARY KEY,
                    interface VARCHAR(32) NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_id TEXT DEFAULT NULL,
                    chat_name TEXT DEFAULT NULL,
                    message_thread_name TEXT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_chat (interface, chat_id(255))
                )
                """
            )
            
            # Ensure all required columns exist (for existing installations)
            # Note: chatgpt_link is NOT included here - it's managed by selenium_chatgpt.py
            columns_to_ensure = [
                ('thread_id', 'TEXT DEFAULT NULL'),
                ('chat_name', 'TEXT DEFAULT NULL'),
                ('message_thread_name', 'TEXT DEFAULT NULL'),
                ('int_id', 'INT AUTO_INCREMENT PRIMARY KEY'),
                ('created_at', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
                ('last_updated', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP')
            ]
            
            for col_name, col_definition in columns_to_ensure:
                try:
                    # Check if column exists
                    await cursor.execute(
                        """
                        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS 
                        WHERE TABLE_SCHEMA = DATABASE() 
                        AND TABLE_NAME = 'chatlink' 
                        AND COLUMN_NAME = %s
                        """,
                        (col_name,)
                    )
                    result = await cursor.fetchone()
                    
                    if not result:
                        # Column doesn't exist, add it
                        await cursor.execute(f"ALTER TABLE chatlink ADD COLUMN {col_name} {col_definition}")
                except Exception as e:
                    # Log but continue - this is not fatal
                    print(f"Warning: Could not add column {col_name}: {e}")
                    pass
            
            await conn.commit()
        conn.close()
        self._table_ensured = True



    async def get_or_create_internal_id(
        self,
        chat_id: int | str,
        thread_id: Optional[int | str],
        interface: Optional[str] = None,
        *,
        chat_name: Optional[str] = None,
        message_thread_name: Optional[str] = None,
    ) -> int:
        """Get or create an internal ID for a chat/thread combination."""
        await self._ensure_table()
        
        thread_ids = []
        if thread_id is not None:
            thread_ids = [str(thread_id)]
        
        conn = await get_conn()
        try:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                # Try to find existing record
                await cursor.execute(
                    """
                    SELECT int_id FROM chatlink
                    WHERE interface = %s AND chat_id = %s
                    """,
                    (interface, str(chat_id))
                )
                row = await cursor.fetchone()
                
                if row:
                    # Update last_contact and names if provided
                    if chat_name or message_thread_name:
                        await cursor.execute(
                            """
                            UPDATE chatlink 
                            SET chat_name = COALESCE(%s, chat_name),
                                message_thread_name = COALESCE(%s, message_thread_name),
                                last_contact = CURRENT_TIMESTAMP
                            WHERE int_id = %s
                            """,
                            (chat_name, message_thread_name, row['int_id'])
                        )
                        await conn.commit()
                    return row['int_id']
                else:
                    # Create new record
                    await cursor.execute(
                        """
                        INSERT INTO chatlink 
                        (interface, chat_id, thread_id, chat_name, message_thread_name)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (interface, str(chat_id), json.dumps(thread_ids), chat_name, message_thread_name)
                    )
                    await conn.commit()
                    return cursor.lastrowid
        finally:
            conn.close()

    async def ensure_chat_exists(
        self,
        chat_id: int | str,
        thread_id: Optional[int | str] = None,
        interface: Optional[str] = None,
        *,
        chat_name: Optional[str] = None,
        message_thread_name: Optional[str] = None,
    ) -> None:
        """Ensure a chat record exists in the database."""
        await self._ensure_table()
        
        conn = await get_conn()
        async with conn.cursor() as cursor:
            # Check what columns actually exist
            await cursor.execute(
                """
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = DATABASE() 
                AND TABLE_NAME = 'chatlink'
                """
            )
            existing_columns = {row[0] for row in await cursor.fetchall()}
            
            # Build the query based on available columns
            base_columns = ['interface', 'chat_id']
            base_values = [interface, str(chat_id)]
            
            if 'thread_id' in existing_columns:
                base_columns.append('thread_id')
                base_values.append(str(thread_id) if thread_id is not None else '0')
            
            if 'chat_name' in existing_columns and chat_name is not None:
                base_columns.append('chat_name')
                base_values.append(chat_name)
                
            if 'message_thread_name' in existing_columns and message_thread_name is not None:
                base_columns.append('message_thread_name')
                base_values.append(message_thread_name)
            
            columns_str = ', '.join(base_columns)
            placeholders = ', '.join(['%s'] * len(base_values))
            
            await cursor.execute(
                f"""
                REPLACE INTO chatlink 
                ({columns_str})
                VALUES ({placeholders})
                """,
                base_values
            )
            await conn.commit()
        conn.close()

    async def get_chat_info(
        self,
        chat_id: int | str,
        thread_id: Optional[int | str] = None,
        interface: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get chat information for a chat/thread combination."""
        await self._ensure_table()
        
        conn = await get_conn()
        try:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM chatlink
                    WHERE interface = %s AND chat_id = %s AND thread_id = %s
                    """,
                    (interface, str(chat_id), str(thread_id) if thread_id is not None else '0')
                )
                row = await cursor.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    async def resolve_chat_identifier(
        self,
        identifier: str,
        interface: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Resolve a chat identifier (name or ID) to chat records."""
        await self._ensure_table()
        
        conn = await get_conn()
        try:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                # Try exact chat_id match first
                await cursor.execute(
                    """
                    SELECT * FROM chatlink
                    WHERE interface = %s AND chat_id = %s
                    """,
                    (interface, identifier)
                )
                results = await cursor.fetchall()
                
                if not results:
                    # Try chat name match
                    await cursor.execute(
                        """
                        SELECT * FROM chatlink
                        WHERE interface = %s AND chat_name LIKE %s
                        """,
                        (interface, f"%{identifier}%")
                    )
                    results = await cursor.fetchall()
                
                return list(results)
        finally:
            conn.close()

    async def update_chat_names(
        self,
        chat_id: int | str,
        thread_id: Optional[int | str],
        interface: Optional[str] = None,
        *,
        chat_name: Optional[str] = None,
        message_thread_name: Optional[str] = None,
    ) -> int:
        """Update chat and thread names for existing records."""
        await self._ensure_table()
        
        conn = await get_conn()
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE chatlink 
                    SET chat_name = COALESCE(%s, chat_name),
                        message_thread_name = COALESCE(%s, message_thread_name)
                    WHERE interface = %s AND chat_id = %s AND thread_id = %s
                    """,
                    (chat_name, message_thread_name, interface, str(chat_id), 
                     str(thread_id) if thread_id is not None else '0')
                )
                affected_rows = cursor.rowcount
                await conn.commit()
                return affected_rows
        finally:
            conn.close()

    async def update_names_from_resolver(
        self,
        chat_id: int | str,
        thread_id: Optional[int | str],
        *,
        interface: Optional[str] = None,
        bot: Any = None,
    ) -> bool:
        """Use the registered resolver to update chat/thread names."""
        if interface is None:
            log_warning("[chatlink] Interface must be specified for name resolution")
            return False
            
        resolver = self.get_name_resolver(interface)
        if not resolver:
            log_debug(f"[chatlink] No resolver registered for interface: {interface}")
            return False
            
        try:
            try:
                result = await resolver(chat_id, thread_id, bot)
            except TypeError:  # resolver might not accept bot parameter
                result = await resolver(chat_id, thread_id)
        except Exception as e:
            log_warning(f"[chatlink] Resolver execution failed: {e}")
            return False
            
        if not result:
            log_debug("[chatlink] Resolver returned no result")
            return False
            
        # Update names using the resolved values
        affected_rows = await self.update_chat_names(
            chat_id,
            thread_id,
            interface,
            chat_name=result.get("chat_name"),
            message_thread_name=result.get("message_thread_name"),
        )
        
        log_debug(f"[chatlink] Updated {affected_rows} records with resolved names")
        return affected_rows > 0

    async def list_all_links(self, interface: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all stored chat links, optionally filtered by interface."""
        await self._ensure_table()
        
        conn = await get_conn()
        try:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                if interface:
                    await cursor.execute(
                        """
                        SELECT * FROM chatlink
                        WHERE interface = %s
                        ORDER BY chat_name, message_thread_name
                        """,
                        (interface,)
                    )
                else:
                    await cursor.execute(
                        """
                        SELECT * FROM chatlink
                        ORDER BY interface, chat_name, message_thread_name
                        """
                    )
                return await cursor.fetchall()
        finally:
            conn.close()


class ChatLinkPlugin:
    """Plugin for chat link management."""
    
    display_name = "Chat Links"

    def __init__(self):
        self.store = ChatLinkStore()
        register_plugin("chat_link", self)
        log_info("[chat_link] ChatLinkPlugin initialized and registered")

    def get_supported_action_types(self):
        return ["ensure_chat", "get_chat_info", "resolve_chat", "update_chat_names", "list_chats"]

    def get_supported_actions(self):
        return {
            "ensure_chat": {
                "description": "Ensure a chat record exists in the database",
                "required_fields": ["chat_id"],
                "optional_fields": ["thread_id", "interface", "chat_name", "message_thread_name"],
            },
            "get_chat_info": {
                "description": "Get chat information for a chat/thread combination",
                "required_fields": ["chat_id"],
                "optional_fields": ["thread_id", "interface"],
            },
            "resolve_chat": {
                "description": "Resolve a chat identifier to chat records",
                "required_fields": ["identifier"],
                "optional_fields": ["interface"],
            },
            "update_chat_names": {
                "description": "Update chat and thread names",
                "required_fields": ["chat_id"],
                "optional_fields": ["thread_id", "interface", "chat_name", "message_thread_name"],
            },
            "list_chats": {
                "description": "List all stored chat records",
                "required_fields": [],
                "optional_fields": ["interface"],
            },
        }

    def execute_action(self, action: dict, context: dict, bot, original_message):
        action_type = action.get("type")
        payload = action.get("payload", {}) or {}
        
        if action_type == "ensure_chat":
            import asyncio
            asyncio.create_task(self._ensure_chat_action(payload))
            
        elif action_type == "get_chat_info":
            import asyncio
            asyncio.create_task(self._get_chat_info_action(bot, original_message, payload))
            
        elif action_type == "resolve_chat":
            import asyncio
            asyncio.create_task(self._resolve_chat_action(bot, original_message, payload))
            
        elif action_type == "update_chat_names":
            import asyncio
            asyncio.create_task(self._update_names_action(payload))
            
        elif action_type == "list_chats":
            import asyncio
            asyncio.create_task(self._list_chats_action(bot, original_message, payload))

    async def _ensure_chat_action(self, payload):
        """Execute ensure chat action."""
        try:
            await self.store.ensure_chat_exists(
                chat_id=payload["chat_id"],
                thread_id=payload.get("thread_id"),
                interface=payload.get("interface"),
                chat_name=payload.get("chat_name"),
                message_thread_name=payload.get("message_thread_name")
            )
            log_info(f"[chat_link] Ensured chat record for {payload['chat_id']}")
        except Exception as e:
            log_error(f"[chat_link] Failed to ensure chat: {e}")

    async def _get_chat_info_action(self, bot, original_message, payload):
        """Execute get chat info action and send response."""
        try:
            info = await self.store.get_chat_info(
                chat_id=payload["chat_id"],
                thread_id=payload.get("thread_id"),
                interface=payload.get("interface")
            )
            if info:
                response = f"Chat info: {info}"
            else:
                response = f"No chat info found for {payload['chat_id']}"
            
            await bot.send_message(original_message.chat_id, response)
        except Exception as e:
            log_error(f"[chat_link] Failed to get chat info: {e}")

    async def _resolve_chat_action(self, bot, original_message, payload):
        """Execute resolve chat action and send response."""
        try:
            results = await self.store.resolve_chat_identifier(
                identifier=payload["identifier"],
                interface=payload.get("interface")
            )
            if results:
                response = f"Found {len(results)} chat(s):\n"
                for result in results:
                    response += f"• {result['chat_name'] or result['chat_id']} ({result['interface']})\n"
            else:
                response = f"No chats found for identifier '{payload['identifier']}'"
            
            await bot.send_message(original_message.chat_id, response)
        except Exception as e:
            log_error(f"[chat_link] Failed to resolve chat: {e}")

    async def _update_names_action(self, payload):
        """Execute update names action."""
        try:
            affected = await self.store.update_chat_names(
                chat_id=payload["chat_id"],
                thread_id=payload.get("thread_id"),
                interface=payload.get("interface"),
                chat_name=payload.get("chat_name"),
                message_thread_name=payload.get("message_thread_name")
            )
            log_info(f"[chat_link] Updated {affected} chat name records")
        except Exception as e:
            log_error(f"[chat_link] Failed to update names: {e}")

    async def _list_chats_action(self, bot, original_message, payload):
        """Execute list chats action and send response."""
        try:
            chats = await self.store.list_all_links(
                interface=payload.get("interface")
            )
            if chats:
                response = f"Found {len(chats)} chat(s):\n"
                for chat in chats[:10]:  # Limit to first 10
                    name = chat['chat_name'] or chat['chat_id']
                    response += f"• {name} ({chat['interface']})\n"
                if len(chats) > 10:
                    response += f"... and {len(chats) - 10} more"
            else:
                response = "No chats found"
            
            await bot.send_message(original_message.chat_id, response)
        except Exception as e:
            log_error(f"[chat_link] Failed to list chats: {e}")


PLUGIN_CLASS = ChatLinkPlugin
