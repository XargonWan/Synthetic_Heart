# plugins/message_plugin.py
"""Message plugin for handling text message actions."""

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
        return [
            "message_telegram_bot",
            "message_reddit",
            "message_discord",
            "message_discord_bot",
            "message_x",
            "message_synth_webui",
            "message_ollama_serve",
            "message_matrix_chat",
        ]

    @staticmethod
    def get_interface_id() -> str:
        """Return the unique identifier for this plugin interface."""
        return "message"  # Generic message plugin - works with any interface

    def get_supported_actions(self) -> dict:
        """
        Claim support for standard message actions to intercept them
        and ensure context (like TTS audio) is passed to interface execution.
        """
        return {
            "message_telegram_bot": {},
            "message_discord_bot": {},
            "message_synth_webui": {},
            "message_reddit": {},
            "message_x": {},
            "message_matrix_chat": {},
            "message_ollama_serve": {},
        }

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
            log_info(
                f"[message_plugin] Handling {action_type} action with payload: "
                + str(payload)
            )

    async def _handle_message_action(
        self, action: dict, context: dict, bot, original_message
    ):
        """Handle message action execution using the interface registry."""

        payload = action.get("payload", {})
        text = payload.get("text", "")
        interface_path = payload.get("interface_path")
        target = payload.get("target")
        thread_id = payload.get("thread_id")

        # Decompose interface_path into components
        if interface_path:
            parts = interface_path.split("/")
            if len(parts) >= 2:
                # interface_name_from_path = parts[0]
                target = parts[1]  # chat_id or user_id
                if len(parts) >= 3:
                    # Treat an empty third segment (trailing slash) as no thread
                    thread_id = parts[2].strip() or None
            else:
                log_warning(
                    f"[message_plugin] Invalid interface_path format: {interface_path}"
                )

        # Map action types to interface names
        action_type = action.get("type", "")
        interface_map = {
            "message_telegram_bot": "telegram_bot",
            "message_reddit": "reddit",
            "message_discord": "discord",
            "message_discord_bot": "discord_bot",
            "message_x": "x",
            "message_synth_webui": "synth_webui",
            "message_ollama_serve": "ollama_serve",
            "message_matrix_chat": "matrix_chat",
        }

        interface_name = interface_map.get(action_type)
        if not interface_name:
            interface_name = action.get("interface")

        if not interface_name and interface_path:
            interface_name = interface_path.split("/")[0]

        if not interface_name:
            interface_name = (
                self.supported_interfaces[0]
                if self.supported_interfaces
                else "telegram_bot"
            )

        log_debug(
            f"[message_plugin] Handling {action_type} via {interface_name}: {str(text)[:50]}..."
        )

        interface = INTERFACE_REGISTRY.get(interface_name)
        if not interface:
            log_warning(
                f"[message_plugin] Interface '{interface_name}' not found in registry"
            )
            return

        if not target:
            target = getattr(original_message, "chat_id", None)

        if not thread_id:
            # Accept both normalized `thread_id` and Telegram's native `message_thread_id`
            thread_id = getattr(original_message, "thread_id", None) or getattr(
                original_message, "message_thread_id", None
            )

        rebuilt_interface_path = None
        if interface_name and target:
            if thread_id:
                rebuilt_interface_path = f"{interface_name}/{target}/{thread_id}"
            else:
                rebuilt_interface_path = f"{interface_name}/{target}"

        # --- Grillo Suppression Logic ---
        # Outreach beats are EXEMPT from suppression: their purpose is to
        # initiate conversation, so silencing them defeats the feature.
        is_outreach = (
            isinstance(context, dict) and context.get("beat_type") == "outreach"
        )

        if (
            not is_outreach
            and isinstance(context, dict)
            and (
                context.get("grillo_beat")
                or context.get("activity_log_id")
                or context.get("grillo_activity_log_id")
            )
        ):
            try:
                suppress_enabled = config_registry.get_value(
                    "GRILLO_SUPPRESS_INACTIVE",
                    True,
                    label="Suppress Grillo outbound messages when last message is from synth",
                    description=(
                        "When enabled, Grillo will skip outbound messages if the most recent message in the target chat was sent by the synth."
                    ),
                    group="grillo",
                    component="grillo",
                )
            except Exception:
                suppress_enabled = True

            try:
                from core.chat_history_cache import get_last_message

                target_interface_path = (
                    rebuilt_interface_path
                    or interface_path
                    or f"{interface_name}/{target}"
                )

                # Check exemptions (WebUI)
                bypass = False
                if "webui" in (interface_name or "").lower():
                    bypass = True

                # Exemption: Trainer chats (simplified check without registry dependency to avoid circles)
                # If we assume trainer is handled elsewhere or Grillo won't spam trainer chats excessively

                if not bypass:
                    # Check if public chat logic
                    is_public = False
                    # Simple heuristic: Telegram negative IDs, or Discord/Reddit/Matrix channels
                    if interface_name == "telegram_bot" and target:
                        try:
                            if int(target) < 0:
                                is_public = True
                        except Exception:
                            pass
                    elif (
                        interface_name
                        in ["discord", "discord_bot", "reddit", "matrix", "matrix_chat"]
                        and target
                    ):
                        is_public = True

                    if is_public:
                        last = await get_last_message(target_interface_path)
                        if last:
                            sender_name = (
                                last.get("sender_name") or last.get("username") or ""
                            ).lower()
                            sender_id = str(
                                last.get("sender_id") or last.get("user_id") or ""
                            )

                            is_synth = sender_id == "self" or any(
                                k in sender_name for k in ["rekku", "synth", "bot"]
                            )

                            if is_synth and suppress_enabled:
                                log_info(
                                    f"[message_plugin] Suppressing Grillo message to {target_interface_path} (last msg from synth)"
                                )
                                return

            except Exception as e:
                log_debug(f"[message_plugin] Grillo suppression check failed: {e}")

        # --- Dispatch ---
        handler = INTERFACE_REGISTRY.get(interface_name)
        if not handler:
            log_warning(
                f"[message_plugin] Interface '{interface_name}' not found in registry"
            )
            return

        if not target:
            target = getattr(original_message, "chat_id", None)

        if not thread_id:
            # Accept both normalized `thread_id` and Telegram's native `message_thread_id`
            thread_id = getattr(original_message, "thread_id", None) or getattr(
                original_message, "message_thread_id", None
            )

        reply_to = None
        if (
            original_message
            and hasattr(original_message, "chat_id")
            and hasattr(original_message, "message_id")
            and target == getattr(original_message, "chat_id")
        ):
            reply_to = original_message.message_id

        send_payload = {"text": text, "target": target}
        if thread_id is not None:
            send_payload["thread_id"] = thread_id
        if rebuilt_interface_path:
            send_payload["interface_path"] = rebuilt_interface_path

        try:
            await handler.send_message(send_payload, original_message)
            log_info(
                f"[message_plugin] Message successfully sent to {target} (thread: {thread_id}, reply_to: {reply_to})"
            )

        except Exception as e:
            log_error(
                f"[message_plugin] Failed to send message via {interface_name}: {e}"
            )


# Export the plugin class
__all__ = ["MessagePlugin"]

# Define the plugin class for automatic loading
PLUGIN_CLASS = MessagePlugin
