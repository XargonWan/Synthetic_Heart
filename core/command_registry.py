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
    user_id: int | str | None = None,
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
        "`/get_interface_path` – Show the interface_path of the current chat\n"
        "`/diary [days]` – View synth's diary entries (default: 7 days)\n"
        "`/purge_map [days]` – Purge old mappings\n"
        "`/logchat` – Set the current chat as the log chat\n"
        "`/splitprompt [on|off]` – Enable/disable double-prompt mode (PART1/PART2)\n"
        "`/task` – List recent agent tasks\n"
        "`/task resume <id>` – Resume a paused (pending) agent task\n"
        "`/plugins` – List plugins and their state\n"
        "`/plugin enable|disable <name>` – Enable or disable a plugin\n"
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


async def get_interface_path_command(interface_context=None) -> str:
    """Report the interface_path of the chat where the command was issued."""
    from core.interface_path_utils import build_interface_path

    interface_id, update, _context = await _resolve_interface_context(interface_context)

    if interface_id == "telegram_bot" and update:
        chat = getattr(update, "effective_chat", None)
        message = getattr(update, "effective_message", None)
        chat_id = chat.id if chat else None
        if chat_id is None:
            return "⚠️ Unable to determine chat."
        thread_id = getattr(message, "message_thread_id", None) if message else None
        interface_path = build_interface_path(
            "telegram_bot",
            str(chat_id),
            str(thread_id) if thread_id else None,
        )
        return f"📍 Interface path: `{interface_path}`"

    if isinstance(interface_context, dict):
        interaction = interface_context.get("discord_interaction")
        if interaction is not None:
            guild = getattr(interaction, "guild", None)
            channel = getattr(interaction, "channel", None)
            guild_id = getattr(guild, "id", None)
            channel_id = getattr(channel, "id", None)
            if guild_id:
                interface_path = build_interface_path(
                    "discord_bot",
                    str(guild_id),
                    str(channel_id) if channel_id else None,
                )
            else:
                user = getattr(interaction, "user", None)
                user_id = getattr(user, "id", None)
                interface_path = build_interface_path("discord_bot", str(user_id))
            return f"📍 Interface path: `{interface_path}`"

    return "📍 Interface path unavailable for this interface."


register_command("wake", wake_command)
register_command("awake", wake_command)
register_command("sleep", sleep_command)
register_command("status", status_command)
register_command("get_interface_path", get_interface_path_command)


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


def _get_engine_models(engine_name: str) -> list[str]:
    """Return the list of models supported by *engine_name*.

    Returns an empty list when the engine is not loaded or does not expose
    model selection.
    """
    from core.cortex_registry import get_cortex_registry

    try:
        instance = get_cortex_registry().get_engine(engine_name)
    except Exception:
        return []
    if instance is None or not hasattr(instance, "get_supported_models"):
        return []
    try:
        return list(instance.get_supported_models() or [])
    except Exception:
        return []


def _get_engine_current_model(engine_name: str) -> str | None:
    """Return the currently selected model for *engine_name*, or ``None``."""
    from core.cortex_registry import get_cortex_registry

    try:
        instance = get_cortex_registry().get_engine(engine_name)
    except Exception:
        return None
    if instance is None or not hasattr(instance, "get_current_model"):
        return None
    try:
        return instance.get_current_model()
    except Exception:
        return None


async def _apply_cortex_model(engine_name: str, model_name: str) -> str:
    """Set *model_name* as the active model for *engine_name* and persist it.

    Returns the applied model name. Raises ``ValueError`` with a
    user-friendly message on failure.
    """
    from core.cortex_registry import get_cortex_registry

    instance = get_cortex_registry().get_engine(engine_name)
    if instance is None:
        raise ValueError(f"❌ Engine `{engine_name}` is not loaded.")
    if not hasattr(instance, "set_current_model"):
        raise ValueError(f"❌ Engine `{engine_name}` does not support model selection.")
    try:
        instance.set_current_model(model_name)
    except ValueError as exc:
        raise ValueError(f"❌ {exc}") from exc

    # Persist model selection to the DB for external endpoint engines.
    try:
        from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine

        if isinstance(instance, ExternalCortexEngine):
            from core.external_endpoints.registry import (
                get_external_endpoint_registry,
            )

            await get_external_endpoint_registry().set_default_model(
                instance._endpoint.id, model_name
            )
    except Exception:
        # Non-fatal: the runtime model is set even if DB persistence fails.
        pass

    return model_name


async def cortex_command(*args) -> str:
    """Handle Cortex switching command.

    Usage:
      `/cortex` -> list registered cortex kinds and their engines (kind/engine)
      `/cortex list` -> list every engine grouped by kind with its models
      `/cortex list <engine>` -> list only the models of that engine
      `/cortex <kind>/<engine>` -> set by fully-qualified name
      `/cortex <engine>` -> set by short name if unambiguous
      `/cortex <engine> <model>` -> set the engine and its active model

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

    async def _safe_active(scope: str | None) -> str:
        """Resolve the active engine for *scope*, degrading gracefully.

        ``get_active_cortex_engine`` re-raises when the configured engine is
        unresolvable (e.g. a stale ``BASE_CORTEX`` pointing at an engine that
        is no longer registered).  We must never let that abort the command:
        otherwise listing (``/cortex``, ``/cortex list``) would break exactly
        when the user needs it most to fix the broken setting.  Fall back to
        the raw configured value, or a placeholder, so listing still renders.
        """
        try:
            return await get_active_cortex_engine(scope)
        except Exception:
            from core.config import config_registry

            key = {
                None: "BASE_CORTEX",
                "grillo": "GRILLO_CORTEX",
                "trainer": "TRAINER_CORTEX",
            }.get(scope, "BASE_CORTEX")
            raw = config_registry.get_value(key, "Default")
            return f"{raw} (unresolved)" if raw not in (None, "") else "unresolved"

    # resolve active engines across scopes (degrade gracefully on failure)
    base = await _safe_active(None)
    grillo = await _safe_active("grillo")
    trainer = await _safe_active("trainer")

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

    # `/cortex list` and `/cortex list <engine>` -> show engines with models.
    if args and str(args[0]).strip().lower() == "list":
        if len(args) >= 2:
            # single-engine listing
            try:
                engine_name = await _resolve_cortex_choice(str(args[1]).strip())
            except ValueError as ve:
                return str(ve)
            models = _get_engine_models(engine_name)
            current = _get_engine_current_model(engine_name)
            lines = [f"*Models for `{engine_name}` (Cortex):*"]
            if not models:
                lines.append("_No models available for this engine._")
            else:
                for m in models:
                    marker = " ✅" if m == current else ""
                    lines.append(f"• `{m}`{marker}")
            lines.append("")
            lines.append(f"To switch: `/cortex {engine_name} <model>`")
            return "\n".join(lines)

        # full listing: every engine grouped by kind with its models
        lines = ["*Available Cortex Engines and models:*"]
        for k in sorted(kind_map.keys()):
            engines = kind_map.get(k) or []
            if not engines:
                continue
            lines.append(f"\n{k}:")
            for e in sorted(engines):
                current = _get_engine_current_model(e)
                lines.append(f"• `{k}/{e}`")
                models = _get_engine_models(e)
                if models:
                    for m in models:
                        marker = " ✅" if m == current else ""
                        lines.append(f"    - `{m}`{marker}")
                else:
                    lines.append("    - _no models available_")
        return "\n".join(lines)

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

        # optionally show live override when configured and different from base
        try:
            from core.config import config_registry

            live_override = config_registry.get_value("LIVE_CORTEX", "Default")
        except Exception:
            live_override = "Default"

        lines: list[str] = [
            f"*Active Cortex engine:* `{base}`",
            f"• trainer override: `{trainer_display}`",
            f"• grillo override: `{grillo_display}`",
        ]
        if (
            live_override
            and live_override not in ("Default", "", None)
            and live_override != base
        ):
            lines.append(f"• live override: `{live_override}`")

        lines.append("")
        lines.append("*Available Cortex engines:*")
        has_engines = False
        for k in sorted(kind_map.keys()):
            engines = kind_map.get(k) or []
            if not engines:
                continue
            has_engines = True
            for e in sorted(engines):
                marker = " ✅" if e == base else ""
                lines.append(f"• `{k}/{e}`{marker}")
        if not has_engines:
            lines.append("_No engines registered._")

        lines.append("")
        lines.append("To switch: `/cortex <engine>` or `/cortex <engine> <model>`")
        lines.append("To override grillo: `/cortex_grillo <engine>`")
        lines.append("To override trainer: `/cortex_trainer <engine>`")
        return "\n".join(lines)

    choice_raw = str(args[0]).strip()
    model_arg = str(args[1]).strip() if len(args) >= 2 else None

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
    except Exception as e:
        return f"❌ Error loading plugin: {e}"

    # Optionally apply a specific model on the newly-selected engine.
    if model_arg:
        try:
            applied_model = await _apply_cortex_model(selected_engine, model_arg)
        except ValueError as ve:
            return (
                f"✅ Cortex engine dynamically updated to `{selected_engine}`, "
                f"but the model was not changed.\n{ve}"
            )
        return (
            f"✅ Cortex engine dynamically updated to `{selected_engine}` "
            f"model `{applied_model}`."
        )

    # No explicit model: include the engine's current/default model when known.
    current_model = _get_engine_current_model(selected_engine)
    if current_model:
        return (
            f"✅ Cortex engine dynamically updated to `{selected_engine}` "
            f"model `{current_model}`."
        )
    return f"✅ Cortex engine dynamically updated to `{selected_engine}`."


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


# ---------------------------------------------------------------------------
# Media subsystem commands: /vox /auris /iris /live
#
# All four mirror the /cortex UX:
#   /<cmd>                    -> list every registered engine (active marked)
#   /<cmd> <engine>           -> list that engine's models (current marked)
#   /<cmd> <engine> <model>   -> switch the active engine AND its model
#
# Engines come from the per-subsystem registry (VOX/AURIS/IRIS/LIVE_REGISTRY).
# Models come from the external-endpoint registry (available_models); local
# engines that are not external endpoints simply expose no models.
# ---------------------------------------------------------------------------


async def _media_endpoint_models(engine_name: str) -> tuple[list[str], str | None]:
    """Return ``(available_models, default_model)`` for an external endpoint.

    Returns ``([], None)`` when *engine_name* is not an external endpoint
    (e.g. a bundled local engine) or on any lookup failure.
    """
    try:
        from core.external_endpoints.registry import get_external_endpoint_registry

        endpoint = await get_external_endpoint_registry().get_endpoint_by_name(
            engine_name
        )
    except Exception:
        return [], None
    if endpoint is None:
        return [], None
    return list(endpoint.available_models or []), endpoint.default_model


async def _media_set_endpoint_model(engine_name: str, model: str) -> None:
    """Persist *model* as the default for external endpoint *engine_name*.

    Silently no-ops when the engine is not an external endpoint.
    """
    try:
        from core.external_endpoints.registry import get_external_endpoint_registry

        registry = get_external_endpoint_registry()
        endpoint = await registry.get_endpoint_by_name(engine_name)
        if endpoint is not None:
            await registry.set_default_model(endpoint.id, model)
    except Exception:
        pass


async def _media_command(
    label: str,
    config_key: str,
    engines: list[str],
    default_engine: str,
    args: tuple,
) -> str:
    """Shared handler for the /vox /auris /iris /live commands.

    Args:
        label:          Human-readable subsystem name for headings.
        config_key:     Config registry key holding the active engine.
        engines:        Registered engine names for this subsystem.
        default_engine: Fallback value if the config key is unset.
        args:           Raw command arguments.
    """
    from core.config import config_registry

    try:
        active = str(
            config_registry.get_value(config_key, default_engine, value_type=str)
        )
    except Exception:
        active = default_engine

    # No-arg -> list every registered engine, marking the active one.
    if not args:
        lines = [
            f"*Active {label} engine:* `{active}`",
            "",
            f"*Available {label} engines:*",
        ]
        if not engines:
            lines.append("_No engines registered._")
        else:
            for name in sorted(engines):
                marker = " ✅" if name == active else ""
                lines.append(f"• `{name}`{marker}")
        lines.append("")
        lines.append(
            f"To switch: `/{label.lower()} <engine>` or `/{label.lower()} <engine> <model>`"
        )
        return "\n".join(lines)

    engine_arg = str(args[0]).strip()
    model_arg = str(args[1]).strip() if len(args) >= 2 else None

    # Resolve the engine name (exact, else case-insensitive substring).
    if engine_arg in engines:
        engine_name = engine_arg
    else:
        candidates = [e for e in engines if engine_arg.lower() in e.lower()]
        exact = [e for e in candidates if e.lower() == engine_arg.lower()]
        if len(exact) == 1:
            engine_name = exact[0]
        elif len(candidates) == 1:
            engine_name = candidates[0]
        elif len(candidates) > 1:
            hint = "\n".join(f"/{label.lower()} {c}" for c in sorted(candidates))
            return (
                f"❌ Found multiple matching {label} engines for "
                f"'{engine_arg}'. Which one did you mean?\n{hint}"
            )
        else:
            avail = ", ".join(f"`{e}`" for e in sorted(engines)) or "_(none)_"
            return f"❌ {label} engine `{engine_arg}` not found. Available: {avail}"

    models, current_model = await _media_endpoint_models(engine_name)

    # `/<cmd> <engine>` with no model argument.
    if model_arg is None:
        # Engines without selectable models (e.g. bundled local engines like
        # kitten) have nothing to pick, so switch directly instead of showing
        # an empty model list that would otherwise be a dead end.
        if not models:
            try:
                await config_registry.set_value(config_key, engine_name)
            except Exception as exc:
                return f"❌ Failed to switch {label} engine: {exc}"
            return (
                f"✅ {label} engine switched to `{engine_name}`.\n"
                f"_Note: media engines are applied on next use; a restart guarantees a full re-sync._"
            )

        # Engine has selectable models -> list them for the user to choose.
        lines = [f"*Models for `{engine_name}` ({label}):*"]
        for m in models:
            marker = " ✅" if m == current_model else ""
            lines.append(f"• `{m}`{marker}")
        lines.append("")
        lines.append(f"To switch: `/{label.lower()} {engine_name} <model>`")
        return "\n".join(lines)

    # `/<cmd> <engine> <model>` -> switch active engine AND model.
    if models and model_arg not in models:
        avail = ", ".join(f"`{m}`" for m in models)
        return (
            f"❌ Model `{model_arg}` not available for `{engine_name}`. "
            f"Available: {avail}"
        )

    try:
        await config_registry.set_value(config_key, engine_name)
    except Exception as exc:
        return f"❌ Failed to switch {label} engine: {exc}"

    await _media_set_endpoint_model(engine_name, model_arg)

    return (
        f"✅ {label} engine switched to `{engine_name}` model `{model_arg}`.\n"
        f"_Note: media engines are applied on next use; a restart guarantees a full re-sync._"
    )


async def vox_command(*args) -> str:
    """Show or switch the active Vox (TTS) engine and model.

    Usage:
      `/vox`                    -> list registered TTS engines (active marked)
      `/vox <engine>`           -> list that engine's models
      `/vox <engine> <model>`   -> switch active engine and model
    """
    from core.vox_registry import VOX_REGISTRY

    return await _media_command(
        label="Vox",
        config_key="ACTIVE_VOX_ENGINE",
        engines=VOX_REGISTRY.get_available_engines(),
        default_engine="disabled",
        args=args,
    )


async def auris_command(*args) -> str:
    """Show or switch the active Auris (STT) engine and model.

    Usage:
      `/auris`                    -> list registered STT engines (active marked)
      `/auris <engine>`           -> list that engine's models
      `/auris <engine> <model>`   -> switch active engine and model
    """
    from core.auris_registry import AURIS_REGISTRY

    return await _media_command(
        label="Auris",
        config_key="ACTIVE_AURIS_ENGINE",
        engines=AURIS_REGISTRY.get_available_engines(),
        default_engine="disabled",
        args=args,
    )


async def iris_command(*args) -> str:
    """Show or switch the active Iris (vision) engine and model.

    Usage:
      `/iris`                    -> list registered vision engines (active marked)
      `/iris <engine>`           -> list that engine's models
      `/iris <engine> <model>`   -> switch active engine and model
    """
    from core.iris_registry import IRIS_REGISTRY

    return await _media_command(
        label="Iris",
        config_key="ACTIVE_IRIS_ENGINE",
        engines=IRIS_REGISTRY.get_available_engines(),
        default_engine="disabled",
        args=args,
    )


async def live_command(*args) -> str:
    """Show or switch the active Live (real-time audio) engine and model.

    Usage:
      `/live`                    -> list registered live engines (active marked)
      `/live <engine>`           -> list that engine's models
      `/live <engine> <model>`   -> switch active engine and model
    """
    from core.live_registry import LIVE_REGISTRY

    return await _media_command(
        label="Live",
        config_key="LIVE_CORTEX",
        engines=LIVE_REGISTRY.get_available_engines(),
        default_engine="disabled",
        args=args,
    )


register_command("vox", vox_command)
register_command("auris", auris_command)
register_command("iris", iris_command)
register_command("live", live_command)


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
    from core.interface_paths import build_pretty_name, get_recent_interface_paths

    entries = await get_recent_interface_paths(10)
    if not entries:
        return "⚠️ No recent chat found."

    lines = []
    for item in entries:
        path = item.get("interface_path")
        # Re-derive the pretty name fresh: stored segment labels may have been
        # updated after the row was last touched, and cached labels can be stale.
        display = path
        if path:
            try:
                pretty = await build_pretty_name(path, use_cache=False)
                display = pretty.get("display") or path
            except Exception:
                display = item.get("display") or path
        lines.append(f"{display} — `{path}`")
    return "🕔 Last active chats:\n\n" + "\n".join(lines)


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


async def task_command(*args, interface_context=None) -> str:
    """List agent tasks, or resume a paused one.

    Usage:
        /task                 – list the most recent agent tasks
        /task list            – same as above
        /task resume <id>     – resume the paused (pending) task <id>
    """
    from core.agent_core import AgentLoopManager

    manager = AgentLoopManager()

    sub = args[0].lower() if args else "list"

    # --- Resume a specific task -------------------------------------------
    if sub == "resume":
        if len(args) < 2:
            return "❌ Usage: `/task resume <id>`"
        try:
            task_id = int(args[1])
        except (TypeError, ValueError):
            return f"❌ Invalid task id: `{args[1]}`"

        resumable = await manager.find_task_by_id(task_id)
        if not resumable:
            return (
                f"⚠️ Task `{task_id}` is not resumable "
                "(unknown id or not in `pending` state)."
            )

        # Derive interface_path so the resumed turn's final reply is delivered
        # back to the interface that issued the command.
        interface_path = None
        interface_name = None
        bot = None
        message = None
        if isinstance(interface_context, dict):
            interface_path = interface_context.get("interface_path")
            interface_name = interface_context.get("interface_id")
            bot = interface_context.get("bot")
            message = interface_context.get("message")

        context: Dict[str, Any] = {}
        if interface_path:
            context["interface_path"] = interface_path
        if interface_name:
            context["interface_id"] = interface_name

        from core.agent_router import start_task_resume

        scheduled = start_task_resume(
            task_id, context=context, bot=bot, message=message
        )
        if not scheduled:
            return f"❌ Could not schedule resume for task `{task_id}`."
        goal = resumable.get("goal") or ""
        preview = (goal[:120] + "…") if len(goal) > 120 else goal
        return (
            f"▶️ Resuming task `{task_id}`. It runs in the background and its "
            f"result will be delivered here when ready.\n_Goal:_ {preview}"
        )

    # --- List recent tasks -------------------------------------------------
    tasks = await manager.list_recent_tasks(limit=15)
    if not tasks:
        return "📭 No agent tasks found."

    status_icon = {
        "pending": "⏸️",
        "running": "▶️",
        "waiting_for_approval": "⏳",
        "paused": "⏸️",
        "completed": "✅",
        "failed": "❌",
        "cancelled": "🚫",
    }

    def _truncate(text: str, limit: int = 80) -> str:
        text = " ".join(str(text).split())
        return text[: limit - 1] + "…" if len(text) > limit else text

    lines = ["🗂️ *Agent tasks* (newest first)\n"]
    has_resumable = False
    for t in tasks:
        icon = status_icon.get(t["status"], "•")
        if t.get("resumable"):
            has_resumable = True

        # Header: id + icon + status.
        header = f"`{t['task_id']}` {icon} *{t['status']}*"
        lines.append(header)

        # Description: best human-readable label. Fall back to a short reason
        # for failed tasks, else a neutral placeholder.
        label = _truncate(t.get("name") or "")
        if not label:
            if t["status"] == "failed" and t.get("stop_reason"):
                label = f"_{_truncate(t['stop_reason'], 60)}_"
            else:
                label = "_(no description)_"
        lines.append(f"    {label}")

        # Metrics line, only when we have concrete numbers.
        actions = t.get("actions_executed")
        iters = t.get("iterations")
        metrics: list[str] = []
        if isinstance(actions, int) and actions > 0:
            metrics.append(f"{actions} action{'s' if actions != 1 else ''}")
        if isinstance(iters, int) and iters > 0:
            metrics.append(f"{iters} iteration{'s' if iters != 1 else ''}")
        if metrics:
            lines.append(f"    _{' · '.join(metrics)}_")

        lines.append("")  # blank spacer between tasks

    if has_resumable:
        lines.append("_Use_ `/task resume <id>` _to continue a paused task._")
    return "\n".join(lines).rstrip()


def _iter_plugin_components() -> list:
    """Return the ComponentInfo records tracked as plugins.

    Interfaces, cortex engines and core meta-components are excluded so the
    listing matches the WebUI *Plugins* tab.
    """
    from core.core_initializer import core_initializer

    plugins = []
    for info in core_initializer.components.values():
        if getattr(info, "type", "") == "plugin":
            plugins.append(info)
    return plugins


def _plugin_display_label(short_name: str) -> str:
    """Best-effort human-readable name for a plugin short name.

    Prefers the live instance's ``display_name`` / ``get_metadata`` name, then
    falls back to a prettified short name so listings stay readable even for
    disabled (ghost) plugins whose instance is gone.
    """
    from core.core_initializer import PLUGIN_REGISTRY

    instance = PLUGIN_REGISTRY.get(short_name)
    if instance is not None:
        getter = getattr(instance, "get_metadata", None)
        if callable(getter):
            try:
                meta = getter()
                if isinstance(meta, dict):
                    label = meta.get("display_name")
                    if isinstance(label, str) and label.strip():
                        return label.strip()
            except Exception:
                pass
        label = getattr(instance, "display_name", None)
        if isinstance(label, str) and label.strip():
            return label.strip()
    return short_name.replace("_", " ").title()


def _resolve_plugin_short_name(user_name: str) -> str | None:
    """Map a user-supplied plugin name to its canonical short name.

    A plugin's runtime short name is its module stem (e.g. ``radio_host_plugin``
    for ``plugins/radio_host/radio_host_plugin.py``) — but a user may type the
    registration alias (``radio_host``), the module stem, or the display name.
    Resolution order (all case-insensitive): exact short name → ``<name>_plugin``
    → registry alias whose instance matches a tracked plugin → display name.
    Returns ``None`` if nothing matches.
    """
    from core.core_initializer import PLUGIN_REGISTRY

    if not user_name:
        return None
    target = user_name.strip().lower()

    tracked = {
        info.name.lower(): info.name
        for info in _iter_plugin_components()
        if getattr(info, "name", "")
    }

    # 1) Exact short name.
    if target in tracked:
        return tracked[target]

    # 2) <name>_plugin module-stem convention.
    if f"{target}_plugin" in tracked:
        return tracked[f"{target}_plugin"]

    # 3) Registry alias → matching tracked plugin instance.
    alias_instance = PLUGIN_REGISTRY.get(user_name.strip()) or PLUGIN_REGISTRY.get(
        target
    )
    if alias_instance is not None:
        for short_name in tracked.values():
            if PLUGIN_REGISTRY.get(short_name) is alias_instance:
                return short_name

    # 4) Display name match.
    for short_name in tracked.values():
        if _plugin_display_label(short_name).lower() == target:
            return short_name

    return None


async def plugin_command(*args, interface_context=None) -> str:
    """List plugins or enable/disable one at runtime.

    Usage:
        /plugin                       – list all plugins with their state
        /plugins                      – same as above (alias)
        /plugin list                  – same as above
        /plugin enable <name>         – enable a plugin (no restart)
        /plugin disable <name>        – disable a plugin (no restart)
    """
    from core.core_initializer import core_initializer, PLUGIN_REGISTRY

    sub = args[0].lower() if args else "list"

    # --- List -------------------------------------------------------------
    if sub in ("list", "ls"):
        plugins = _iter_plugin_components()
        if not plugins:
            return "🧩 No plugins are currently tracked."

        rows = []
        for info in sorted(plugins, key=lambda i: getattr(i, "name", "")):
            short_name = getattr(info, "name", "")
            enabled = short_name in PLUGIN_REGISTRY
            led = "✅" if enabled else "🔻"
            core_tag = (
                " _(core)_" if core_initializer.is_core_plugin(short_name) else ""
            )
            label = _plugin_display_label(short_name)
            rows.append(f"{led} `{short_name}` – {label}{core_tag}")

        enabled_count = sum(
            1 for i in plugins if getattr(i, "name", "") in PLUGIN_REGISTRY
        )
        header = (
            f"🧩 *Plugins* ({enabled_count}/{len(plugins)} enabled)\n"
            "_Use_ `/plugin enable|disable <name>` _to manage them._\n\n"
        )
        return header + "\n".join(rows)

    # --- Enable / Disable -------------------------------------------------
    if sub in ("enable", "disable"):
        if len(args) < 2:
            return f"❌ Usage: `/plugin {sub} <name>`"

        user_name = " ".join(args[1:]).strip()
        short_name = _resolve_plugin_short_name(user_name)
        if not short_name:
            return (
                f"⚠️ Unknown plugin: `{user_name}`. "
                "Use `/plugin list` to see available plugins."
            )

        if core_initializer.is_core_plugin(short_name):
            return f"❌ Plugin `{short_name}` is a core plugin and cannot be disabled."

        try:
            if sub == "enable":
                result = await core_initializer.enable_plugin(short_name)
            else:
                result = await core_initializer.disable_plugin(short_name)
        except Exception as exc:
            log_debug(f"[command_registry] plugin {sub} failed for {short_name}: {exc}")
            return f"❌ Error toggling plugin `{short_name}`: {exc}"

        if not result.get("ok"):
            reason = result.get("error", "toggle_failed")
            return f"⚠️ Could not {sub} `{short_name}`: {reason}"

        state = "enabled ✅" if result.get("enabled") else "disabled 🔻"
        label = _plugin_display_label(short_name)
        return f"🧩 Plugin `{short_name}` ({label}) is now {state}."

    return (
        "❌ Usage:\n"
        "`/plugin` or `/plugin list` – list plugins\n"
        "`/plugin enable <name>` – enable a plugin\n"
        "`/plugin disable <name>` – disable a plugin"
    )


register_command("cancel", cancel_command)
register_command("logchat", logchat_command)
register_command("task", task_command)


async def vessel_command(*args) -> str:
    """Inspect the Rift Vessel embodiment layer.

    Usage:
        /vessel status   Show active connector + available connectors.
    """
    sub = args[0].lower() if args else "status"

    if sub != "status":
        return "❌ Use: `/vessel status`"

    try:
        from core.config_manager import config_registry
        from core.vessel_registry import VESSEL_REGISTRY

        active = config_registry.get_value("ACTIVE_VESSEL", "disabled") or "disabled"
        available = VESSEL_REGISTRY.get_available_connectors()

        lines = ["🌀 *Rift Vessel*", f"Active: `{active}`"]
        if available:
            lines.append("Available connectors:")
            for name in available:
                meta = VESSEL_REGISTRY.get_connector_meta(name) or {}
                label = meta.get("label") or name
                lines.append(f"  • `{name}` — {label}")
        else:
            lines.append("_No connectors registered._")

        instance = (
            VESSEL_REGISTRY.get_instance(str(active)) if active != "disabled" else None
        )
        if instance is not None:
            connected = bool(getattr(instance, "is_connected", False))
            lines.append(f"Connection: {'🟢 connected' if connected else '⚪ idle'}")

        return "\n".join(lines)
    except Exception as e:
        log_debug(f"[command_registry] Error in vessel_command: {e}")
        return f"❌ Error handling vessel command: {e}"


async def minecraft_command(*args) -> str:
    """Control the Minecraft Vessel bridge provisioner.

    Usage:
        /minecraft provision start        Install (if needed) and start the bridge.
        /minecraft provision stop         Stop the bridge subprocess.
        /minecraft provision status       Show bridge status.
        /minecraft provision logs [n]     Show the last n bridge log lines.
    """
    usage = "❌ Use: `/minecraft provision start|stop|status|logs [n]`"
    if len(args) < 2 or args[0].lower() != "provision":
        return usage

    action = args[1].lower()

    try:
        from interface.minecraft_provisioner import get_bridge_provisioner

        prov = get_bridge_provisioner()

        if action == "start":
            res = await prov.start()
        elif action == "stop":
            res = await prov.stop()
        elif action == "status":
            res = prov.status()
        elif action == "logs":
            n = 100
            if len(args) >= 3:
                try:
                    n = int(args[2])
                except ValueError:
                    return "❌ logs count must be an integer"
            res = prov.logs(n)
        else:
            return usage

        if action == "status":
            return (
                "🟦 *Minecraft bridge*\n"
                f"Enabled: `{res.get('enabled')}`\n"
                f"Installed: `{res.get('installed')}`\n"
                f"Running: `{res.get('running')}`"
                + (f" (pid {res.get('pid')})" if res.get("running") else "")
            )
        if action == "logs":
            if not res.get("ok"):
                return f"❌ {res.get('detail')}"
            body = "\n".join(res.get("lines", [])) or "(empty)"
            return f"🟦 *Minecraft bridge logs*\n```\n{body}\n```"

        ok = res.get("ok")
        icon = "✅" if ok else "❌"
        return f"{icon} Minecraft bridge {action}: {res.get('detail')}"
    except Exception as e:
        log_debug(f"[command_registry] Error in minecraft_command: {e}")
        return f"❌ Error handling minecraft command: {e}"


register_command("vessel", vessel_command)
register_command("minecraft", minecraft_command)
register_command("plugin", plugin_command)
register_command("plugins", plugin_command)
