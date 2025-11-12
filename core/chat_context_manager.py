"""Centralized chat context memory management with persistence.

This module manages context_memory across all interfaces, ensuring:
1. Consistent deque sizes based on CHAT_HISTORY_LIMIT
2. Automatic persistence to database
3. Loading of persisted history on startup
4. No interface-specific context management logic
"""

from collections import deque
from typing import Dict, Optional
from core.logging_utils import log_debug, log_warning, log_info
from core.config_manager import config_registry

# Global context memory
_context_memory: Dict[str, deque] = {}
_initialized = False


async def initialize_context_manager() -> None:
    """Initialize context manager and load persisted chat history.
    
    Should be called during core initialization before any interfaces start.
    """
    global _initialized
    if _initialized:
        log_debug("[context_manager] Already initialized")
        return
    
    try:
        # Ensure chat history cache table exists
        from core.chat_history_cache import init_chat_history_table
        await init_chat_history_table()
        log_info("[context_manager] Chat history cache table initialized")
    except Exception as e:
        log_warning(f"[context_manager] Failed to initialize chat history cache: {e}")
    
    _initialized = True
    log_info("[context_manager] Context manager initialized")


def get_context_memory() -> Dict[str, deque]:
    """Get the global context memory dictionary.
    
    This should be used by all interfaces instead of maintaining their own.
    """
    return _context_memory


def get_or_create_chat_context(chat_id: str) -> deque:
    """Get or create a context deque for a chat.
    
    Args:
        chat_id: The chat identifier
        
    Returns:
        A deque with maxlen=CHAT_HISTORY_LIMIT
    """
    if chat_id not in _context_memory:
        limit = config_registry.get_var(
            "CHAT_HISTORY_LIMIT",
            10,
            label="Chat History Limit",
            description="Number of messages to keep in memory per chat",
            group="core",
            component="chat_context_manager"
        )
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 10
        
        _context_memory[chat_id] = deque(maxlen=limit)
        log_debug(f"[context_manager] Created context for chat {chat_id} with limit {limit}")
    
    return _context_memory[chat_id]


async def add_message_to_context(
    chat_id: str,
    message_text: str,
    sender_name: str,
    sender_id: str,
    interface: str,
    thread_id: Optional[str] = None,
    message_id: Optional[int] = None,
    timestamp: Optional[str] = None,
    **extra_fields
) -> None:
    """Add a message to chat context with automatic persistence.
    
    This function:
    1. Adds the message to the in-memory context_memory deque
    2. Persists it to the database cache
    3. Auto-cleans old messages beyond CHAT_HISTORY_LIMIT
    
    Args:
        chat_id: Chat identifier
        message_text: The message text
        sender_name: Sender display name
        sender_id: Sender unique ID
        interface: Interface name (e.g., 'telegram_bot', 'discord_interface')
        thread_id: Optional thread/topic ID
        message_id: Optional message ID from interface
        timestamp: Optional ISO format timestamp
        **extra_fields: Additional fields to store in context
    """
    # Add to in-memory context
    context = get_or_create_chat_context(chat_id)
    
    message_obj = {
        "message_id": message_id,
        "user_id": sender_id,
        "username": sender_name,
        "text": message_text,
        "timestamp": timestamp,
        "interface": interface,
        "thread_id": thread_id,
    }
    message_obj.update(extra_fields)
    
    context.append(message_obj)
    log_debug(f"[context_manager] Added message to context for chat {chat_id}")
    
    # Persist to database (non-blocking, don't let DB failures affect message processing)
    try:
        from core.chat_history_cache import save_chat_message
        await save_chat_message(
            chat_id=chat_id,
            message_text=message_text,
            sender_name=sender_name,
            sender_id=sender_id,
            interface=interface,
            thread_id=thread_id
        )
    except Exception as e:
        log_warning(f"[context_manager] Failed to persist message to cache: {e}")


async def load_chat_history(chat_id: str) -> None:
    """Load persisted chat history into context memory.
    
    This is called during initialization to restore chat history
    from previous sessions.
    
    Args:
        chat_id: Chat identifier to load history for
    """
    try:
        from core.chat_history_cache import load_chat_history as cache_load
        
        history = await cache_load(chat_id)
        if history:
            context = get_or_create_chat_context(chat_id)
            # Load all messages from cache
            for msg in history:
                context.append(msg)
            log_info(f"[context_manager] Loaded {len(history)} messages for chat {chat_id}")
        else:
            log_debug(f"[context_manager] No persisted history for chat {chat_id}")
    except Exception as e:
        log_warning(f"[context_manager] Failed to load chat history for {chat_id}: {e}")


def clear_chat_context(chat_id: str) -> None:
    """Clear context for a specific chat.
    
    Args:
        chat_id: Chat identifier
    """
    if chat_id in _context_memory:
        _context_memory[chat_id].clear()
        log_debug(f"[context_manager] Cleared context for chat {chat_id}")


def get_context_stats() -> dict:
    """Get statistics about the context memory.
    
    Returns:
        Dict with keys: 'total_chats', 'total_messages', 'chats'
    """
    stats = {
        'total_chats': len(_context_memory),
        'total_messages': sum(len(deq) for deq in _context_memory.values()),
        'chats': {}
    }
    
    for chat_id, deq in _context_memory.items():
        stats['chats'][chat_id] = {
            'messages': len(deq),
            'maxlen': deq.maxlen
        }
    
    return stats
