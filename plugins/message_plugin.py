# plugins/message_plugin.py
"""Message plugin for handling text message actions."""

import asyncio
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.core_initializer import INTERFACE_REGISTRY


class MessagePlugin:
    """Plugin to handle message-type actions across multiple interfaces."""
    
    display_name = "Message Handler"

    def __init__(self):
        """Initialize the plugin."""
        # Populate supported interfaces from the registry if available
        if INTERFACE_REGISTRY:
            self.supported_interfaces = list(INTERFACE_REGISTRY.keys())
        else:
            # Default to telegram_bot if no interfaces registered yet
            self.supported_interfaces = ["telegram_bot"]
        log_debug("[message_plugin] MessagePlugin initialized")

    @property
    def description(self):
        """Return a description of this plugin."""
        return "Handles text message sending across different interfaces (Telegram, Discord, etc.)"

    def get_supported_action_types(self):
        """Return the action types this plugin supports."""
        return ["message_telegram_bot", "message_reddit", "message_discord", "message_x"]

    @staticmethod
    def get_interface_id() -> str:
        """Return the unique identifier for this plugin interface."""
        return "message"  # Generic message plugin - works with any interface

    def get_supported_actions(self) -> dict:
        """Return empty dict - let interfaces handle action registration."""
        return {}

    def get_prompt_instructions(self, action_name: str) -> dict:
        """Prompt instructions for supported actions."""
        # No longer provides instructions - interfaces handle this
        return {}

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        """Execute a message action."""
        try:
            await self._handle_message_action(action, context, bot, original_message)
            
        except Exception as e:
            log_error(f"[message_plugin] Error executing message action: {repr(e)}")

    async def handle_custom_action(self, action_type: str, payload: dict):
        """Handle custom message actions."""
        if action_type.startswith("message_"):
            log_info(f"[message_plugin] Handling {action_type} action with payload: " + str(payload))
            # This method is called by the centralized action system
            # The actual execution is done via execute_action

    async def _handle_message_action(self, action: dict, context: dict, bot, original_message):
        """Handle message action execution using the interface registry."""

        payload = action.get("payload", {})
        text = payload.get("text", "")
        target = payload.get("target")
        thread_id = payload.get("thread_id")
        
        # Map action types to interface names
        action_type = action.get("type", "")
        interface_map = {
            "message_telegram_bot": "telegram_bot",
            "message_reddit": "reddit", 
            "message_discord": "discord",
            "message_x": "x"
        }
        
        interface_name = interface_map.get(action_type)
        if not interface_name:
            # Fallback to the interface field if present
            interface_name = action.get("interface", self.supported_interfaces[0])

        log_debug(
            f"[message_plugin] Handling {action_type} via {interface_name}: {text[:50]}..."
        )

        if not text:
            log_warning("[message_plugin] Invalid message action: missing text")
            return

        if not target:
            target = getattr(original_message, "chat_id", None)
            log_debug(f"[message_plugin] No target specified, using original chat_id: {target}")

        if not thread_id and hasattr(original_message, "thread_id"):
            orig_thread = getattr(original_message, "thread_id", None)
            if orig_thread:
                thread_id = orig_thread  # fixed: use thread_id from original message
                log_debug(
                    f"[message_plugin] No thread_id specified, using original thread_id: {thread_id}"
                )

        if not target:
            log_warning("[message_plugin] No valid target found, cannot send message")
            return

        handler = INTERFACE_REGISTRY.get(interface_name)
        if not handler:
            log_warning(f"[message_plugin] Unsupported interface: {interface_name}")
            return

        reply_to = None
        if (
            original_message
            and hasattr(original_message, "chat_id")
            and hasattr(original_message, "message_id")
            and target == getattr(original_message, "chat_id")
        ):
            reply_to = original_message.message_id
            log_debug(f"[message_plugin] Adding reply_to_message_id: {reply_to}")

        send_payload = {"text": text, "target": target}
        if thread_id is not None:
            send_payload["thread_id"] = thread_id

        try:
            await handler.send_message(send_payload, original_message)
            log_info(
                f"[message_plugin] Message successfully sent to {target} (thread: {thread_id}, reply_to: {reply_to})"
            )
        except Exception as e:
            log_error(
                f"[message_plugin] Failed to send message to {target} (thread: {thread_id}): {repr(e)}"
            )



# Export the plugin class
__all__ = ["MessagePlugin"]

# Define the plugin class for automatic loading
PLUGIN_CLASS = MessagePlugin
