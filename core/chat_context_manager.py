"""Centralized chat context memory management with persistence.

This module manages context_memory across all interfaces, ensuring:
1. Consistent deque sizes based on CHAT_HISTORY_LIMIT
2. Automatic persistence to database
3. Loading of persisted history on startup
4. No interface-specific context management logic
"""

from collections import deque
from typing import Any, Dict, Optional
from core.logging_utils import log_debug, log_warning, log_info
from core.config_manager import config_registry

# Global context memory
_context_memory: Dict[str, deque] = {}
_initialized = False

# Register configuration variable at module level to ensure visibility
CONTEXT_LINK_MAP = config_registry.get_var(
    "CONTEXT_LINK_MAP",
    default={},
    label="Context Link Map (JSON)",
    value_type="json",
    description=(
        "JSON map to link different interface paths to a single context (Unified Lane). "
        "Format: {'source_path_or_id': 'target_path'}."
    ),
    group="core",
    component="chat_context_manager",
    hidden=True,
)


def _resolve_context_path(interface_path: str) -> str:
    """Resolve interface path to a target path if aliased (Unified Lane)."""
    try:
        alias_map = CONTEXT_LINK_MAP.value
        log_debug(
            f"[context_manager] Loaded alias_map: {alias_map} (type: {type(alias_map)})"
        )

        if not alias_map:
            return interface_path

        if not isinstance(alias_map, dict):
            log_debug(
                f"[context_manager] CONTEXT_LINK_MAP is not a dict: {type(alias_map)}"
            )
            return interface_path

        if interface_path in alias_map:
            target = alias_map[interface_path]
            log_debug(
                f"[context_manager] 🔗 Resolved alias: {interface_path} -> {target}"
            )
            return target

        parts = interface_path.split("/")
        if len(parts) >= 2:
            user_segment = parts[1]
            if user_segment in alias_map:
                target = alias_map[user_segment]
                log_debug(
                    f"[context_manager] 🔗 Resolved user alias: {interface_path} -> {target}"
                )
                return target
    except Exception as e:
        log_warning(f"[context_manager] Error resolving context path: {e}")

    return interface_path


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


def get_or_create_chat_context(interface_path: str) -> deque:
    """Get or create a context deque for an interface path.

    Args:
        interface_path: The interface path (e.g., telegram_bot/123456/2)

    Returns:
        A deque with maxlen=CHAT_HISTORY_LIMIT
    """
    original_path = interface_path
    interface_path = _resolve_context_path(interface_path)

    if interface_path not in _context_memory:
        # Keep legacy exposed var for backward compatibility, but prefer the
        # unified global verbosity setting.
        config_registry.get_var(
            "CHAT_HISTORY_LIMIT",
            10,
            label="Chat History Limit",
            description="(Legacy) Number of messages to keep in memory per chat. Prefer CONTEXT_VERBOSITY.",
            group="core",
            component="chat_context_manager",
        )

        limit = None
        for key in ("CONTEXT_VERBOSITY", "CHAT_HISTORY", "CHAT_HISTORY_LIMIT"):
            try:
                limit = config_registry.get_value(key, None, value_type=int)
                if limit is not None:
                    limit = int(limit)
                    break
            except Exception:
                continue
        if not limit:
            limit = 10

        _context_memory[interface_path] = deque(maxlen=limit)
        if interface_path != original_path:
            log_debug(
                f"[context_manager] Created context for alias {original_path} -> {interface_path} with limit {limit}"
            )
        else:
            log_debug(
                f"[context_manager] Created context for interface_path {interface_path} with limit {limit}"
            )

    return _context_memory[interface_path]


async def add_message_to_context(
    interface_path: str,
    message_text: str,
    sender_name: str,
    sender_id: str,
    message_id: Optional[int] = None,
    timestamp: Optional[str] = None,
    metadata: dict[str, Any] | None = None,
    **extra_fields,
) -> None:
    """Add a message to chat context with automatic persistence.

    This function:
    1. Adds the message to the in-memory context_memory deque
    2. Persists it to the database cache
    3. Auto-cleans old messages beyond CHAT_HISTORY_LIMIT

    Args:
        interface_path: Interface path (e.g., 'telegram_bot/123456/2')
        message_text: The message text
        sender_name: Sender display name
        sender_id: Sender unique ID
        message_id: Optional message ID from interface
        timestamp: Optional ISO format timestamp
        metadata: Optional dict of extra metadata (e.g. reply context).
        **extra_fields: Additional fields to store in context
    """
    interface_path = _resolve_context_path(interface_path)

    # Add to in-memory context
    context = get_or_create_chat_context(interface_path)

    message_obj: dict[str, Any] = {
        "message_id": message_id,
        "user_id": sender_id,
        "username": sender_name,
        "text": message_text,
        "timestamp": timestamp,
        "interface_path": interface_path,
    }
    if metadata:
        message_obj["metadata"] = metadata
    message_obj.update(extra_fields)

    context.append(message_obj)
    log_debug(
        f"[context_manager] Added message to context for interface_path {interface_path}"
    )

    # Automatically update chat activity (mechanical action, centralized here)
    # This tracks the last activity time for each chat without requiring LLM reasoning
    try:
        from plugins.recent_chats import update_chat_activity

        # Extract chat_id from interface_path (format: interface/chat_id/thread_id)
        parts = interface_path.split("/")
        chat_id = parts[1] if len(parts) > 1 else interface_path
        await update_chat_activity(
            chat_id=chat_id,
            metadata={
                "username": sender_name,
                "user_id": sender_id,
                "interface_path": interface_path,
            },
        )
    except Exception as e:
        log_debug(f"[context_manager] Failed to update chat activity: {e}")

    # Persist to database (non-blocking, don't let DB failures affect message processing)
    try:
        from core.chat_history_cache import save_chat_message

        await save_chat_message(
            interface_path=interface_path,
            message_text=message_text,
            sender_name=sender_name,
            sender_id=sender_id,
            timestamp=timestamp,
            metadata=metadata,
        )
    except Exception as e:
        log_warning(f"[context_manager] Failed to persist message to cache: {e}")


async def load_chat_history(interface_path: str) -> None:
    """Load persisted chat history into context memory.

    This is called during initialization to restore chat history
    from previous sessions.

    Args:
        interface_path: Interface path to load history for (e.g., telegram_bot/123456/2)
    """
    try:
        from core.chat_history_cache import load_chat_history as cache_load

        interface_path = _resolve_context_path(interface_path)
        history = await cache_load(interface_path, match_chat_level=True)
        context = get_or_create_chat_context(interface_path)
        # IMPORTANT: This function is used to rehydrate memory from persistence.
        # It must be idempotent (e.g., WebUI refresh/reconnect calls it again).
        # Replace in-memory history with persisted history to avoid duplicates.
        previous_len = len(context)
        context.clear()
        if history:
            for msg in history:
                context.append(msg)
            log_info(
                f"[context_manager] Loaded {len(history)} messages for interface_path {interface_path} "
                f"(replaced {previous_len} in-memory messages)"
            )
        else:
            log_debug(
                f"[context_manager] No persisted history for interface_path {interface_path} "
                f"(cleared {previous_len} in-memory messages)"
            )
    except Exception as e:
        log_warning(
            f"[context_manager] Failed to load chat history for {interface_path}: {e}"
        )


def clear_chat_context(interface_path: str) -> None:
    """Clear context for a specific interface path.

    Args:
        interface_path: Interface path (e.g., telegram_bot/123456/2)
    """
    interface_path = _resolve_context_path(interface_path)
    if interface_path in _context_memory:
        _context_memory[interface_path].clear()
        log_debug(
            f"[context_manager] Cleared context for interface_path {interface_path}"
        )


def get_context_stats() -> dict:
    """Get statistics about the context memory.

    Returns:
        Dict with keys: 'total_paths', 'total_messages', 'paths'
    """
    stats = {
        "total_paths": len(_context_memory),
        "total_messages": sum(len(deq) for deq in _context_memory.values()),
        "paths": {},
    }

    for interface_path, deq in _context_memory.items():
        stats["paths"][interface_path] = {"messages": len(deq), "maxlen": deq.maxlen}

    return stats


async def save_response_message(
    interface_path: str,
    message_text: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Save a message sent by the core (bot response).

    This should be called by the core whenever it sends a message to an interface.

    Args:
        interface_path: Interface path (e.g., telegram_bot/123456/2)
        message_text: The response message text
        metadata: Optional message metadata (e.g. {"tts_url": ...})
    """
    if not interface_path or not message_text:
        return

    interface_path = _resolve_context_path(interface_path)

    try:
        from core.chat_history_cache import save_chat_message

        await save_chat_message(
            interface_path=interface_path,
            message_text=message_text,
            sender_name="self",
            sender_id="self",
            metadata=metadata,
        )
        log_debug(
            f"[context_manager] Saved response message for interface_path {interface_path}"
        )
    except Exception as e:
        log_warning(f"[context_manager] Failed to save response message: {e}")
