# plugins/message_plugin.py
"""Message plugin for handling text message actions."""

import asyncio
from datetime import datetime, timezone
import difflib
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
            # If this action originates from a Grillo beat, avoid sending in noisy/public chats
            # if the last message was from the synth within a cooldown window, and also
            # avoid sending content that duplicates recent messages (exact or fuzzy).
            if isinstance(context, dict) and (context.get("grillo_beat") or context.get("activity_log_id") or context.get("grillo_activity_log_id")):
                try:
                    # Configuration values
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

                    try:
                        cooldown_hours = int(config_registry.get_value(
                            "GRILLO_COOLDOWN_HOURS", 24,
                            label="Grillo cooldown hours for public chats",
                            description="Hours to wait after a synth message before allowing Grillo to post in a public chat.",
                            group="grillo",
                            component="message_plugin",
                            value_type=int,
                        ))
                    except Exception:
                        cooldown_hours = 24

                    try:
                        dup_enabled = bool(config_registry.get_value(
                            "GRILLO_DUP_SIMILARITY_ENABLED", True,
                            label="Enable Grillo duplicate similarity suppression",
                            description="If enabled, Grillo will avoid posting messages that are semantically or textually similar to recent messages.",
                            group="grillo",
                            component="message_plugin",
                            value_type=bool,
                        ))
                    except Exception:
                        dup_enabled = True

                    try:
                        dup_threshold = float(config_registry.get_value(
                            "GRILLO_DUP_SIMILARITY_THRESHOLD", 0.8,
                            label="Grillo duplicate similarity threshold",
                            description="Similarity ratio (0..1) above which a message is considered duplicate.",
                            group="grillo",
                            component="message_plugin",
                            value_type=float,
                        ))
                    except Exception:
                        dup_threshold = 0.8

                    try:
                        dup_lookback = int(config_registry.get_value(
                            "GRILLO_DUP_SIMILARITY_LOOKBACK", 3,
                            label="Grillo duplicate lookback",
                            description="Number of recent messages to compare against for duplicate detection.",
                            group="grillo",
                            component="message_plugin",
                            value_type=int,
                        ))
                    except Exception:
                        dup_lookback = 3

                    from core.chat_history_cache import get_last_message, load_chat_history
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
                                        if str(sender_id) == "self" or any(k in sender_name for k in ("synth", "bot", "auto_response", "autoreply")):
                                            # Check time-based cooldown
                                            if suppress_enabled:
                                                last_ts = last.get('timestamp')
                                                last_dt = None
                                                try:
                                                    if isinstance(last_ts, str):
                                                        try:
                                                            last_dt = datetime.fromisoformat(last_ts.replace('Z', '+00:00'))
                                                        except Exception:
                                                            last_dt = None
                                                    elif isinstance(last_ts, datetime):
                                                        last_dt = last_ts
                                                    if last_dt is not None:
                                                        if last_dt.tzinfo is None:
                                                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                                                        else:
                                                            last_dt = last_dt.astimezone(timezone.utc)
                                                except Exception:
                                                    last_dt = None

                                                now_dt = datetime.utcnow().replace(tzinfo=timezone.utc)
                                                if last_dt is None:
                                                    # If we can't parse timestamp, fall back to immediate suppression
                                                    log_info(f"[message_plugin] Skipping Grillo outbound message to {target_interface_path} because last message was from synth ({sender_name}/{sender_id}) and timestamp is unknown")
                                                    try:
                                                        activity_log_id = context.get("activity_log_id") or context.get("grillo_activity_log_id")
                                                        if activity_log_id:
                                                            from plugins.grillo.grillo_impl import GrilloPlugin
                                                            if hasattr(GrilloPlugin, "set_activity_response_text"):
                                                                await GrilloPlugin.set_activity_response_text(int(activity_log_id), "[suppressed: last_message_from_synth_unknown_ts]", append=True)
                                                            try:
                                                                if hasattr(GrilloPlugin, "record_suppressed_event"):
                                                                    await GrilloPlugin.record_suppressed_event(activity_log_id=activity_log_id, reason="last_message_from_synth_unknown_ts")
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        pass
                                                    return

                                                hours_since = (now_dt - last_dt).total_seconds() / 3600.0
                                                if hours_since < float(cooldown_hours):
                                                    log_info(f"[message_plugin] Skipping Grillo outbound message to {target_interface_path} because last message from synth was {hours_since:.2f}h ago (cooldown={cooldown_hours}h)")
                                                    try:
                                                        activity_log_id = context.get("activity_log_id") or context.get("grillo_activity_log_id")
                                                        if activity_log_id:
                                                            from plugins.grillo.grillo_impl import GrilloPlugin
                                                            if hasattr(GrilloPlugin, "set_activity_response_text"):
                                                                await GrilloPlugin.set_activity_response_text(int(activity_log_id), f"[suppressed: cooldown {hours_since:.2f}h<{cooldown_hours}h]", append=True)
                                                            try:
                                                                if hasattr(GrilloPlugin, "record_suppressed_event"):
                                                                    await GrilloPlugin.record_suppressed_event(activity_log_id=activity_log_id, reason="cooldown")
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        pass
                                                    return

                                            else:
                                                log_debug("[message_plugin] Grillo suppression disabled via config 'GRILLO_SUPPRESS_INACTIVE'; allowing send")

                                    # Duplicate-similarity suppression
                                    if dup_enabled:
                                        try:
                                            recent = await load_chat_history(target_interface_path)
                                            # Take most recent messages and compare
                                            candidates = list(recent)[-dup_lookback:] if recent else []
                                            for c in reversed(candidates):
                                                cand_text = c.get('text') if isinstance(c, dict) else None
                                                if not cand_text:
                                                    continue
                                                # Exact match
                                                if cand_text.strip() == text.strip():
                                                    log_info(f"[message_plugin] Suppressing Grillo message to {target_interface_path}: exact duplicate of recent message")
                                                    try:
                                                        activity_log_id = context.get("activity_log_id") or context.get("grillo_activity_log_id")
                                                        if activity_log_id:
                                                            from plugins.grillo.grillo_impl import GrilloPlugin
                                                            if hasattr(GrilloPlugin, "set_activity_response_text"):
                                                                await GrilloPlugin.set_activity_response_text(int(activity_log_id), "[suppressed: exact_duplicate]", append=True)
                                                            try:
                                                                if hasattr(GrilloPlugin, "record_suppressed_event"):
                                                                    await GrilloPlugin.record_suppressed_event(activity_log_id=activity_log_id, reason="exact_duplicate")
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        pass
                                                    return

                                                # Fuzzy match (difflib)
                                                try:
                                                    import difflib
                                                    ratio = difflib.SequenceMatcher(None, cand_text, text).ratio()
                                                    if ratio >= float(dup_threshold):
                                                        log_info(f"[message_plugin] Suppressing Grillo message to {target_interface_path}: fuzzy duplicate (ratio={ratio:.2f})")
                                                        try:
                                                            activity_log_id = context.get("activity_log_id") or context.get("grillo_activity_log_id")
                                                            if activity_log_id:
                                                                from plugins.grillo.grillo_impl import GrilloPlugin
                                                                if hasattr(GrilloPlugin, "set_activity_response_text"):
                                                                    await GrilloPlugin.set_activity_response_text(int(activity_log_id), f"[suppressed: fuzzy_duplicate ratio={ratio:.2f}>", append=True)
                                                                try:
                                                                    if hasattr(GrilloPlugin, "record_suppressed_event"):
                                                                        await GrilloPlugin.record_suppressed_event(activity_log_id=activity_log_id, reason="fuzzy_duplicate")
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            pass
                                                        return
                                                except Exception:
                                                    # If similarity check fails, continue
                                                    pass

                                        except Exception as e:
                                            log_debug(f"[message_plugin] Failed to evaluate duplicate similarity suppression: {e}")
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
