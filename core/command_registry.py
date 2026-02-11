"""Backend registry for slash commands usable by any interface."""

from typing import Awaitable, Callable, Dict, Any
from core.logging_utils import log_debug
import core.plugin_instance as plugin_instance
from core.context import get_context_state
from core.config import get_active_llm

CommandHandler = Callable[..., Awaitable[str]]

_commands: Dict[str, CommandHandler] = {}


def register_command(name: str, handler: CommandHandler) -> None:
    """Register a backend command handler."""
    _commands[name] = handler
    log_debug(f"[command_registry] registered command: {name}")


def list_commands() -> list[str]:
    return list(_commands.keys())


def get_handler(name: str) -> CommandHandler | None:
    return _commands.get(name)


async def execute_command(name: str, *args: Any, **kwargs: Any) -> str:
    handler = get_handler(name)
    if not handler:
        raise ValueError(f"Unknown command: {name}")
    return await handler(*args, **kwargs)


async def handle_command_message(
    command_text: str,
    user_id: int = None,
    interface_id: str = None,
    interface_context=None,
) -> str:
    """
    Generic command handler for all interfaces.

    Args:
        command_text: The command text (e.g., "/help" or "help arg1 arg2")
        user_id: User ID for permission checking (optional)
        interface_id: Interface identifier for specific handling (optional)
        interface_context: Interface-specific context (bot instance, update, etc.)

    Returns:
        Response text to send back to user
    """
    from core.interfaces_registry import get_interface_registry

    # Parse command and arguments
    parts = command_text.strip().split()
    if not parts:
        return "❌ Invalid command."

    command_name = parts[0]
    if command_name.startswith("/"):
        command_name = command_name[1:]  # Remove leading slash

    args = parts[1:] if len(parts) > 1 else []

    # Check if command exists
    if command_name not in _commands:
        # Return None to indicate unknown command - interfaces should ignore this
        return None

    # Permission check - most commands require trainer privileges
    interface_registry = get_interface_registry()
    if user_id and interface_id:
        if not interface_registry.is_trainer(interface_id, user_id):
            return "❌ Access denied. This command requires trainer privileges."

    try:
        # Pass interface context to commands that support it
        handler = _commands[command_name]
        import inspect

        sig = inspect.signature(handler)
        if "interface_context" in sig.parameters:
            result = await handler(*args, interface_context=interface_context)
        else:
            result = await handler(*args)
        return result
    except Exception as e:
        log_debug(f"[command_registry] Error executing command {command_name}: {e}")
        return f"❌ Error executing command: {e}"


async def help_command() -> str:
    """Generate help text shared across interfaces."""
    context_status = "active ✅" if get_context_state() else "inactive ❌"
    llm_mode = await get_active_llm()

    help_text = (
        "🧞‍♀️ *synth – Available Commands*\n\n"
        "*🧠 Context Mode*\n"
        f"`/context` – Enable/disable history in forwarded messages, currently *{context_status}*\n\n"
        "*✏️ /say Command*\n"
        "`/say` – Select a chat from recent ones\n"
        "`/say <id> <message>` – Send a message directly to a chat\n\n"
        "*🧩 Manual Mode*\n"
        "Reply to a forwarded message with text or content (stickers, photos, audio, files, etc.)\n"
        "`/cancel` – Cancel a pending send\n\n"
        "*🧱 User Management*\n"
        "`/block <user_id>` – Block a user\n"
        "`/unblock <user_id>` – Unblock a user\n"
        "`/block_list` – List blocked users\n\n"
        "*⚙️ LLM Mode*\n"
        f"`/llm` – Show and select current engine (active: `{llm_mode}`)\n"
    )

    try:
        models = plugin_instance.get_supported_models()
        if models:
            current_model = plugin_instance.get_current_model() or models[0]
            help_text += (
                f"`/model` – View or set active model (active: `{current_model}`)\n"
            )
    except Exception:
        pass

    help_text += (
        "\n*📋 Misc*\n"
        "`/last_chats` – Last active chats\n"
        "`/wake` – Enable normal routing in this chat\n"
        "`/sleep` – Ignore non-command messages in this chat\n"
        "`/status` – Show wake/sleep status for this chat\n"
        "`/diary [days]` – View synth's diary entries (default: 7 days)\n"
        "`/purge_map [days]` – Purge old mappings\n"
        "`/clean_chat_link <chat_id>` – Remove the link between a chat and conversation.\n"
        "`/logchat` – Set the current chat as the log chat\n"
        "`/splitprompt [on|off]` – Enable/disable double-prompt mode (PART1/PART2)\n"
    )
    return help_text


async def diary_command(days: str = "7") -> str:
    """Get diary entries for the specified number of days."""
    try:
        from plugins.ai_diary import (
            get_recent_entries,
            format_diary_for_injection,
            is_plugin_enabled,
        )

        if not is_plugin_enabled():
            return "📔 Diary plugin is currently disabled or unavailable."

        # Parse days argument
        try:
            num_days = int(days) if days else 7
            if num_days <= 0:
                num_days = 7
        except ValueError:
            num_days = 7

        # Get recent entries (no char limit for manual viewing)
        try:
            days_arg = int(num_days)
        except Exception:
            days_arg = 2
        entries = get_recent_entries(days=days_arg, max_chars=None)

        if not entries:
            return f"📔 No diary entries found in the last {num_days} days."
        else:
            response = f"📔 **synth's Diary - Last {num_days} days ({len(entries)} entries)**\n\n"
            response += format_diary_for_injection(entries)
            response += "\n\n_Use `/diary <days>` to view a different time range._"
            return response

    except ImportError:
        return "📔 Diary plugin is not installed."
    except Exception as e:
        log_debug(f"[command_registry] Error in diary command: {e}")
        return "❌ Error retrieving diary entries."


def get_help_text() -> str:
    """Get help text for use in generic commands."""
    import asyncio

    try:
        # Run the async help command
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(help_command())
    except Exception as e:
        log_debug(f"[command_registry] Error getting help text: {e}")
        return "Help text unavailable."


# Register default commands
register_command("help", help_command)
register_command("diary", diary_command)


async def _resolve_interface_context(
    interface_context: Any,
) -> tuple[str | None, Any | None, Any | None]:
    """Extract (interface_id, update, context) from an interface_context payload."""
    if not interface_context:
        return None, None, None
    interface_id = (
        interface_context.get("interface_id")
        if isinstance(interface_context, dict)
        else None
    )
    update = (
        interface_context.get("update") if isinstance(interface_context, dict) else None
    )
    context = (
        interface_context.get("context")
        if isinstance(interface_context, dict)
        else None
    )
    return interface_id, update, context


async def wake_command(interface_context=None) -> str:
    """Set the chat to awake (normal routing)."""
    interface_id, update, _context = await _resolve_interface_context(interface_context)
    if interface_id != "telegram_bot" or not update:
        return "👀 Awake."
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return "⚠️ Unable to determine chat."
    try:
        from core.chat_attention import set_attention

        set_attention(chat_id, True)
    except Exception as exc:
        log_debug(f"[command_registry] Failed to set wake state: {exc}")
        return "❌ Failed to set awake state."
    return "👀 Awake: normal routing enabled."


async def sleep_command(interface_context=None) -> str:
    """Set the chat to sleep (ignore non-command messages)."""
    interface_id, update, _context = await _resolve_interface_context(interface_context)
    if interface_id != "telegram_bot" or not update:
        return "💤 Asleep."
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return "⚠️ Unable to determine chat."
    try:
        from core.chat_attention import set_attention

        set_attention(chat_id, False)
    except Exception as exc:
        log_debug(f"[command_registry] Failed to set sleep state: {exc}")
        return "❌ Failed to set sleep state."
    return "💤 Asleep: non-command messages are ignored."


async def status_command(interface_context=None) -> str:
    """Report wake/sleep state for the chat."""
    interface_id, update, _context = await _resolve_interface_context(interface_context)
    if interface_id != "telegram_bot" or not update:
        return "🤖 Status unavailable for this interface."
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return "⚠️ Unable to determine chat."
    try:
        from core.chat_attention import get_attention

        is_awake = get_attention(chat_id, True)
    except Exception as exc:
        log_debug(f"[command_registry] Failed to read status: {exc}")
        return "❌ Failed to read status."
    status_text = (
        "Awake (normal routing)" if is_awake else "Asleep (ignoring non-commands)"
    )
    return f"🤖 Status: {status_text}"


register_command("wake", wake_command)
register_command("awake", wake_command)
register_command("sleep", sleep_command)
register_command("status", status_command)


async def llm_command(*args) -> str:
    """Handle LLM switching command."""
    from core.config import get_active_llm, list_available_llms

    current = await get_active_llm()
    available = list_available_llms()

    if not args:
        msg = f"*Active LLM:* `{current}`\n\n*Available:*"
        msg += "\n" + "\n".join(f"• `{name}`" for name in available)
        msg += "\n\nTo change: `/llm <name>`"
        return msg

    choice = args[0]
    if choice not in available:
        return f"❌ LLM `{choice}` not found."

    try:
        # Use centralized switching helper to ensure consistent behavior and notifications
        from core.config import switch_active_llm

        await switch_active_llm(choice, use_hot_swap=False)

        return f"✅ LLM mode dynamically updated to `{choice}`."
    except Exception as e:
        return f"❌ Error loading plugin: {e}"


async def model_command(*args) -> str:
    """Handle model switching command."""
    import core.plugin_instance as plugin_instance

    try:
        models = plugin_instance.get_supported_models()
    except Exception:
        return "⚠️ This plugin does not support model selection."

    if not models:
        return "⚠️ No models available for this plugin."

    if not args:
        current = plugin_instance.get_current_model() or models[0]
        msg = "*Available models:*\n" + "\n".join(f"• `{m}`" for m in models)
        msg += f"\n\nActive model: `{current}`"
        msg += "\n\nTo change: `/model <name>`"
        return msg

    choice = args[0]
    if choice not in models:
        return f"❌ Model `{choice}` not valid."

    try:
        plugin_instance.set_current_model(choice)
        return f"✅ Model updated to `{choice}`."
    except Exception as e:
        return f"❌ Error changing model: {e}"


async def last_chats_command(*args) -> str:
    """Get last active chats."""
    from core import recent_chats

    # Note: This is interface-agnostic but needs context from interface
    # The interface should handle the formatting
    entries = await recent_chats.get_last_active_chats_verbose(10, None)
    if not entries:
        return "⚠️ No recent chat found."

    lines = [f"{name} — `{cid}`" for cid, name in entries]
    return "🕔 Last active chats:\n" + "\n".join(lines)


async def context_command(*args) -> str:
    """Handle context enable/disable."""
    from core.context import toggle_context_state, get_context_state

    if args and args[0].lower() in ["on", "enable", "true", "1"]:
        from core.context import enable_context

        enable_context()
        return "✅ Context mode enabled."
    elif args and args[0].lower() in ["off", "disable", "false", "0"]:
        from core.context import disable_context

        disable_context()
        return "❌ Context mode disabled."
    else:
        # Toggle
        toggle_context_state()
        state = "enabled" if get_context_state() else "disabled"
        return f"🔄 Context mode {state}."


register_command("llm", llm_command)
register_command("model", model_command)
register_command("last_chats", last_chats_command)
register_command("context", context_command)


async def splitprompt_command(*args) -> str:
    """Toggle or query the double-prompt (split prompt) feature.

    Usage:
      /splitprompt           -> Show current state
      /splitprompt on|enable -> Enable double-prompt
      /splitprompt off|disable -> Disable double-prompt
    """
    try:
        # Lazy import to avoid cycles
        from core.config_manager import config_registry

        key = "SELENIUM_DOUBLE_PROMPT"

        if not args:
            val = config_registry.get_value(key, True)
            state = "enabled ✅" if bool(val) else "disabled ❌"
            return f"🔀 Double-prompt (PART1/PART2) is currently *{state}* (config: `{key}`)."

        arg = args[0].lower()
        if arg in ["on", "enable", "true", "1"]:
            await config_registry.set_value(key, True)
            return "✅ Double-prompt enabled. Calls will split large prompts into PART1 (context) + PART2 (message)."
        elif arg in ["off", "disable", "false", "0"]:
            await config_registry.set_value(key, False)
            return "❌ Double-prompt disabled. Prompts will be sent in a single pass as before."
        else:
            return "❌ Use: `/splitprompt [on|off]` or `/splitprompt` to show the current state."

    except Exception as e:
        log_debug(f"[command_registry] Error in splitprompt_command: {e}")
        return f"❌ Error handling splitprompt command: {e}"


register_command("splitprompt", splitprompt_command)


async def agent_command(*args, interface_context=None) -> str:
    """Handle agent subcommands (trainer-only).

    Usage:
      /agent approve <proposal_id>
    """
    if not args:
        return "Usage: /agent approve <proposal_id>"

    sub = args[0].lower()
    if sub == "approve":
        if len(args) < 2:
            return "❌ Use: /agent approve <proposal_id>"
        try:
            proposal_id = int(args[1])
        except ValueError:
            return "❌ proposal_id must be an integer"

        # Extract possible trainer info from interface_context
        trainer_id = None
        try:
            if interface_context and isinstance(interface_context, dict):
                update = interface_context.get("update")
                if update and getattr(update, "effective_user", None):
                    trainer_id = getattr(update.effective_user, "id", None)
        except Exception:
            trainer_id = None

        try:
            from core.core_initializer import PLUGIN_REGISTRY

            plugin = PLUGIN_REGISTRY.get("agent")
            if not plugin:
                return "❌ Agent plugin not available"
            original_message = {"sender_id": trainer_id}
            res = await plugin.execute_action(
                {"type": "approve_action", "payload": {"proposal_id": proposal_id}},
                {},
                None,
                original_message,
            )
            return f"✅ Approval result: {res}"
        except Exception as e:
            return f"❌ Error approving proposal: {e}"

    return "❌ Unknown agent subcommand. Use: /agent approve <proposal_id>"


register_command("agent", agent_command)


async def block_command(*args) -> str:
    """Block a user by ID."""
    if not args:
        return "❌ Use: `/block <user_id>`"

    try:
        from plugins.blocklist import block_user

        user_id = int(args[0])
        await block_user(user_id)
        return f"🚫 User {user_id} blocked."
    except (ValueError, IndexError):
        return "❌ Use: `/block <user_id>`"
    except Exception as e:
        return f"❌ Error blocking user: {e}"


async def unblock_command(*args) -> str:
    """Unblock a user by ID."""
    if not args:
        return "❌ Use: `/unblock <user_id>`"

    try:
        from plugins.blocklist import unblock_user

        user_id = int(args[0])
        await unblock_user(user_id)
        return f"✅ User {user_id} unblocked."
    except (ValueError, IndexError):
        return "❌ Use: `/unblock <user_id>`"
    except Exception as e:
        return f"❌ Error unblocking user: {e}"


async def block_list_command(*args) -> str:
    """List all blocked users."""
    try:
        from plugins.blocklist import get_blocked_users

        blocked = await get_blocked_users()
        if not blocked:
            return "✅ No users blocked."
        else:
            return "🚫 Blocked users:\n" + "\n".join(map(str, blocked))
    except Exception as e:
        return f"❌ Error getting blocked users: {e}"


async def purge_map_command(*args) -> str:
    """Purge old message mappings."""
    try:
        from plugins.message_map import cleanup_old_mappings

        days = int(args[0]) if args else 7
        deleted = await cleanup_old_mappings(days * 86400)
        return f"🗑️ Removed {deleted} mappings older than {days} days."
    except ValueError:
        return "❌ Use: `/purge_map [days]`"
    except Exception as e:
        return f"❌ Error purging mappings: {e}"


register_command("block", block_command)
register_command("unblock", unblock_command)
register_command("block_list", block_list_command)
register_command("purge_map", purge_map_command)


async def say_command(*args, interface_context=None) -> str:
    """Send a message to a chat. Interface-agnostic implementation."""
    if not interface_context:
        return "💬 `/say` command usage:\n`/say <chat_id> <message>` - Send message directly to chat ID\n\nNote: Interactive features require interface context."

    # Get interface-specific objects
    update = interface_context.get("update")
    context = interface_context.get("context")
    bot = interface_context.get("bot")

    if not all([update, context, bot]):
        return "❌ Missing interface context for `/say` command"

    if len(args) >= 2:
        # Direct send: /say <chat_id> <message>
        try:
            chat_id = int(args[0])
            message_text = " ".join(args[1:])

            # Use generic interface send function
            try:
                # Get the current interface's send function dynamically
                interface_name = getattr(
                    context.get("bot"), "get_interface_id", lambda: "unknown"
                )()

                if hasattr(context.get("bot"), "send_message"):
                    await context.get("bot").send_message(
                        chat_id=chat_id, text=message_text
                    )
                else:
                    # Fallback: try to find appropriate send function
                    send_func = getattr(context.get("bot"), "send", None) or getattr(
                        context.get("bot"), "send_text", None
                    )
                    if send_func:
                        await send_func(chat_id, message_text)
                    else:
                        return f"❌ No send function available for interface {interface_name}"

                return "✅ Message sent."
            except Exception as e:
                return f"❌ Error sending message: {e}"

        except ValueError:
            # Could be username format
            if args[0].startswith("@"):
                username = args[0]
                message_text = " ".join(args[1:])
                try:
                    chat = await bot.get_chat(username)
                    if chat.type == "private":
                        # Use generic interface send function
                        if hasattr(bot, "send_message"):
                            await bot.send_message(chat_id=chat.id, text=message_text)
                        else:
                            send_func = getattr(bot, "send", None) or getattr(
                                bot, "send_text", None
                            )
                            if send_func:
                                await send_func(chat.id, message_text)
                            else:
                                return "❌ No send function available"
                        return f"✅ Message sent to {username}."
                    else:
                        return f"❌ Cannot send to {username}. They must start the chat with the bot first."
                except Exception as e:
                    return f"❌ Cannot send to {username}: {e}"
            else:
                return "❌ Invalid chat ID format"

    # No arguments - show recent chats (simplified version)
    return "💬 For interactive chat selection, use the interface-specific implementation.\nUse: `/say <chat_id> <message>` or `/say @username <message>`"


async def logchat_command(*args, interface_context=None) -> str:
    """Set current chat as log chat."""
    if not interface_context:
        return "❌ This command requires interface context"

    update = interface_context.get("update")
    context = interface_context.get("context")

    if not all([update, context]):
        return "❌ Missing interface context for `/logchat` command"

    try:
        from core.config import set_log_chat_id_and_thread
        from core.logging_utils import log_debug

        chat_id = update.effective_chat.id

        # Try to get thread_id - python-telegram-bot v20+ uses message_thread_id
        thread_id = None
        if update.effective_message:
            # Try both attribute names for compatibility
            thread_id = getattr(
                update.effective_message, "message_thread_id", None
            ) or getattr(update.effective_message, "thread_id", None)
            log_debug(
                f"[logchat_command] Extracted thread_id: {thread_id} from message"
            )

        # Get interface_id from interface_context
        interface_name = interface_context.get("interface_id", "unknown")

        await set_log_chat_id_and_thread(chat_id, thread_id, interface_name)
        # Escape square brackets to avoid Markdown parsing issues
        return f"✅ This chat is now set as logchat \\[{chat_id}, {thread_id}\\] on {interface_name}"
    except Exception as e:
        return f"❌ Unable to set log chat: {e}"


async def cancel_command(*args, interface_context=None) -> str:
    """Cancel pending operations."""
    if not interface_context:
        return "❌ This command requires interface context"

    update = interface_context.get("update")

    if not update:
        return "❌ Missing interface context for `/cancel` command"

    try:
        from core import response_proxy, say_proxy
        from core.interfaces_registry import get_interface_registry

        # Get user and trainer info
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id:
            return "❌ Cannot identify user"

        interface_registry = get_interface_registry()
        # Get current interface dynamically
        interface_name = getattr(
            context.get("bot"), "get_interface_id", lambda: "unknown"
        )()
        trainer_id = interface_registry.get_trainer_id(interface_name)

        if user_id != trainer_id:
            return "❌ Access denied. This command requires trainer privileges."

        # Check for pending operations
        has_pending_response = response_proxy.has_pending(trainer_id)
        has_pending_say = say_proxy.get_target(trainer_id) not in [None, "EXPIRED"]

        if has_pending_response or has_pending_say:
            response_proxy.clear_target(trainer_id)
            say_proxy.clear(trainer_id)
            return "❌ Pending operations cancelled."
        else:
            return "⚠️ No active operation to cancel."

    except Exception as e:
        return f"❌ Error cancelling operations: {e}"


register_command("say", say_command)
register_command("cancel", cancel_command)
register_command("logchat", logchat_command)
