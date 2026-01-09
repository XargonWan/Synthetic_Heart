# plugins/message_plugin.py
"""Message plugin for handling text message actions."""

import asyncio
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.core_initializer import INTERFACE_REGISTRY
from core.config_manager import config_registry


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
        interface_path = payload.get("interface_path")
        target = payload.get("target")  # Fallback for legacy code
        thread_id = payload.get("thread_id")  # Fallback for legacy code
        
        # Decompose interface_path into components
        # Format: "interface_name/chat_id/thread_id" or "interface_name/chat_id"
        if interface_path:
            parts = interface_path.split("/")
            if len(parts) >= 2:
                interface_name_from_path = parts[0]
                target = parts[1]  # chat_id or user_id
                if len(parts) >= 3:
                    thread_id = parts[2]  # thread_id or channel_id
            else:
                log_warning(f"[message_plugin] Invalid interface_path format: {interface_path}")
                return
        
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

        # Rebuild interface_path for the send_payload
        rebuilt_interface_path = None
        if interface_name and target:
            if thread_id:
                rebuilt_interface_path = f"{interface_name}/{target}/{thread_id}"
            else:
                rebuilt_interface_path = f"{interface_name}/{target}"

        send_payload = {"text": text, "target": target}
        if thread_id is not None:
            send_payload["thread_id"] = thread_id
        if rebuilt_interface_path:
            send_payload["interface_path"] = rebuilt_interface_path

        try:
            # If this action originates from a Grillo beat, avoid sending if the last message
            # in the target chat/thread was authored by the synth (to prevent Grillo spamming).
            if isinstance(context, dict) and (context.get("grillo_beat") or context.get("activity_log_id") or context.get("grillo_activity_log_id")):
                try:
                    # Allow runtime configuration to enable/disable duplicate suppression
                    try:
                        suppress_enabled = config_registry.get_value(
                            "GRILLO_SUPPRESS_INACTIVE",
                            True,
                            label="Suppress Grillo outbound messages when last message is from synth",
                            description=("If enabled, Grillo-originated outbound messages will be blocked when the last message "
                                         "in the target thread was authored by the synth to avoid duplicate/spam."),
                            group="grillo",
                            component="grillo",
                            value_type=bool,
                        )
                    except Exception:
                        suppress_enabled = True

                    from core.chat_history_cache import get_last_message
                    target_interface_path = rebuilt_interface_path or interface_path or f"{interface_name}/{target}"

                    # Exempt webui and trainer 1:1 chats from suppression checks (synth should be able to write)
                    try:
                        parts = (target_interface_path or "").split('/')
                        maybe_interface = parts[0] if parts else None
                        maybe_chat_id = parts[1] if len(parts) > 1 else None
                        # Exempt webui-like interfaces
                        if maybe_interface and maybe_interface.lower() in ("synth_webui", "webui"):
                            log_debug(f"[message_plugin] Bypassing Grillo suppression for webui interface: {target_interface_path}")
                        else:
                            # Check trainer exemption
                            try:
                                from core.interfaces_registry import get_interface_registry
                                registry = get_interface_registry()
                                if maybe_interface and maybe_chat_id and maybe_chat_id.lstrip('-').isdigit():
                                    try:
                                        if registry.is_trainer(maybe_interface, int(maybe_chat_id)):
                                            log_debug(f"[message_plugin] Bypassing Grillo suppression for trainer chat: {target_interface_path}")
                                            bypass_trainer = True
                                        else:
                                            bypass_trainer = False
                                    except Exception:
                                        bypass_trainer = False
                                else:
                                    bypass_trainer = False
                            except Exception:
                                bypass_trainer = False

                            if not bypass_trainer:
                                # Determine whether the target chat is public. We apply suppression ONLY for public chats
                                is_public = False
                                try:
                                    if maybe_interface == 'telegram_bot' and maybe_chat_id:
                                        try:
                                            cid = int(maybe_chat_id)
                                            if cid < 0:
                                                is_public = True
                                        except Exception:
                                            # If not parseable, default to non-public
                                            is_public = False
                                    elif maybe_interface in ('discord', 'reddit', 'matrix') and maybe_chat_id:
                                        # For channel-based interfaces assume public unless proven otherwise
                                        is_public = True
                                except Exception:
                                    is_public = False

                                if not is_public:
                                    log_debug(f"[message_plugin] Target chat {target_interface_path} considered non-public; bypassing grillo duplicate suppression")
                                else:
                                    last = await get_last_message(target_interface_path)
                                    if last:
                                        sender_id = last.get("sender_id") or last.get("user_id") or ""
                                        sender_name = (last.get("sender_name") or last.get("username") or "").lower()
                                        # Consider 'self' and obvious bot names as synth-origin
                                        if str(sender_id) == "self" or any(k in sender_name for k in ("rekku", "synth", "bot", "auto_response", "autoreply")):
                                            if not suppress_enabled:
                                                log_debug("[message_plugin] Grillo suppression disabled via config 'GRILLO_SUPPRESS_INACTIVE'; allowing send")
                                            else:
                                                log_info(f"[message_plugin] Skipping Grillo outbound message to {target_interface_path} because last message was from synth ({sender_name}/{sender_id})")
                                                # Record suppression in Grillo activity log if available (best-effort)
                                                try:
                                                    activity_log_id = context.get("activity_log_id") or context.get("grillo_activity_log_id")
                                                    if activity_log_id:
                                                        from plugins.grillo.grillo_impl import GrilloPlugin
                                                        if hasattr(GrilloPlugin, "set_activity_response_text"):
                                                            await GrilloPlugin.set_activity_response_text(int(activity_log_id), "[suppressed duplicate message by grillo]", append=True)
                                                        # Increment suppression metric/counter if plugin exposes it
                                                        try:
                                                            if hasattr(GrilloPlugin, "record_suppressed_event"):
                                                                await GrilloPlugin.record_suppressed_event(activity_log_id=activity_log_id, reason="last_message_from_synth")
                                                        except Exception:
                                                            pass
                                                except Exception:
                                                    pass
                                                return
                    except Exception as e:
                        log_debug(f"[message_plugin] Failed to evaluate grillo duplicate suppression: {e}")
                except Exception as e:
                    log_debug(f"[message_plugin] Failed to evaluate grillo duplicate suppression: {e}")
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
