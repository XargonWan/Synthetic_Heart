# core/interface_adapters.py

"""
Generic adapters for interface abstraction without plugin-specific dependencies.
"""

from typing import Any, Optional, Callable
from core.abstract_context import (
    AbstractContext,
    AbstractUser,
    AbstractMessage,
    AbstractChat,
)
from core.interfaces_registry import get_interface_registry
from core.logging_utils import log_debug, log_warning


def register_interface_with_trainer(
    interface_name: str, interface_instance: Any, trainer_id: Optional[int] = None
):
    """Helper to register an interface and optionally set its trainer ID."""
    registry = get_interface_registry()
    registry.register_interface(interface_name, interface_instance)

    if trainer_id:
        registry.set_trainer_id(interface_name, trainer_id)
        log_debug(
            f"[interface_adapters] Registered {interface_name} with trainer ID {trainer_id}"
        )
    else:
        log_debug(
            f"[interface_adapters] Registered {interface_name} without trainer ID"
        )


def create_generic_adapter(interface_name: str):
    """Create a generic adapter that can be customized by any interface plugin."""

    def generic_to_abstract(data, converter_func: Callable = None) -> AbstractContext:
        """Convert interface-specific data to AbstractContext using provided converter."""
        if converter_func:
            return converter_func(data, interface_name)

        # Fallback generic conversion if no specific converter provided
        log_warning(
            f"[interface_adapters] No converter provided for {interface_name}, using fallback"
        )

        # Try to extract basic info generically
        user_id = (
            getattr(data, "user_id", None)
            or getattr(data, "author_id", None)
            or "unknown"
        )
        username = getattr(data, "username", None) or getattr(data, "author", {}).get(
            "name", "unknown"
        )
        message_id = getattr(data, "id", None) or getattr(data, "message_id", "unknown")
        text_content = (
            getattr(data, "text", None) or getattr(data, "content", "") or str(data)
        )
        chat_id = (
            getattr(data, "chat_id", None)
            or getattr(data, "channel_id", None)
            or "unknown"
        )

        user = AbstractUser(
            id=user_id, username=username, interface_name=interface_name
        )

        abstract_message = AbstractMessage(
            id=message_id,
            text=text_content,
            user=user,
            chat_id=chat_id,
            interface_name=interface_name,
            raw_data={f"{interface_name}_data": data},
        )

        chat = AbstractChat(
            id=chat_id,
            name=getattr(data, "chat_name", None) or f"{interface_name}_chat",
            type="generic",
            interface_name=interface_name,
        )

        return AbstractContext(interface_name, user, abstract_message, chat)

    return generic_to_abstract


def create_generic_reply_function(interface_name: str, send_function: Callable):
    """Create a generic reply function for any interface."""

    async def generic_reply(message: str, **kwargs):
        """Generic reply function that delegates to the interface-specific sender."""
        try:
            await send_function(message, **kwargs)
        except Exception as e:
            log_warning(
                f"[interface_adapters] Failed to send reply via {interface_name}: {e}"
            )

    return generic_reply
