"""Backend registry for slash commands usable by any interface."""

import asyncio
from typing import Awaitable, Callable, Dict, Any
from core.logging_utils import log_debug
import core.plugin_instance as plugin_instance
from core.context import get_context_state
from core.config import get_active_cortex_engine

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
    user_id: int | None = None,
    interface_id: str | None = None,
    interface_context=None,
) -> str | None:
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
    # Strip @botname suffix that Telegram appends in group chats (e.g. /cortex@synth_bot)
    if "@" in command_name:
        command_name = command_name.split("@", 1)[0]

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
        log_debug(f"[command_registry] Executing command: {command_name} args={args}")
        if "interface_context" in sig.parameters:
            result = await handler(*args, interface_context=interface_context)
        else:
            result = await handler(*args)
        # Ensure every registered command that does something always returns feedback
        if not result:
            result = f"✅ /{command_name} eseguito."
        log_debug(
            f"[command_registry] Command {command_name} returned: {result[:80] if result else None}"
        )
        return result
    except Exception as e:
        log_debug(f"[command_registry] Error executing command {command_name}: {e}")
        return f"❌ Error executing command: {e}"


async def help_command() -> str:
    """Generate help text shared across interfaces."""
    context_status = "active ✅" if get_context_state() else "inactive ❌"
    cortex_mode = await get_active_cortex_engine()

    help_text = (
        "🧞‍♀️ *synth – Available Commands*\n\n"
        "*🧠 Context Mode*\n"
        f"`/context` – Enable/disable history in forwarded messages, currently *{context_status}*\n\n"
        "*🧩 Manual Mode*\n"
        "Reply to a forwarded message with text or content (stickers, photos, audio, files, etc.)\n"
        "`/cancel` – Cancel a pending operation\n\n"
        "*🧱 User Management*\n"
        "`/block <user_id>` – Block a user\n"
        "`/unblock <user_id>` – Unblock a user\n"
        "`/block_list` – List blocked users\n\n"
        "*⚙️ Cortex Engine*\n"
        f"`/cortex` – Show and select current engine (active: `{cortex_mode}`)\n"
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
            get_recent_entries_async,
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
        entries = await get_recent_entries_async(days=days_arg)

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


async def _resolve_cortex_choice(choice_raw: str) -> str:
    """Turn a user-provided string into a canonical engine name.

    The logic mirrors the parsing code used by :func:`cortex_command` so that
    the two helpers stay in sync.  Returns the engine name on success or
    raises ``ValueError`` with a user-friendly error message.
    """
    from core.config import list_available_cortexs, list_available_cortex_engines
    from core.cortex_registry import get_cortex_registry

    # build maps so we can validate kinds and disambiguate short names
    kinds = list_available_cortexs()
    kind_map: dict[str, list[str]] = {}
    for k in kinds:
        try:
            kind_map[k] = list(list_available_cortex_engines(k))
        except Exception:
            kind_map[k] = []

    reg = get_cortex_registry()

    try:
        all_engines = list_available_cortex_engines(None)
    except Exception:
        all_engines = []

    reverse_map: dict[str, list[str]] = {}
    for eng in all_engines:
        meta = reg._engine_meta.get(eng, {}) if hasattr(reg, "_engine_meta") else {}
        k = meta.get("cortex", None) or next(
            (kk for kk, lst in kind_map.items() if eng in lst), None
        )
        reverse_map.setdefault(eng, []).append(k or "unknown")

    choice = choice_raw.strip()

    if "/" in choice:
        parts = choice.split("/", 1)
        if len(parts) != 2:
            raise ValueError(
                "❌ Invalid format. Use `/cortex <kind>/<engine>` or `/cortex <engine>`"
            )
        kind, name = parts[0], parts[1]
        if kind not in kind_map:
            raise ValueError(
                f"❌ Cortex kind `{kind}` not recognised. Available kinds: {', '.join(sorted(kind_map.keys()))}"
            )
        if name not in kind_map.get(kind, []):
            raise ValueError(f"❌ Engine `{name}` not found for cortex `{kind}`.")
        return name

    # short-name resolution across all kinds
    candidates = [e for e in all_engines if e == choice or choice.lower() in e.lower()]
    exact = [e for e in candidates if e == choice]
    if len(exact) == 1:
        return exact[0]
    elif len(candidates) == 1:
        return candidates[0]
    elif len(candidates) > 1:
        opts: list[str] = []
        for e in sorted(set(candidates)):
            ks = reverse_map.get(e, [])
            for kk in sorted(ks):
                opts.append(f"{kk}/{e}")
        hint = "\n".join(f"/cortex {o}" for o in opts)
        raise ValueError(
            f"❌ Found multiple matching engines for '{choice}'. Which one did you mean?\n{hint}"
        )
    else:
        raise ValueError(f"❌ Cortex `{choice}` not found.")


async def cortex_command(*args) -> str:
    """Handle Cortex switching command.

    Usage:
      `/cortex` -> list registered cortex kinds and their engines (kind/engine)
      `/cortex <kind>/<engine>` -> set by fully-qualified name
      `/cortex <engine>` -> set by short name if unambiguous

    If the short name is ambiguous across cortex kinds, the command will ask
    the user to disambiguate using the fully-qualified form.  When invoked
    without arguments the reply now includes *all* scope overrides (base/live,
    grillo, trainer) to make it easy to see what engines are active in which
    contexts.
    """
    from core.config import (
        get_active_cortex_engine,
        list_available_cortex_engines,
        list_available_cortexs,
    )
    from core.cortex_registry import get_cortex_registry

    # resolve active engines across scopes
    base = await get_active_cortex_engine(None)
    grillo = await get_active_cortex_engine("grillo")
    trainer = await get_active_cortex_engine("trainer")

    reg = get_cortex_registry()

    # Build kind -> engines map (stable ordering)
    kinds = list_available_cortexs()
    kind_map: dict[str, list[str]] = {}
    for k in kinds:
        try:
            kind_map[k] = list(list_available_cortex_engines(k))
        except Exception:
            kind_map[k] = []

    # Also build reverse map engine -> [kinds] (used for disambiguation below)
    reverse_map: dict[str, list[str]] = {}
    try:
        all_engines = list_available_cortex_engines(None)
    except Exception:
        all_engines = []
    for eng in all_engines:
        meta = reg._engine_meta.get(eng, {}) if hasattr(reg, "_engine_meta") else {}
        k = meta.get("cortex", None) or next(
            (kk for kk, lst in kind_map.items() if eng in lst), None
        )
        reverse_map.setdefault(eng, []).append(k or "unknown")

    # No-arg -> list by kind/engine with scope info.  We display the
    # *raw* override value for grillo/trainer so that when the setting is
    # "Default" (i.e. no override) the output reads "Default" instead of
    # repeating the base engine; this matches the Web UI behaviour.
    if not args:
        # helper for deciding what to show
        from core.config import config_registry

        def _fmt_override(scope: str, effective: str) -> str:
            key = "GRILLO_CORTEX" if scope == "grillo" else "TRAINER_CORTEX"
            raw = config_registry.get_value(key, "Default")
            if raw in (None, "", "Default", "None"):
                return "Default"
            return raw

        grillo_display = _fmt_override("grillo", grillo)
        trainer_display = _fmt_override("trainer", trainer)

        lines: list[str] = ["*Active Cortex engines:*"]
        # show base engine separately from any live override
        lines.append(f"• base: `{base}`")
        # trainer and grillo overrides always shown (even if Default)
        lines.append(f"• trainer override: `{trainer_display}`")
        lines.append(f"• grillo override: `{grillo_display}`")
        # optionally show live override when configured and different from base
        try:
            from core.config import config_registry

            live_override = config_registry.get_value("LIVE_CORTEX", "Default")
        except Exception:
            live_override = "Default"
        if (
            live_override
            and live_override not in ("Default", "", None)
            and live_override != base
        ):
            lines.append(f"• live override: `{live_override}`")
        lines.append("\n*Available Cortex Engines:*")
        for k in sorted(kind_map.keys()):
            engines = kind_map.get(k) or []
            if not engines:
                continue
            lines.append(f"\n{k}:")
            for e in sorted(engines):
                lines.append(f"• `{k}/{e}`")
        lines.append(
            "\nTo change base: `/cortex <kind>/<engine>` or `/cortex <engine>` (if unambiguous)"
        )
        lines.append("To override grillo: `/cortex_grillo <engine>`")
        lines.append("To override trainer: `/cortex_trainer <engine>`")
        return "\n".join(lines)

    choice_raw = str(args[0]).strip()

    try:
        selected_engine = await _resolve_cortex_choice(choice_raw)
    except ValueError as ve:
        return str(ve)

    # Final step: switch via central helper.
    # use_hot_swap=True calls load_plugin() directly — much lighter than
    # initialize_all() and avoids leaving selenium polling tasks hanging
    # with driver=None when the old engine is torn down mid-request.
    try:
        from core.config import switch_active_cortex_engine

        await switch_active_cortex_engine(selected_engine, use_hot_swap=True)
        return f"✅ Cortex engine dynamically updated to `{selected_engine}`."
    except Exception as e:
        return f"❌ Error loading plugin: {e}"


# Register cortex command after function is defined
register_command("cortex", cortex_command)


async def llm_alias(*args) -> str:
    """Backward-compatible alias for `/llm` (deprecated).

    This returns the same result as `/cortex` but prepends a deprecation hint.
    """
    res = await cortex_command(*args)
    prefix = "⚠️ `/llm` is deprecated — use `/cortex` instead.\n\n"
    return prefix + res


# Deprecated alias (kept for backward compatibility)
register_command("llm", llm_alias)


# convenience aliases for setting particular cortex scopes
async def cortex_live_alias(*args) -> str:
    """Alias for `/cortex` that makes it clear we're changing the live/base engine."""
    return await cortex_command(*args)


async def cortex_grillo_command(*args) -> str:
    """Show or update the Cortex engine used for grillo beats.

    Usage:
      `/cortex_grillo` -> display the current override and available engines
      `/cortex_grillo <engine>` -> set a new engine (short names allowed)
    """
    from core.config import (
        set_scope_cortex,
        get_active_cortex_engine,
        list_available_cortex_engines,
    )

    if not args:
        current = await get_active_cortex_engine("grillo")
        engines = list_available_cortex_engines(None)
        msg = f"*Active Cortex (grillo override):* `{current}`\n\n*Available:*"
        msg += "\n" + "\n".join(f"• `{name}`" for name in engines)
        msg += "\n\nTo change: `/cortex_grillo <kind>/<engine>` or `/cortex_grillo <engine>`"
        return msg

    choice_raw = str(args[0]).strip()
    try:
        engine = await _resolve_cortex_choice(choice_raw)
    except ValueError as ve:
        return str(ve)

    await set_scope_cortex("grillo", engine)
    return f"✅ Cortex engine override for grillo updated to `{engine}`."


async def cortex_trainer_command(*args) -> str:
    """Show or update the Cortex engine used for the trainer interface.

    Works exactly like :func:`cortex_grillo_command` but targets the
    ``trainer`` scope instead of ``grillo``.
    """
    from core.config import (
        set_scope_cortex,
        get_active_cortex_engine,
        list_available_cortex_engines,
    )

    if not args:
        current = await get_active_cortex_engine("trainer")
        engines = list_available_cortex_engines(None)
        msg = f"*Active Cortex (trainer override):* `{current}`\n\n*Available:*"
        msg += "\n" + "\n".join(f"• `{name}`" for name in engines)
        msg += "\n\nTo change: `/cortex_trainer <kind>/<engine>` or `/cortex_trainer <engine>`"
        return msg

    choice_raw = str(args[0]).strip()
    try:
        engine = await _resolve_cortex_choice(choice_raw)
    except ValueError as ve:
        return str(ve)

    await set_scope_cortex("trainer", engine)
    return f"✅ Cortex engine override for trainer updated to `{engine}`."


# register the new helper commands
register_command("cortex_live", cortex_live_alias)
register_command("cortex_grillo", cortex_grillo_command)
register_command("cortex_trainer", cortex_trainer_command)


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
    from core.context import get_context_state, set_context_state

    if args and args[0].lower() in ["on", "enable", "true", "1"]:
        set_context_state(True)
        return "✅ Context mode enabled."
    elif args and args[0].lower() in ["off", "disable", "false", "0"]:
        set_context_state(False)
        return "❌ Context mode disabled."
    else:
        # Toggle
        set_context_state(not get_context_state())
        state = "enabled" if get_context_state() else "disabled"
        return f"🔄 Context mode {state}."


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


# /say command removed from backend registry (function deprecated).


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
        from core import response_proxy
        from core.interfaces_registry import get_interface_registry

        # Get user and trainer info
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id:
            return "❌ Cannot identify user"

        interface_registry = get_interface_registry()
        # Get current interface dynamically
        interface_name = getattr(
            interface_context.get("bot"), "get_interface_id", lambda: "unknown"
        )()
        trainer_id = interface_registry.get_trainer_id(interface_name)

        if user_id != trainer_id:
            return "❌ Access denied. This command requires trainer privileges."

        # Check for pending operations
        has_pending_response = response_proxy.has_pending(trainer_id)

        if has_pending_response:
            response_proxy.clear_target(trainer_id)
            return "❌ Pending operations cancelled."
        else:
            return "⚠️ No active operation to cancel."

    except Exception as e:
        return f"❌ Error cancelling operations: {e}"


register_command("cancel", cancel_command)
register_command("logchat", logchat_command)


async def clean_chat_link_command(*args: str, interface_context: Any = None) -> str:
    """Remove the path link between a chat_id and its conversation folder.

    Usage:
      /clean_chat_link <chat_id>  – Remove the mapping for the given chat_id.
      /clean_chat_link            – Remove the mapping for the *current* chat
                                    (requires interface_context).
    """
    from core.recent_chats import clear_chat_path, get_chat_path

    # Resolve target chat_id
    if args:
        try:
            target_id: int | str = int(args[0])
        except ValueError:
            return "❌ Use: `/clean_chat_link <chat_id>` where chat_id is an integer."
    else:
        # Try to infer from interface context
        target_id_raw: int | str | None = None
        if interface_context and isinstance(interface_context, dict):
            update = interface_context.get("update")
            if update and getattr(update, "effective_chat", None):
                target_id_raw = update.effective_chat.id
        if target_id_raw is None:
            return "❌ Use: `/clean_chat_link <chat_id>` or run inside a chat."
        target_id = target_id_raw

    existing = get_chat_path(target_id)
    if existing is None:
        return f"⚠️ No chat link found for `{target_id}`."

    clear_chat_path(target_id)
    return f"✅ Chat link removed for `{target_id}` (was: `{existing}`)."


register_command("clean_chat_link", clean_chat_link_command)
