# core/reaction_handler.py
"""
Reaction handler for bot messages.

This module provides functionality to add reactions to messages when the bot
is mentioned or triggered, if configured via REACT_WHEN_MENTIONED env variable.
"""

import os
from typing import Optional
from types import SimpleNamespace
from core.logging_utils import log_debug, log_info, log_warning
from core.config_manager import config_registry

# Register REACT_WHEN_MENTIONED configuration
REACT_WHEN_MENTIONED = config_registry.get_var(
    "REACT_WHEN_MENTIONED",
    "👀",
    label="React When Mentioned",
    description="Emoji to use as reaction when bot is mentioned. Leave empty to disable. ⚠️ Note: Some interfaces or servers/channels may not support all emojis as reactions.",
    group="core",
    component="core",
)



def get_reaction_emoji() -> Optional[str]:
    """
    Get the reaction emoji from REACT_WHEN_MENTIONED configuration.
    
    Returns:
        Optional[str]: The emoji to use as reaction, or None if not configured
    """
    raw = REACT_WHEN_MENTIONED
    log_debug(f"[reaction] REACT_WHEN_MENTIONED raw: '{raw}' (type: {type(raw)})")
    emoji = str(raw).strip() if raw else ""
    log_debug(f"[reaction] Processed emoji: '{emoji}'")
    if not emoji:
        return None
    return emoji


async def react_when_mentioned(interface, message, emoji: Optional[str] = None) -> bool:
    """
    Add a reaction to a message using the interface's add_reaction method.
    
    If `emoji` is None this function will query the configured default via
    `get_reaction_emoji()` and skip the reaction if no emoji is configured.
    
    Args:
        interface: The interface instance that supports add_reaction
        message: The message object that triggered the bot
        emoji: Optional emoji to use as reaction (defaults to configured value)
        
    Returns:
        bool: True if reaction was added successfully, False otherwise
    """    # If emoji is None, get configured default
    if emoji is None:
        emoji = get_reaction_emoji()
    log_debug(f"[reaction] react_when_mentioned called with emoji '{emoji}', interface={type(interface).__name__}, message_id={getattr(message, 'message_id', 'unknown')}")
    if not emoji:
        log_debug("[reaction] No emoji configured, skipping reaction")
        return False
    
    log_debug(f"[reaction] Attempting to add reaction '{emoji}' to message via interface {type(interface).__name__}")
    
    try:
        if hasattr(interface, 'add_reaction'):
            success = await interface.add_reaction(message, emoji)
            if success:
                log_info(f"[reaction] Successfully added reaction '{emoji}' via interface")
            return success
        else:
            log_warning(f"[reaction] Interface {type(interface).__name__} does not support add_reaction")
            return False
    except Exception as e:
        log_warning(f"[reaction] Failed to add reaction '{emoji}': {e}")
        return False
