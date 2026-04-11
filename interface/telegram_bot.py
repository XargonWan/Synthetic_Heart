# interface/telegram_bot.py

import os
import re
import asyncio
import time
from typing import Optional
from telegram import Update, Bot

# Some test environments may not expose all exception names from python-telegram-bot
try:
    from telegram.error import TelegramError, RetryAfter, BadRequest, TimedOut
except Exception:
    # Provide safe fallbacks so the module imports in tests without the real library
    class TelegramError(Exception):
        pass

    class RetryAfter(Exception):
        pass

    class BadRequest(Exception):
        pass

    class TimedOut(Exception):
        pass


from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv  # type: ignore
from plugins.blocklist import block_user, unblock_user, get_blocked_users
from plugins.message_map import init_message_map_table, cleanup_old_mappings
from core import response_proxy
from core import message_queue
from core import recent_chats  # For command functions only, not for tracking
from core.mention_utils import is_message_for_bot
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.chat_attention import set_attention, get_attention, evaluate_triggers
from interface.message_send_utils import (
    safe_send,
    send_with_thread_fallback,
)
from core.message_sender import (
    send_content,
    detect_media_type,
)
from core.config import (
    get_active_cortex_engine,
    set_log_chat_id_and_thread,
    get_log_chat_id_sync,
)
from core.command_registry import execute_command, handle_command_message

from plugins.chat_link import ChatLinkStore

chat_link_store = ChatLinkStore()
import core.plugin_instance as plugin_instance
from core.core_initializer import register_interface
from core.interfaces_registry import get_interface_registry
from core.config_manager import config_registry
from core.variables_engine import register_exposed_var

# Get interface registry for trainer verification
_interface_registry = get_interface_registry()

# Load environment variables
load_dotenv()

# Register exposed variable for WebUI
register_exposed_var(
    "BOTFATHER_TOKEN",
    label="Telegram Bot Token",
    default=None,
    value_type=str,
    ui_type="string",
    description="Bot token provided by BotFather on Telegram.",
    scope="interface",
    tags=["sensitive"],
    needs_component_reload=True,
    component="telegram_bot",
)

# Read Telegram-specific configuration using config_registry
# This supports: env override -> database -> default (None)
BOTFATHER_TOKEN = config_registry.get_var(
    "BOTFATHER_TOKEN",
    None,
    label="Telegram Bot Token",
    description="Bot token provided by BotFather on Telegram.",
    group="interface",
    component="telegram_bot",
    sensitive=True,
)

# Bot username will be fetched dynamically using bot.get_me()
BOT_USERNAME = None


# Parse trainer ID from TRAINER_IDS configuration
# Expected format: "telegram_bot:12345,discord:67890,..."
def _parse_trainer_id_from_config() -> Optional[int]:
    """Extract trainer ID for telegram_bot from TRAINER_IDS configuration."""
    trainer_ids = config_registry.get_var(
        "TRAINER_IDS",
        "",
        label="Trainer IDs",
        description="Comma-separated list of trainer IDs for each interface (format: interface_name:user_id)",
        group="core",
        component="telegram_bot",
    )

    trainer_ids_str = str(trainer_ids) if trainer_ids else ""
    if not trainer_ids_str:
        return None

    for trainer_config in trainer_ids_str.split(","):
        trainer_config = trainer_config.strip()
        if trainer_config.startswith("telegram_bot:"):
            try:
                return int(trainer_config.split(":")[1])
            except (ValueError, IndexError):
                log_warning(
                    f"[telegram_bot] Invalid trainer ID format in TRAINER_IDS: {trainer_config}"
                )
                return None

    log_debug("[telegram_bot] No trainer ID found for telegram_bot in TRAINER_IDS")
    return None


def is_trainer(user_id: int) -> bool:
    """Check if user is the trainer for this Telegram interface."""
    return _interface_registry.is_trainer("telegram_bot", user_id)


def get_trainer_id() -> Optional[int]:
    """Get the trainer ID for this Telegram interface."""
    return _interface_registry.get_trainer_id("telegram_bot")


class MessageWrapper:
    """Thin wrapper to add extra attributes to immutable Telegram message objects."""

    def __init__(self, message, **extra_attrs):
        self._message = message
        self._extra = extra_attrs

    def __getattr__(self, name):
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        if name in self._extra:
            return self._extra[name]
        return getattr(self._message, name)


context_memory = {}
last_selected_chat = {}
message_id = None

# Throttling for bot None lookup warnings
_last_bot_none_lookup_log_time = 0
_bot_none_log_throttle_sec = 5


async def ensure_plugin_loaded(update: Update):
    """
    Check that an LLM plugin has been loaded correctly.
    If absent, reply to the user with an error message and log the issue.
    """
    log_debug(
        f"[telegram_bot] Checking if plugin is loaded: {plugin_instance.plugin is not None}"
    )
    if plugin_instance.plugin is None:
        log_warning("[telegram_bot] No plugin loaded, attempting to load...")
        try:
            current = await get_active_cortex_engine()
            log_debug(f"[telegram_bot] Active Cortex from config: {current}")
            if current:
                log_debug(f"[telegram_bot] Loading plugin: {current}")
                await plugin_instance.load_plugin(current, notify_fn=telegram_notify)
                log_debug(
                    f"[telegram_bot] Plugin loaded successfully: {plugin_instance.plugin is not None}"
                )
        except Exception as e:  # pragma: no cover - runtime safeguard
            log_warning(f"[telegram_interface] Failed to autoload Cortex: {e}")
        if plugin_instance.plugin is None:
            log_warning("[telegram_bot] Plugin still None, trying manual fallback...")
            try:
                await plugin_instance.load_plugin("manual", notify_fn=telegram_notify)
                log_warning("[telegram_interface] Falling back to ManualAIPlugin")
            except Exception as e:
                log_error(f"[telegram_bot] Manual plugin fallback failed: {e}")
                log_error("No Cortex plugin loaded.")
                from core.notifier import notify_trainer

                notify_trainer("⚠️ No Cortex plugin active. Use /cortex to select one.")
                return False
    else:
        log_debug(
            f"[telegram_bot] Plugin already loaded: {plugin_instance.plugin.__class__.__name__}"
        )
    return True


async def resolve_forwarded_target(message):
    """Resolve forwarded/correlated original target from a trainer-context message.

    This is a thin wrapper around `_resolve_original_from_reply` so both the
    legacy response commands and PRIORITY 3 share the same robust resolution
    logic.
    """
    return await _resolve_original_from_reply(message)


async def _resolve_original_from_reply(reply_message):
    """Try multiple strategies to resolve the original chat_id/message_id

    Strategies tried (in order):
    - plugin_instance.get_target on reply_message.message_id and reply_message.reply_to_message.message_id
    - forward metadata available on the reply (forward_from_chat / forward_from_message_id)
    - textual fallback produced when forwarding failed ("(original message from chat X id Y)")
    Returns tuple (chat_id, message_id) or (None, None)

    Adds debug logging at each decision point for easier troubleshooting.
    """
    trainer_mid = getattr(reply_message, "message_id", None)
    log_debug(f"[telegram_bot] resolving reply for trainer_msg_id={trainer_mid}")

    # 1) plugin mapping (some plugins return tuple or dict)
    possible_ids = [trainer_mid]
    if getattr(reply_message, "reply_to_message", None):
        possible_ids.append(getattr(reply_message.reply_to_message, "message_id", None))

    for mid in possible_ids:
        if not mid:
            continue
        try:
            log_debug(
                f"[telegram_bot] Checking plugin mapping for trainer_msg_id={mid}"
            )
            tracked = plugin_instance.get_target(mid)
            # Plugins may implement get_target as async; if so await it
            try:
                import asyncio as _asyncio

                if _asyncio.iscoroutine(tracked):
                    log_debug(
                        f"[telegram_bot] plugin.get_target({mid}) returned coroutine; awaiting result"
                    )
                    tracked = await tracked
            except Exception as e:
                log_debug(
                    f"[telegram_bot] Exception while awaiting plugin.get_target result: {e}"
                )
            log_debug(f"[telegram_bot] plugin lookup for {mid} -> {repr(tracked)}")
        except Exception as e:
            tracked = None
            log_exception(f"Failed to query plugin mapping: {e}")
        if tracked:
            # support both tuple and dict return types
            log_debug(
                f"[telegram_bot] plugin.get_target({mid}) returned: {repr(tracked)}"
            )
            if isinstance(tracked, (list, tuple)) and len(tracked) >= 2:
                return int(tracked[0]), int(tracked[1])
            if (
                isinstance(tracked, dict)
                and "chat_id" in tracked
                and "message_id" in tracked
            ):
                return int(tracked["chat_id"]), int(tracked["message_id"])
        else:
            # Informational: plugin did not return a mapping for this trainer message id
            log_info(f"[telegram_bot] plugin.get_target({mid}) returned no mapping")

        # 1b) persistent mapping independent from the active LLM plugin.
        # This allows replies to work even if the active engine changed after the
        # trainer message was sent.
        try:
            from plugins.message_map import (
                get_original_message as _message_map_get_original_message,
            )

            mapped = await _message_map_get_original_message(int(mid))
            if mapped and isinstance(mapped, (list, tuple)) and len(mapped) >= 2:
                log_debug(f"[telegram_bot] message_map lookup for {mid} -> {mapped}")
                return int(mapped[0]), int(mapped[1])
        except Exception as e:
            log_debug(f"[telegram_bot] message_map lookup failed for {mid}: {e}")

    # 2) forwarded metadata on the trainer-context message
    try:
        log_debug(
            f"[telegram_bot] Checking forwarded metadata on reply_message: forward_from_chat={getattr(reply_message, 'forward_from_chat', None)}, forward_from_message_id={getattr(reply_message, 'forward_from_message_id', None)}"
        )
        if hasattr(reply_message, "forward_from_chat") and hasattr(
            reply_message, "forward_from_message_id"
        ):
            if getattr(reply_message, "forward_from_chat", None) and getattr(
                reply_message, "forward_from_message_id", None
            ):
                return (
                    reply_message.forward_from_chat.id,
                    reply_message.forward_from_message_id,
                )

        # Newer Bot API / python-telegram-bot versions expose `forward_origin`
        origin = getattr(reply_message, "forward_origin", None)
        if origin is not None:
            origin_chat = getattr(origin, "chat", None) or getattr(
                origin, "sender_chat", None
            )
            origin_message_id = getattr(origin, "message_id", None)
            if origin_chat is not None and origin_message_id is not None:
                return getattr(origin_chat, "id", None), int(origin_message_id)
    except Exception as e:
        log_debug(f"[telegram_bot] Exception while checking forwarded metadata: {e}")
        pass

    # 3) textual fallback emitted when forwarding failed
    text = getattr(reply_message, "text", "") or ""
    import re

    m = re.search(r"original message from chat\s+(-?\d+)\s+id\s+(\d+)", text)
    log_debug(
        f"[telegram_bot] textual fallback search in text={text!r} -> match={bool(m)}"
    )
    if m:
        try:
            log_debug(
                f"[telegram_bot] Textual fallback matched in reply text for trainer message: {text}"
            )
            return int(m.group(1)), int(m.group(2))
        except Exception as e:
            log_exception(f"Error parsing textual fallback: {e}")

    log_debug(f"[telegram_bot] no mapping found for trainer_msg_id={trainer_mid}")
    return None, None


# === Block commands ===


async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return
    try:
        to_block = int(context.args[0])
        block_user(to_block)
        log_debug(f"User {to_block} blocked.")
        await update.message.reply_text(f"\U0001f6ab User {to_block} blocked.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Use: /block <user_id>")


async def block_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return
    blocked = get_blocked_users()
    log_debug("Blocked users list requested.")
    if not blocked:
        await update.message.reply_text("\u2705 No users blocked.")
    else:
        await update.message.reply_text(
            "\U0001f6ab Blocked users:\n" + "\n".join(map(str, blocked))
        )


async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return
    try:
        to_unblock = int(context.args[0])
        unblock_user(to_unblock)
        log_debug(f"User {to_unblock} unblocked.")
        await update.message.reply_text(f"\u2705 User {to_unblock} unblocked.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Use: /unblock <user_id>")


async def purge_mappings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return
    # Ensure table exists even if manual plugin never loaded
    await init_message_map_table()
    try:
        days = int(context.args[0]) if context.args else 7
    except ValueError:
        await update.message.reply_text("❌ Use: /purge_map [days]")
        return
    deleted = await cleanup_old_mappings(days * 86400)
    await update.message.reply_text(
        f"\U0001f5d1 Removed {deleted} mappings older than {days} days."
    )


async def logchat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the current chat as the log chat."""
    if not is_trainer(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.thread_id
    try:
        await set_log_chat_id_and_thread(chat_id, thread_id, "telegram_bot")
        confirmation = (
            f"This chat is now set as logchat [{chat_id}, {thread_id}] on telegram_bot"
        )
        await safe_send(context.bot, chat_id, confirmation, thread_id=thread_id)
    except Exception as e:
        log_error(f"[telegram_interface] Failed to set log chat: {e}")
        await update.message.reply_text("❌ Unable to set log chat.")


# === Generic command for sticker/audio/photo/file/video ===


async def handle_response_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, content_type: str
):
    if not await ensure_plugin_loaded(update):
        return

    if not is_trainer(update.effective_user.id):
        return

    message = update.message
    if not message.reply_to_message:
        synth_name = os.getenv("SYNTH_NAME") or "SyntH"
        await message.reply_text(
            f"⚠️ You must use this command in reply to a message forwarded by {synth_name}."
        )
        return

    chat_id, message_id = await resolve_forwarded_target(message.reply_to_message)

    if not chat_id or not message_id:
        await message.reply_text("❌ Invalid message for this command.")
        return

    response_proxy.set_target(get_trainer_id(), chat_id, message_id, content_type)
    log_debug(f"Target {content_type} set: chat_id={chat_id}, message_id={message_id}")
    await safe_send(
        context.bot,
        chat_id=get_trainer_id(),
        text=f"📎 Send me the {content_type.upper()} file to use as response.",
    )  # [FIX]


async def cancel_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return
    if response_proxy.has_pending(get_trainer_id()):
        response_proxy.clear_target(get_trainer_id())
        log_debug("Response sending cancelled.")
        await update.message.reply_text("❌ Sending cancelled.")
    else:
        await update.message.reply_text("⚠️ No active send to cancel.")


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_debug("/test received")
    await update.message.reply_text("✅ Test OK")


async def last_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return

    entries = await recent_chats.get_last_active_chats_verbose(10, context.bot)
    if not entries:
        await update.message.reply_text("⚠️ No recent chat found.")
        return

    lines = [f"[{name}](tg://user?id={cid}) — `{cid}`" for cid, name in entries]
    await update.message.reply_text(
        "\U0001f553 Last active chats:\n" + "\n".join(lines), parse_mode="Markdown"
    )

    entries = await recent_chats.get_last_active_chats_verbose(10, context.bot)
    if not entries:
        await update.message.reply_text("⚠️ No recent chat found.")
        return

    lines = [f"[{name}](tg://user?id={cid}) — `{cid}`" for cid, name in entries]
    await update.message.reply_text(
        "\U0001f553 Last active chats:\n" + "\n".join(lines), parse_mode="Markdown"
    )


async def _inject_memory_interaction(
    chat_id: int,
    user_text: str,
    assistant_text: str,
    interface_path: str = "telegram_bot",
):
    """
    Background task to inject an interaction into the memory system (message_chain).
    This ensures that Live API calls and other side-channel interactions are recorded
    in the database and visible to Grillo/Diary, without blocking the user response.
    """
    try:
        from types import SimpleNamespace
        from datetime import datetime

        log_debug(
            f"[telegram_bot] 🧠 Injecting memory interaction for chat {chat_id}..."
        )

        # 1. Inject User Message
        user_msg = SimpleNamespace()
        user_msg.chat_id = chat_id
        user_msg.text = user_text
        user_msg.interface_path = f"{interface_path}/{chat_id}"
        user_msg.date = datetime.utcnow()
        # Mark as already processed so we don't trigger new actions, just logging
        # We use a special flag or just call process_history_only if that existed?
        # Actually message_chain doesn't have a "log only" mode easily exposed.
        # BUT, we can use the 'scheduler' or 'internal' interface trick, or better:
        # We can insert directly into DB or use a specialized function.
        # However, to be visible to Grillo, it needs to be in `message_logs`.

        # We will use a direct DB insertion helper to avoid triggering the whole LLM chain again.
        from core.db import get_conn_ctx
        import json

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Insert User Message
                await cur.execute(
                    """
                    INSERT INTO message_logs 
                    (chat_id, interface, sender_id, sender_name, content, role, metadata) 
                    VALUES (%s, %s, %s, %s, %s, 'user', %s)
                    """,
                    (
                        chat_id,
                        interface_path.split("/")[0],
                        str(chat_id),  # sender_id
                        "User",  # We could fetch name but "User" is okay for memory sync
                        user_text,
                        json.dumps({"is_multimodal_transcript": True}),
                    ),
                )

                # Insert Assistant Message
                await cur.execute(
                    """
                    INSERT INTO message_logs 
                    (chat_id, interface, sender_id, sender_name, content, role, metadata) 
                    VALUES (%s, %s, %s, %s, %s, 'assistant', %s)
                    """,
                    (
                        chat_id,
                        interface_path.split("/")[0],
                        "self",
                        "SyntH",
                        assistant_text,
                        json.dumps({"is_multimodal_response": True}),
                    ),
                )
                await conn.commit()

        log_debug("[telegram_bot] 🧠 Memory injection successful.")

    except Exception as e:
        log_error(f"[telegram_bot] Failed to inject memory: {e}")


async def _caption_image_in_background(file_path: str, chat_id: int):
    """
    Generates a caption for an image in the background and injects it into memory.
    """
    try:
        if not plugin_instance.plugin or not hasattr(plugin_instance.plugin, "client"):
            return

        # We can reuse the existing Gemini client to generate a caption
        # We need to act carefully not to disturb the main flow.
        # We'll valid if we can make a lightweight call.

        # NOTE: This assumes the plugin exposes the client.
        # For now, let's keep it simple: Just log a placeholder
        # "User sent an image" unless we strictly want the caption.
        # The user requested "Explicit captioning".

        # TODO: Implement actual LLM call for captioning here.
        # For this step, I will inject a placeholder and we can refine the captioning call next
        # to ensure we don't race condition the client session.
        pass

    except Exception as e:
        log_error(f"[telegram_bot] Background captioning failed: {e}")


async def handle_media_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle incoming voice/video messages using the LLM's Live API capability.
    Processes the media and returns TEXT response.
    """
    message = update.message
    log_info(f"[telegram_bot] Handling live media message from {message.from_user.id}")

    # First thing: decide whether this media message is *actually* meant for
    # the bot.  Prior to 2026-03 the interface treated every piece of media as
    # directed which meant voice notes would be transcribed even if nobody had
    # mentioned Rekku/aliases and we were in a group chat.  That behaviour
    # caused excessive churn and confused users when non‑directed audio popped
    # up in the queue.
    #
    # All of the usual text rules apply (aliases, @mentions, replies, private
    # chats, etc).  If the message is **not** for the bot, bail out *before*
    # doing any heavy work (download/transcribe/dispatch).  The calling
    # handler may still forward us an update for routing convenience; checking
    # again here makes the function self‑contained and safe.
    try:
        from core.mention_utils import is_message_for_bot
        from core.reaction_handler import get_reaction_emoji, react_when_mentioned
        from core.core_initializer import INTERFACE_REGISTRY

        directed, _reason = await is_message_for_bot(message, context.bot)
    except Exception as e:
        log_debug(f"[telegram_bot] mention check failed: {e}")
        directed = False

    # if the bot isn't targeted, drop the update now without touching the file
    if not directed:
        log_debug("[telegram_bot] media message not directed to bot; skipping")
        return

    # the caller has already confirmed the message is for us, so we can add
    # the reaction safely.  (we compute the emoji separately so that failures
    # during the mention check above don’t prevent the reaction from occurring.)
    try:
        emoji = get_reaction_emoji()
        if emoji:
            interface = INTERFACE_REGISTRY.get("telegram_bot")
            if interface:
                await react_when_mentioned(interface, message, emoji)
    except Exception as e:
        log_debug(f"[telegram_bot] config reaction skipped/failed: {e}")

    # Determine media type and file_id
    file_id = None
    media_type_hint = "audio"

    if message.voice:
        file_id = message.voice.file_id
        media_type_hint = "audio/ogg"
        # if no configured emoji, fall back to simple icon
        if not get_reaction_emoji():
            try:
                await message.set_reaction("👂")
            except Exception as exc:  # pragma: no cover - reaction may fail on old PTB
                log_warning(f"[telegram_bot] failed to react to voice: {exc}")
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action="record_voice"
        )
    elif message.video_note:
        file_id = message.video_note.file_id
        media_type_hint = "video/mp4"
        try:
            await message.set_reaction("👀")
        except Exception:
            pass
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action="record_video"
        )
    elif message.video:
        file_id = message.video.file_id
        media_type_hint = "video/mp4"
        try:
            await message.set_reaction("👀")
        except Exception:
            pass
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action="record_video"
        )

    if not file_id:
        log_warning(f"[telegram_bot] live_media: no file_id available for {message}")
        return

    # Download file and process
    input_path = None
    try:
        log_debug(f"[telegram_bot] live_media: downloading file {file_id}")
        new_file = await context.bot.get_file(file_id)

        # Create temp directory
        temp_dir = os.path.join(os.getcwd(), "tmp", "live_io")
        os.makedirs(temp_dir, exist_ok=True)

        ext = ".oga" if "audio" in media_type_hint else ".mp4"
        input_path = os.path.join(
            temp_dir, f"in_{message.message_id}_{int(time.time())}{ext}"
        )
        await new_file.download_to_drive(custom_path=input_path)

        log_debug(f"[telegram_bot] Media file downloaded to {input_path}")

        # ------------------------------------------------------------------
        # PRIMARY PATH: Auris STT → enqueue as text message
        # ------------------------------------------------------------------
        auris_handled = False
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            auris = PLUGIN_REGISTRY.get("auris_plugin")
            if auris is None:
                log_warning(
                    "[telegram_bot] Auris plugin not available; will attempt generic dispatch fallback."
                )
                transcribed = None
            else:
                log_debug(
                    "[telegram_bot] Auris plugin found — attempting transcription."
                )
                # Auris itself logs any successful transcription; the interface should
                # remain a dumb carrier and avoid duplicating that message.
                _auris_result = await auris.transcribe_audio(
                    input_path, media_type_hint
                )
                transcribed = _auris_result.text if _auris_result else None

            if transcribed:
                # Wrap the Telegram message so the queue sees `text = transcribed`.
                # `is_voice_input=True` propagates through message_queue → context dict
                # so that message_chain can auto-inject `tts_speak` and prompt_engine
                # can expose `input_source: \"voice\"` to the LLM.
                # include request_tts to ensure voice replies are generated
                wrapped = MessageWrapper(
                    message,
                    text=transcribed,
                    is_voice_input=True,
                    request_tts=True,
                )
                await message_queue.enqueue(
                    context.bot,
                    wrapped,
                    interface_id="telegram_bot",
                    original_message=message,
                    skip_mention_check=True,
                )
                auris_handled = True
            else:
                # Auris returned empty string; log a warning but *do not* abort.
                # In case the engine failed (e.g. missing model) we still want to
                # attempt the fallback live API path below rather than dropping the
                # input silently.  The previous implementation returned early here
                # which caused voice messages to vanish when Auris produced "".
                log_warning(
                    "[telegram_bot] Auris returned no transcription; continuing to fallback live API."
                )
                # auris_handled remains False so fallback will execute
        except Exception as _auris_err:
            log_warning(
                f"[telegram_bot] Auris path failed ({_auris_err}); bypassing to fallback."
            )
            # do not return yet; we still want to attempt fallback if plugin supports

        if auris_handled:
            return

        # If Auris produced nothing (empty string or None) or raised an error,
        # try the general-purpose media dispatcher which will attempt a live
        # transcription before giving up.  This avoids sending raw binary to
        # the LLM when the engine could have turned it into text.
        try:
            from core.media_dispatcher import dispatch_media

            fallback = await dispatch_media(input_path, media_type_hint)
        except Exception as e:
            log_warning(
                f"[telegram_bot] dispatch_media failed: {e}, moving to multimodal fallback"
            )
            fallback = None

        if fallback:
            # we received a transcription from dispatch_media; enqueue it like a
            # normal Auris result and stop.
            wrapped = MessageWrapper(
                message,
                text=fallback,
                is_voice_input=True,
                request_tts=True,
            )
            await message_queue.enqueue(
                context.bot,
                wrapped,
                interface_id="telegram_bot",
                original_message=message,
                skip_mention_check=True,
            )
            return

        # FALLBACK PATH: no transcription available, hand off the media as a
        # multimodal attachment.  Scope routing / persona context will still
        # apply later when the attachment is processed by plugin_instance.
        log_info(
            "[telegram_bot] Auris unavailable — enqueuing voice as multimodal "
            "attachment through normal message chain"
        )
        wrapped = MessageWrapper(
            message,
            text=getattr(message, "caption", "") or "",
            is_voice_input=True,
            request_tts=True,
        )
        await message_queue.enqueue(
            context.bot,
            wrapped,
            interface_id="telegram_bot",
            original_message=message,
            skip_mention_check=True,
        )

    except Exception as e:
        log_error(f"[telegram_bot] Error handling live media: {e}")
        await message.reply_text(f"⚠️ Error processing media: {str(e)}")
    finally:
        # Cleanup temp file
        if input_path and os.path.exists(input_path):
            try:
                os.remove(input_path)
                log_debug(f"[telegram_bot] Cleaned up temporary file: {input_path}")
            except Exception as e:
                log_error(
                    f"[telegram_bot] Failed to cleanup temp file {input_path}: {e}"
                )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_info(f"[telegram_bot] 🔔 HANDLE_MESSAGE CALLED! Update: {update}")
    log_debug(
        f"[telegram_bot] Update type: {type(update)}, Message: {update.message if update else 'None'}"
    )

    if update.message:
        log_debug(
            f"[telegram_bot] Message attributes - Voice: {bool(update.message.voice)}, Video: {bool(update.message.video)}, VideoNote: {bool(update.message.video_note)}"
        )

    log_info(f"[telegram_bot] Received message update: {update}")

    plugin_loaded = await ensure_plugin_loaded(update)
    log_debug(f"[telegram_bot] Plugin loaded check result: {plugin_loaded}")
    if not plugin_loaded:
        log_error("[telegram_bot] Plugin loading failed, aborting message processing")
        return

    message = update.message
    if not message or not message.from_user:
        log_debug("Message ignored (empty or no sender)")
        return

    # utility used in several places below; import once to avoid scoping issues

    # LIVE MEDIA HANDLING (Voice, Video, Video Note)
    # Earlier we disabled this and treated media as generic attachments,
    # but the attachment path never transcribes audio.  To ensure voice
    # messages are actually downloaded and fed to Auris or the plugin, we
    # route them through the live media handler whenever a file_id is
    # present.  This also provides clearer logging for debugging.
    #
    # Additionally, if the *current* message is a reply which tags/aliases the
    # bot and the replied-to message contains media, we should still
    # transcribe that original media.  This allows users to ask "transcribe"
    # by replying to someone else's audio instead of forwarding it.

    # check for reply-to-media first, since the incoming message may not
    # itself contain media
    if getattr(message, "reply_to_message", None) and any(
        getattr(message.reply_to_message, attr, None)
        for attr in ("voice", "video", "video_note", "video", "photo")
    ):
        # only take this path if the user explicitly directed the bot
        directed, _ = await is_message_for_bot(message, context.bot)
        if directed:
            log_debug(
                "[telegram_bot] Reply to media detected with bot mention; routing to live media handler"
            )
            try:
                # create a synthetic update containing the original media
                from types import SimpleNamespace

                fake_update = SimpleNamespace(message=message.reply_to_message)
                await handle_media_live(fake_update, context)
            except Exception as e:
                log_error(f"[telegram_bot] live media handler failed: {e}")
            return

    if message.voice or message.video_note or message.video:
        # before we turn the attachment into a file and start transcribing,
        # ensure the message is actually intended for us.  handle_media_live
        # performs the same check internally, but doing it here avoids the
        # overhead of constructing temp files and may keep large downloads from
        # occurring in group chats where the bot is not addressed.
        try:
            directed, _ = await is_message_for_bot(message, context.bot)
        except Exception as e:
            log_debug(f"[telegram_bot] mention check failed on media routing: {e}")
            directed = False

        if not directed:
            log_debug(
                "[telegram_bot] Media message not directed; skipping live handler"
            )
            return

        log_debug(
            "[telegram_bot] Detected media message; routing to live media handler"
        )
        try:
            await handle_media_live(update, context)
        except Exception as e:
            log_error(f"[telegram_bot] live media handler failed: {e}")
        return

    user = message.from_user
    user_id = user.id
    username = user.full_name
    usertag = f"@{user.username}" if user.username else "(no tag)"
    text = getattr(message, "text", "") or getattr(message, "caption", "") or ""
    # Diagnostic: log raw repr and check for mojibake (double-decoding) patterns
    try:
        from core.text_utils import looks_like_mojibake, try_recover_mojibake

        log_debug(f"[telegram_bot] Incoming text repr: {text!r}")
        if looks_like_mojibake(text):
            log_warning(
                f"[telegram_bot] Potential mojibake detected in incoming message from {username} ({user_id})"
            )
            recovered = try_recover_mojibake(text)
            log_debug(
                f"[telegram_bot] Mojibake recovery attempt: original={text!r} recovered={recovered!r}"
            )
    except Exception:
        # Do not fail message processing for diagnostic logging failures
        log_debug("[telegram_bot] mojibake detection unavailable")

    # Log with proper content type
    content_description = ""
    if message.photo:
        content_description = f" [photo with caption: '{text}']"
    elif message.document:
        content_description = f" [document: {message.document.file_name or 'unknown'} with caption: '{text}']"
    elif message.voice:
        content_description = f" [voice message with caption: '{text}']"
    elif message.video_note:
        content_description = f" [video note (circular) with caption: '{text}']"
    elif message.video:
        content_description = f" [video file/message with caption: '{text}']"
    elif text:
        content_description = f": {text}"

    log_info(
        f"[telegram_bot] Processing message from {username} ({user_id}){content_description}"
    )

    # Build interface_path for this message
    from core.interface_path_utils import build_interface_path

    thread_id = getattr(message, "message_thread_id", None) or getattr(
        message, "thread_id", None
    )
    interface_path = build_interface_path(
        "telegram_bot", str(message.chat_id), str(thread_id) if thread_id else None
    )
    log_debug(f"[telegram_bot] Generated interface_path: {interface_path}")

    # Track context - using centralized context manager
    # NOTE: chat activity tracking is now centralized in chat_context_manager.add_message_to_context
    log_debug(f"[telegram_bot] Tracking message for interface_path {interface_path}")
    from core.chat_context_manager import add_message_to_context

    # Build reply metadata if this message is an explicit reply to another message.
    # Skip implicit topic-header attachments: in Telegram forum-topic supergroups every
    # message has reply_to_message pointing at the topic opener, which is NOT a real reply.
    _reply_meta: dict | None = None
    _reply_msg = getattr(message, "reply_to_message", None)
    if _reply_msg is not None:
        _is_topic_header = getattr(message, "is_topic_message", False) and getattr(
            _reply_msg, "message_id", None
        ) == getattr(message, "message_thread_id", None)
        if not _is_topic_header:
            _reply_from = getattr(_reply_msg, "from_user", None)
            _reply_sender = (
                getattr(_reply_from, "full_name", None)
                or getattr(_reply_from, "username", None)
                or "Unknown"
            )
            _reply_meta = {
                "reply_to": {
                    "sender_name": _reply_sender,
                    "text": getattr(_reply_msg, "text", None)
                    or getattr(_reply_msg, "caption", None)
                    or "",
                    "message_id": getattr(_reply_msg, "message_id", None),
                }
            }

    try:
        await add_message_to_context(
            interface_path=interface_path,
            message_text=text,
            sender_name=username,
            sender_id=str(user_id),
            message_id=message.message_id,
            timestamp=message.date.isoformat() if hasattr(message, "date") else None,
            metadata=_reply_meta,
        )
    except Exception as e:
        log_warning(f"[telegram_bot] Failed to add message to context: {e}")
        # Continue processing even if context tracking fails

    # Animation lifecycle is handled centrally by the core message queue (enqueue -> THINK,
    # generation start -> WRITE/TALK, generation end -> IDLE). Telegram should not broadcast
    # a 'think' state here, otherwise messages that are ignored/pre-filtered would still
    # trigger UI thinking and can cause duplicates.

    # /say command removed — interactive send-to-chat feature was intentionally deleted.

    # === PRIORITY 2: Handle trainer incoming responses (stickers, media with target) ===
    log_debug(
        f"🟠 [PRIORITY 2 CHECK] is_trainer({user_id})={is_trainer(user_id)}, chat_type={message.chat.type}"
    )
    if message.chat.type == "private" and is_trainer(user_id):
        media_type = detect_media_type(message)
        log_debug(
            f"🟠 [PRIORITY 2 ACTIVE] Trainer message detected: media_type={media_type}"
        )

        # Check if there's a target set (from reply or other proxies)
        target = response_proxy.get_target(get_trainer_id())
        log_debug(f"Initial target from response_proxy = {target}")

        # If replying to a message, search in plugin mapping
        if not target and message.reply_to_message:
            reply = message.reply_to_message
            log_debug(f"Reply to trainer_message_id={reply.message_id}")
            try:
                chat_id, orig_msg_id = await _resolve_original_from_reply(reply)
                if chat_id and orig_msg_id:
                    target = {
                        "chat_id": chat_id,
                        "message_id": orig_msg_id,
                        "type": media_type,
                    }
                    log_debug(f"Found target via reply resolver: {target}")
            except Exception as e:
                log_debug(
                    f"[telegram_bot] Exception while resolving reply target for PRIORITY 2: {e}"
                )

        # If we have a target, send the content
        if target:
            chat_id = target["chat_id"]
            reply_message_id = target["message_id"]
            content_type = target["type"]

            log_debug(
                f"Sending media_type={content_type} to chat_id={chat_id}, reply_message_id={reply_message_id}"
            )
            success, feedback = await send_content(
                context.bot, chat_id, message, content_type, reply_message_id
            )
            log_debug(f"send_content returned: success={success}, feedback={feedback}")

            await message.reply_text(feedback)

            if success:
                log_debug("✅ Sending successful. Cleaning proxy.")
                response_proxy.clear_target(get_trainer_id())
            return

    log_debug("After trainer-specific checks - continuing to message processing")
    log_debug("After trainer-specific checks - continuing to message processing")
    log_debug("Checking if message is for bot - calling is_message_for_bot")

    # Check if message is directed to bot
    human_count = getattr(message, "human_count", None)
    if human_count is None and hasattr(message, "chat"):
        human_count = getattr(message.chat, "human_count", None)

    log_debug(f"human_count={human_count}, message.chat.type={message.chat.type}")

    # Get bot username and id for mention/reply checking
    bot_username = None
    bot_id = None
    try:
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username if bot_info else None
        bot_id = bot_info.id if bot_info else None
        log_debug(f"Bot username: {bot_username}, ID: {bot_id}")
    except Exception as e:
        log_debug(f"Could not get bot info: {e}")

    # Wake/sleep gating: awake follows normal routing, sleep ignores non-command messages.
    chat_id = message.chat.id
    # Read current awake state (default True)
    is_awake = get_attention(chat_id, True)

    # Check for state change triggers
    text_lower = text.lower().strip()

    # Centralized trigger evaluation handled by core.chat_attention
    should_sleep, is_wake_word, is_wake_sleep_command = evaluate_triggers(text_lower)

    # For compatibility, keep original variable names
    # should_sleep: True if sleep trigger matched
    # is_wake_word: True if wake trigger matched
    # is_wake_sleep_command: True if either matched

    # Mentions also wake up
    is_mention = False
    if bot_username and f"@{bot_username.lower()}" in text_lower:
        is_mention = True

    should_wake = (is_wake_word or is_mention) and not should_sleep

    if should_sleep:
        if is_awake:
            log_info(
                f"[telegram_bot] Putting chat {chat_id} to sleep due to trigger in '{text}'"
            )
            set_attention(chat_id, False)
            # We set local is_awake to True one last time to allow the "bye" message to be processed
            # The next message will check chat attention and find it False.
            is_awake = True
            try:
                # Add sleep reaction
                await context.bot.set_message_reaction(
                    chat_id=chat_id, message_id=message.message_id, reaction="😴"
                )
            except Exception:
                pass

    elif should_wake:
        if not is_awake:
            log_info(
                f"[telegram_bot] Waking up chat {chat_id} due to trigger in '{text}'"
            )
            set_attention(chat_id, True)
            is_awake = True
            try:
                # Add wake reaction
                await context.bot.set_message_reaction(
                    chat_id=chat_id, message_id=message.message_id, reaction="👀"
                )
            except Exception:
                pass

    # Bypass triggers (execute even if asleep, but don't wake up efficiently)
    bypass_triggers = [
        "weather report",
        "weather check",
        "how is the weather",
        "weather forecast",
        "check weather",
        "weather status",
    ]
    should_bypass = any(t in text_lower for t in bypass_triggers) and not should_sleep

    # Re-evaluate effective awake status for THIS message processing
    # If is_awake is True, we process.
    if should_bypass:
        log_info(
            f"[telegram_bot] Bypassing sleep for chat {chat_id} due to trigger in '{text}'"
        )
        is_awake = True

    # Only allow trainer direct messages (private chat) to bypass sleep; group trainer messages do NOT bypass
    if not is_awake and not should_wake and not should_sleep:
        if is_trainer(user_id) and message.chat.type == "private":
            log_info(
                f"[telegram_bot] Chat {chat_id} is asleep but trainer {user_id} direct message will be processed"
            )
            # fall through and process the message
        else:
            log_debug(f"[telegram_bot] Chat {chat_id} is asleep; ignoring message")
            return

    # Avoid single-human fallback in group/supergroup chats to prevent false positives.
    if message.chat.type in ["group", "supergroup"]:
        human_count = None

    directed, reason = await is_message_for_bot(
        message, context.bot, bot_username=bot_username, human_count=human_count
    )
    log_debug(f"is_message_for_bot returned directed={directed}, reason='{reason}'")

    # Add reaction immediately when the message is directed (before sleep suppression)
    try:
        if directed:
            from core.reaction_handler import get_reaction_emoji, react_when_mentioned
            from core.core_initializer import INTERFACE_REGISTRY

            emoji = get_reaction_emoji()
            if emoji:
                interface = INTERFACE_REGISTRY.get("telegram_bot")
                if interface:
                    await react_when_mentioned(interface, message, emoji)
    except Exception as e:
        log_debug(f"[telegram_bot] Reaction add skipped/failed: {e}")

    if is_awake and not directed:
        # Respect centralized attention logic: do NOT force every message to be treated as directed.
        # The centralized queue (core.message_queue) will only process messages when there is an
        # explicit trigger or when is_message_for_bot returned True. Keeping this consistent
        # avoids the bot responding to all messages in an awake chat unexpectedly.
        log_debug(
            "[telegram_bot] Chat is AWAKE but message not directed; leaving directed=False to respect centralized attention rules"
        )
        # Intentionally not setting directed = True here

    if not directed:
        log_debug("[telegram_bot] DEBUG: Message not directed to bot - ignoring")
        if reason == "missing_human_count":
            log_debug("[telegram_bot] DEBUG: Reason: missing_human_count")
        elif reason == "unknown_human_count":
            log_debug("[telegram_bot] DEBUG: Reason: unknown_human_count")
        elif reason == "multiple_humans":
            log_debug("[telegram_bot] DEBUG: Reason: multiple_humans")
        else:
            log_debug(
                f"[telegram_bot] DEBUG: Reason: {reason or 'not directed to bot'}"
            )
        return

    log_debug(
        "[telegram_bot] DEBUG: Message is directed to bot - continuing processing"
    )

    log_debug(f"[telegram_bot] Message from {user_id} ({message.chat.type}): {text}")

    # === PRIORITY 3: Trainer reply to forwarded message ===
    trainer_id = get_trainer_id()
    log_debug(
        f"🟣 [PRIORITY 3 CHECK] Checking trainer reply conditions - chat_type: {message.chat.type}, user_id: {user_id}, trainer_id: {trainer_id}, has_reply: {bool(message.reply_to_message)}"
    )
    if (
        message.chat.type == "private"
        and user_id == trainer_id
        and message.reply_to_message
    ):
        log_debug(
            "🟣 [PRIORITY 3 ACTIVE] Processing trainer reply to forwarded message"
        )
        reply_msg_id = message.reply_to_message.message_id
        log_debug(f"Reply to trainer_message_id={reply_msg_id}")

        # Try robust resolution (plugin mapping, forwarded metadata, textual fallback)
        chat_id, orig_msg_id = await _resolve_original_from_reply(
            message.reply_to_message
        )

        if chat_id and orig_msg_id:
            log_debug(
                f"Trainer replies to resolved original: chat_id={chat_id}, message_id={orig_msg_id}"
            )
            await safe_send(
                context.bot,
                chat_id=chat_id,
                text=message.text,
                reply_to_message_id=orig_msg_id,
            )
            await message.reply_text("✅ Reply sent.")
            return

        # If no mapping, only block/notify when the replied-to message looks like an
        # actual forwarded/mapped trainer inbox item. Otherwise, this is probably the
        # trainer chatting normally with the bot in their private chat, and we should
        # let the normal processing flow handle it.
        try:
            import re as _re

            replied = message.reply_to_message
            has_forward_meta = bool(
                getattr(replied, "forward_from_chat", None)
            ) or bool(getattr(replied, "forward_from_message_id", None))
            has_forward_origin = getattr(replied, "forward_origin", None) is not None
            replied_text = getattr(replied, "text", "") or ""
            has_textual_fallback = bool(
                _re.search(
                    r"original message from chat\s+(-?\d+)\s+id\s+(\d+)", replied_text
                )
            )
            intended_forward_reply = (
                has_forward_meta or has_forward_origin or has_textual_fallback
            )
        except Exception:
            intended_forward_reply = False

        if intended_forward_reply:
            log_warning(
                "⚠️ No message found to reply to. Attempted plugin lookup, message_map, forwarded metadata, and textual fallback. "
                "Enable DEBUG logging (LOGGING_LEVEL=DEBUG) to see detailed resolution steps and plugin responses."
            )
            await message.reply_text("⚠️ No message found to reply to.")
            return

        log_debug(
            "🟣 [PRIORITY 3 SKIP] Reply has no mapping/forward markers; treating as normal trainer chat"
        )
    else:
        log_debug("Not a trainer reply - continuing to queue forwarding")

    # === PRIORITY 4: Forward to centralized queue (default behavior) ===
    log_debug(
        f"🔴 [PRIORITY 4 START] About to forward message to queue: '{text}' from user {user_id}"
    )
    log_debug("🔴 [PRIORITY 4] Checking message_queue module availability")

    # NOTE: Do NOT modify message.thread_id - Message objects are immutable in python-telegram-bot
    # The message_queue.enqueue() function will extract message_thread_id directly
    log_debug(
        f"🔴 [PRIORITY 4] Message has message_thread_id={getattr(message, 'message_thread_id', None)}"
    )

    log_debug("🔴 [PRIORITY 4] About to call message_queue.enqueue()...")
    try:
        log_debug("🔴 [PRIORITY 4] Calling message_queue.enqueue now...")

        # Wrap message to add wake/sleep flag for prompt engine
        wrapped_message = MessageWrapper(
            message,
            is_wake_sleep_command=is_wake_sleep_command,
            # Flag: tells message_chain to auto-inject TTS only for voice-originated messages.
            is_voice_input=bool(
                getattr(message, "voice", None) or getattr(message, "video_note", None)
            ),
        )

        await message_queue.enqueue(
            context.bot,
            wrapped_message,
            interface_id="telegram_bot",
            original_message=message,
            skip_mention_check=directed,
        )

        log_debug(
            "🔴 [PRIORITY 4 SUCCESS] Message successfully enqueued - processing should continue in queue"
        )

    except Exception as e:
        log_error(f"🔴 [PRIORITY 4 ERROR] message_queue enqueue failed: {repr(e)}", e)
        log_error(f"🔴 [PRIORITY 4 ERROR] Exception type: {type(e)}", e)
        import traceback

        log_error(f"🔴 [PRIORITY 4 ERROR] Traceback: {traceback.format_exc()}", e)
        await message.reply_text("⚠️ Error processing message.")


async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic command handler that delegates to centralized command registry."""
    if not update.message or not update.message.text:
        return

    command_text = update.message.text
    user_id = update.effective_user.id if update.effective_user else None

    # Create interface context for commands that need it
    interface_context = {
        "update": update,
        "context": context,
        "bot": context.bot,
        "interface_id": "telegram_bot",
    }

    try:
        response = await handle_command_message(
            command_text, user_id, "telegram_bot", interface_context
        )
        # Only send response if it's not None (meaning command was recognized)
        if response is not None:
            try:
                await update.message.reply_text(response, parse_mode="Markdown")
            except Exception as md_err:
                # Markdown parse error: retry as plain text so the reply is never lost
                log_error(
                    f"[telegram_bot] Markdown parse error, retrying plain text: {md_err}"
                )
                await update.message.reply_text(response)
    except Exception as e:
        log_error(f"[telegram_bot] Error handling command: {e}")
        await update.message.reply_text("❌ Error processing command.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return
    help_text = await execute_command("help")
    await update.message.reply_text(help_text, parse_mode="Markdown")


def escape_markdown(text):
    return re.sub(r"([_*\[\]()~`>#+=|{}.!-])", r"\\\1", text)


async def last_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return

    entries = await recent_chats.get_last_active_chats_verbose(10, context.bot)
    if not entries:
        await update.message.reply_text("⚠️ No recent chat found.")
        return

    lines = [
        f"[{escape_markdown(name)}](tg://user?id={cid}) — `{cid}`"
        for cid, name in entries
    ]
    await update.message.reply_text(
        "\U0001f553 Last active chats:\n" + "\n".join(lines), parse_mode="Markdown"
    )


async def manage_chat_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return

    args = context.args
    if not args:
        entries = await recent_chats.get_last_active_chats_verbose(20, context.bot)
        if not entries:
            await update.message.reply_text("⚠️ No chat found.")
            return
        lines = []
        for cid, name in entries:
            path = recent_chats.get_chat_path(cid)
            if path:
                lines.append(
                    f"{escape_markdown(name)} — `{cid}` -> {escape_markdown(path)}"
                )
            else:
                lines.append(f"{escape_markdown(name)} — `{cid}`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if args[0] == "reset":
        if len(args) < 2:
            await update.message.reply_text("Usage: /manage_chat_id reset <id|this>")
            return
        if args[1] == "this":
            cid = update.effective_chat.id
        else:
            try:
                cid = int(args[1])
            except ValueError:
                await update.message.reply_text("Invalid ID")
                return
        await recent_chats.reset_chat(cid)
        await update.message.reply_text(
            f"✅ Reset mapping for `{cid}`.", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "Usage: /manage_chat_id [reset <id>|reset this>"
        )


# /say command removed — use direct message sending via interface-specific tooling.


async def cortex_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy handler kept for backwards compatibility.

    The real work is performed by :func:`core.command_registry.handle_command_message`.
    This function simply delegates so that any future changes to `/cortex` output
    (including scope overrides) are automatically honoured.
    """
    # the generic handler will take care of permission checks
    command_text = update.message.text
    user_id = update.effective_user.id if update.effective_user else None
    interface_context = {
        "update": update,
        "context": context,
        "bot": context.bot,
        "interface_id": "telegram_bot",
    }

    try:
        response = await handle_command_message(
            command_text, user_id, "telegram_bot", interface_context
        )
        if response is not None:
            try:
                await update.message.reply_text(response, parse_mode="Markdown")
            except Exception as md_err:
                log_error(
                    f"[telegram_bot] Markdown parse error in cortex_command, retrying plain: {md_err}"
                )
                await update.message.reply_text(response)
    except Exception as e:
        log_error(f"[telegram_bot] Error handling cortex command: {e}")
        await update.message.reply_text("❌ Error processing command.")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return

    try:
        models = plugin_instance.get_supported_models()
    except Exception:
        await update.message.reply_text(
            "⚠️ This plugin does not support model selection."
        )
        return

    if not models:
        await update.message.reply_text("⚠️ No models available for this plugin.")
        return

    if not context.args:
        current = plugin_instance.get_current_model() or models[0]
        msg = "*Available models:*\n" + "\n".join(f"• `{m}`" for m in models)
        msg += f"\n\nActive model: `{current}`"
        msg += "\n\nTo change: `/model <name>`"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    choice = context.args[0]
    if choice not in models:
        await update.message.reply_text(f"❌ Model `{choice}` not valid.")
        return

    try:
        plugin_instance.set_current_model(choice)
        await update.message.reply_text(f"✅ Model updated to `{choice}`.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error changing model: {e}")


def telegram_notify(chat_id: int, message: str, reply_to_message_id: int = None):
    import html
    import re
    from telegram import Bot
    from telegram.error import TelegramError
    from telegram.constants import ParseMode

    # Forza la notifica solo al get_trainer_id() in privato
    log_debug(f"[telegram_notify] → CALLED con chat_id={chat_id}")
    log_debug(f"[telegram_notify] → MESSAGE:\n{message}")

    bot = Bot(token=BOTFATHER_TOKEN)

    # Se il destinatario non è il get_trainer_id(), non inviare nulla
    if chat_id != get_trainer_id():
        log_debug(
            f"[telegram_notify] Ignorato: chat_id {chat_id} != get_trainer_id() {get_trainer_id()}"
        )
        return

    # Make URLs clickable
    url_pattern = re.compile(r"https?://\S+")
    match = url_pattern.search(message or "")
    formatted_message = None
    if match:

        def repl(m):
            url = m.group(0)
            return f'<a href="{html.escape(url)}">{html.escape(url)}</a>'

        formatted_message = url_pattern.sub(repl, html.escape(message))

    targets = [get_trainer_id()]
    log_chat_id = get_log_chat_id_sync()
    if log_chat_id and log_chat_id not in targets:
        targets.append(log_chat_id)

    async def send(target: int, reply_id: int | None):
        try:
            await safe_send(
                bot,
                chat_id=target,
                text=formatted_message or message,
                reply_to_message_id=reply_id,
                parse_mode=ParseMode.HTML if formatted_message else None,
                disable_web_page_preview=True,
            )  # [FIX][telegram retry]
            log_debug(f"[notify] ✅ Telegram message sent to {target}")
        except TelegramError as e:
            log_error(f"[notify] ❌ Telegram error: {repr(e)}", e)
        except Exception as e:
            log_error(f"[notify] ❌ Other error in send(): {repr(e)}", e)

    async def runner():
        for tgt in targets:
            await send(tgt, reply_to_message_id if tgt == get_trainer_id() else None)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        loop.create_task(runner())
    else:
        asyncio.run(runner())


# === Startup ===


async def plugin_startup_callback(application):
    """Run pending plugin tasks once the bot's event loop is ready."""
    from core.core_initializer import core_initializer

    # Start any async plugins that were deferred until a loop was available
    await core_initializer.start_pending_async_plugins()

    # Start the queue consumer after the application is ready
    application.create_task(message_queue.run())


# Global variable to track the telegram polling task
_polling_task = None
_bot_started = False


async def start_bot():
    """Start the Telegram bot application.

    This function assumes the core has already been initialized.
    It should be called from TelegramInterface.start() or during autostart.
    """
    global _bot_started
    if _bot_started:
        log_debug(
            "[telegram_bot] start_bot() already called, skipping duplicate startup"
        )
        return

    log_info("[telegram_bot] start_bot() function called")
    _bot_started = True

    if not BOTFATHER_TOKEN:
        log_warning(
            "[telegram_bot] BOTFATHER_TOKEN not configured - skipping Telegram bot startup"
        )
        return

    # Parse trainer ID from configuration
    trainer_id = _parse_trainer_id_from_config()
    if not trainer_id:
        log_warning(
            "[telegram_bot] No trainer ID found in TRAINER_IDS - skipping Telegram bot startup"
        )
        return

    # Set trainer ID in the registry (interface is already registered at import time)
    _interface_registry.set_trainer_id("telegram_bot", trainer_id)
    log_info(f"[telegram_bot] Set trainer ID {trainer_id} for telegram_bot interface")

    try:
        log_info("[telegram_bot] Building Telegram application...")

        # Check if we should disable SSL verification (for dev environments with cert issues)
        import os

        disable_ssl = os.getenv("TELEGRAM_DISABLE_SSL_VERIFY", "0") == "1"

        if disable_ssl:
            log_warning(
                "[telegram_bot] ⚠️ SSL verification DISABLED - use only in development!"
            )
            # Monkey-patch httpx to disable SSL verification globally for this process
            import httpx

            # Store original client init
            original_client_init = httpx.AsyncClient.__init__

            # Create wrapper that forces verify=False
            def patched_client_init(self, *args, **kwargs):
                kwargs["verify"] = False
                return original_client_init(self, *args, **kwargs)

            # Apply monkey patch
            httpx.AsyncClient.__init__ = patched_client_init
            log_debug(
                "[telegram_bot] Patched httpx.AsyncClient to disable SSL verification"
            )

        # Configure timeouts to avoid frequent TimedOut warnings
        # connect_timeout: time to establish connection
        # read_timeout: time to wait for response from Telegram servers
        # write_timeout: time to send data to Telegram servers
        # pool_timeout: time to wait for connection from pool

        # Convert ConfigVar to string for ApplicationBuilder
        bot_token_str = str(BOTFATHER_TOKEN) if BOTFATHER_TOKEN else None

        app = (
            ApplicationBuilder()
            .token(bot_token_str)
            .post_init(plugin_startup_callback)
            .connect_timeout(30.0)  # Increased from default ~5s to 30s
            .read_timeout(30.0)  # Increased from default ~5s to 30s
            .write_timeout(30.0)  # Increased from default ~5s to 30s
            .pool_timeout(10.0)  # Connection pool timeout
            .build()
        )
        log_info("[telegram_bot] Telegram application built successfully")
        log_info(f"[telegram_bot] get_trainer_id() configured as: {get_trainer_id()}")
        bot_token_status = "Yes" if str(BOTFATHER_TOKEN).strip() else "No"
        log_info(f"[telegram_bot] BOTFATHER_TOKEN configured: {bot_token_status}")

        log_info("[telegram_bot] Adding command handlers...")
        # Use generic command handler for all commands
        app.add_handler(MessageHandler(filters.COMMAND, handle_command))

        # Single unified message handler for ALL non-command messages
        log_info("[telegram_bot] Adding unified MessageHandler for all messages...")
        app.add_handler(
            MessageHandler(
                (filters.TEXT & ~filters.COMMAND)
                | filters.PHOTO
                | filters.Document.ALL
                | filters.Sticker.ALL
                | filters.AUDIO
                | filters.VOICE
                | filters.VIDEO,
                handle_message,
            )
        )
        log_info("[telegram_bot] All handlers added successfully")

        # Add error handler to catch any exceptions
        async def error_handler(
            update: object, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            """Log errors caused by updates."""
            log_error(
                f"[telegram_bot] Exception while handling an update: {context.error}"
            )
            if update:
                log_error(f"[telegram_bot] Update that caused error: {update}")

        app.add_error_handler(error_handler)
        log_info("[telegram_bot] Error handler added")

        # The interface will register itself once the Telegram application has
        # been initialized below. Calling core_initializer.register_interface
        # here would run before the interface instance exists and generates a
        # misleading warning about missing action support.
    except Exception as e:
        log_error(f"[telegram_bot] Error building Telegram application: {repr(e)}")
        raise

    # Plugin startup is handled by plugin_startup_callback
    # No need for fallback as the callback ensures proper async startup

    async def _run_polling_loop():
        """Run the polling loop in a separate background task.

        This function will run indefinitely until cancelled or until an error occurs.
        It's executed in a background task created by asyncio.create_task() to avoid
        blocking the main application startup.
        """
        try:
            log_info("[telegram_bot] Polling loop task started")
            log_info("[telegram_bot] Starting Telegram polling...")
            await app.updater.start_polling()
            log_info("[telegram_bot] Polling started successfully")

            # Keep the polling running until cancelled
            log_info("[telegram_bot] Bot is now running and listening for messages...")
            # Wait indefinitely - this will be interrupted when the app shuts down
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            log_info("[telegram_bot] Polling task was cancelled")
            raise
        except Exception as e:
            log_error(f"[telegram_bot] Error in polling loop: {repr(e)}")
            raise
        finally:
            log_info("[telegram_bot] Polling loop task ending...")

    try:
        log_info("[telegram_bot] Starting Telegram application initialization...")
        # Use async initialization instead of run_polling to avoid event loop conflicts
        await app.initialize()
        log_info("[telegram_bot] Telegram application initialized")

        # Stop any existing updater before starting a new one
        if app.updater and app.updater.running:
            log_info("[telegram_bot] Stopping existing updater...")
            await app.updater.stop()
            log_info("[telegram_bot] Existing updater stopped")

        # Update the global interface instance with the bot
        global telegram_interface, _polling_task
        telegram_interface.bot = app.bot
        telegram_interface.is_enabled = True
        telegram_interface.disabled_reason = None
        log_debug("[telegram_bot] Bot instance assigned to telegram_interface")

        # Rebuild action schemas (summary will be shown later by main initialization)
        from core.core_initializer import core_initializer

        await core_initializer.refresh_actions_block()
        log_debug("[telegram_bot] Action schemas refreshed")

        await app.start()
        log_info("[telegram_bot] Telegram application started")

        # Create a background task for polling that doesn't block start_bot() from returning
        log_info("[telegram_bot] Creating background polling task...")
        _polling_task = asyncio.create_task(_run_polling_loop())
        _polling_task.set_name("telegram_polling")
        log_info("[telegram_bot] Background polling task created and scheduled")
        log_info(
            "[telegram_bot] start_bot() completed successfully - polling running in background"
        )

    except Exception as e:
        log_error(f"[telegram_bot] Error during Telegram bot startup: {repr(e)}")
        raise
    finally:
        # Note: We don't stop/shutdown the app here because the polling task runs in background
        # The proper shutdown will be handled by the application lifecycle when signals are received
        log_debug("[telegram_bot] start_bot() finally block completed")


class TelegramInterface:
    """Interface wrapper providing a standard send_message method for Telegram."""

    display_name = "Telegram Bot"

    def __init__(self, bot: Bot = None):
        """Store the python-telegram-bot ``Bot`` instance."""
        self.bot = bot
        self.is_enabled = False
        self.disabled_reason = None

        # Check if we have required configuration
        if not BOTFATHER_TOKEN:
            self.disabled_reason = "BOTFATHER_TOKEN not configured"
            log_warning(
                f"[telegram_interface] Interface loaded in disabled state: {self.disabled_reason}"
            )
        elif not _parse_trainer_id_from_config():
            self.disabled_reason = "No trainer ID configured in TRAINER_IDS"
            log_warning(
                f"[telegram_interface] Interface loaded in disabled state: {self.disabled_reason}"
            )
        else:
            self.is_enabled = True
            log_debug("[telegram_interface] Interface enabled")

        # Register resolver to fetch chat/thread names automatically
        async def _resolver(chat_id, thread_id, bot_instance=None):
            b = bot_instance or self.bot
            chat_name = None
            thread_name = None
            if b is None:
                try:
                    global _last_bot_none_lookup_log_time
                    now = time.time()
                    if (
                        now - _last_bot_none_lookup_log_time
                        >= _bot_none_log_throttle_sec
                    ):
                        log_warning(
                            "[telegram_interface] Bot is None, cannot lookup chat name"
                        )
                        _last_bot_none_lookup_log_time = now
                except NameError:
                    # Variable not defined, log without throttling
                    log_warning(
                        "[telegram_interface] Bot is None, cannot lookup chat name"
                    )
                return {"chat_name": None, "message_thread_name": None}
            try:
                chat = await b.getChat(chat_id)
                chat_name = getattr(chat, "title", None) or getattr(
                    chat, "username", None
                )
            except Exception as e:  # pragma: no cover - network failures
                log_warning(f"[telegram_interface] chat name lookup failed: {e}")
            if thread_id:
                try:
                    # Check if getForumTopic method exists (available in newer versions of python-telegram-bot)
                    if hasattr(b, "getForumTopic"):
                        topic = await b.getForumTopic(chat_id, thread_id)
                        thread_name = getattr(topic, "name", None) or getattr(
                            topic, "title", None
                        )
                    else:
                        log_debug(
                            "[telegram_interface] getForumTopic method not available, skipping thread name lookup"
                        )
                        thread_name = None
                except Exception as e:  # pragma: no cover
                    log_warning(f"[telegram_interface] thread name lookup failed: {e}")
                    thread_name = None
            return {"chat_name": chat_name, "message_thread_name": thread_name}

        ChatLinkStore.set_name_resolver("telegram", _resolver)

        # Register validation rules with the validation registry
        self._register_custom_validation()

        log_debug("[telegram_interface] TelegramInterface instance initialized")

    async def start(self):
        """Start the Telegram interface - called after config load from DB."""
        log_info("[telegram_interface] Starting Telegram interface...")

        # Re-check configuration (may have been loaded from DB after __init__)
        global BOTFATHER_TOKEN
        BOTFATHER_TOKEN = config_registry.get_var(
            "BOTFATHER_TOKEN",
            None,
            label="Telegram Bot Token",
            description="Bot token provided by BotFather on Telegram.",
            group="interface",
            component="telegram_bot",
            sensitive=True,
        )

        if not BOTFATHER_TOKEN:
            self._disable("BOTFATHER_TOKEN not configured")
            log_warning(
                "[telegram_interface] Telegram interface disabled: no token configured"
            )
            return

        trainer_id = _parse_trainer_id_from_config()
        if not trainer_id:
            self._disable("No trainer ID configured in TRAINER_IDS")
            log_warning(
                "[telegram_interface] Telegram interface disabled: no trainer ID"
            )
            return

        # Enable the interface and start the bot
        self.is_enabled = True
        self.disabled_reason = None
        log_info("[telegram_interface] Configuration validated, starting bot...")

        # Start the actual bot
        await start_bot()
        log_info("[telegram_interface] Telegram interface started successfully")

    def _disable(self, reason: str) -> None:
        """Mark interface as disabled with a reason."""
        self.is_enabled = False
        self.disabled_reason = reason

    @staticmethod
    def get_interface_id() -> str:
        """Return the unique identifier for this interface."""
        return "telegram_bot"

    @staticmethod
    def get_supported_actions() -> dict:
        """Return schema information for supported actions."""
        return {
            "message_telegram_bot": {
                "required_fields": ["text", "interface_path"],
                "optional_fields": [
                    "chat_name",
                    "reply_to_message_id",
                ],
                "description": "Send a text message via Telegram",
            },
            "audio_telegram_bot": {
                "required_fields": ["audio", "interface_path"],
                "optional_fields": [
                    "chat_name",
                ],
                "description": "Send a voice message via Telegram",
            },
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> dict:
        """Prompt instructions for supported actions."""
        if action_name == "message_telegram_bot":
            return {
                "description": "Send a message via Telegram bot",
                "payload": {
                    "text": {
                        "type": "string",
                        "example": "Hello!",
                        "description": "The message text to send",
                    },
                    "interface_path": {
                        "type": "string",
                        "example": "telegram_bot/123456789/456",
                        "description": "REQUIRED. Interface path in format 'telegram_bot/chat_id' or 'telegram_bot/chat_id/thread_id'. Use input.payload.source.interface_path to reply in same context.",
                    },
                    "chat_name": {
                        "type": "string",
                        "example": "Rekkus Hideout",
                        "description": "Alternative to interface_path for specifying the chat by name (will be resolved to interface_path)",
                        "optional": True,
                    },
                    "reply_to_message_id": {
                        "type": "integer",
                        "example": 12345,
                        "description": "Optional ID of the message to reply to",
                        "optional": True,
                    },
                },
                "important_notes": [
                    "CRITICAL: ALWAYS use interface_path from input.payload.source.interface_path to reply in same conversation!",
                    "Format: 'telegram_bot/chat_id' for regular chats or 'telegram_bot/chat_id/thread_id' for topics/threads",
                    "Example: if input shows 'telegram_bot/-1003098886330/789', use EXACTLY that as interface_path in your payload",
                    "Never use just chat_id or target - always use the complete interface_path format",
                ],
            }
        if action_name == "audio_telegram_bot":
            return {
                "description": "Send a voice message via Telegram bot",
                "payload": {
                    "audio": {
                        "type": "string",
                        "example": "/path/to/file.ogg",
                        "description": "Path to the voice file",
                    },
                    "interface_path": {
                        "type": "string",
                        "example": "telegram_bot/123456789/456",
                        "description": "REQUIRED. Complete interface path. Use input.payload.source.interface_path to reply in same context.",
                    },
                    "chat_name": {
                        "type": "string",
                        "example": "Rekkus Hideout",
                        "description": "Alternative to interface_path for specifying the chat by name",
                        "optional": True,
                    },
                    "caption": {
                        "type": "string",
                        "example": "This is a voice message",
                        "description": "Text caption to display with the voice message",
                        "optional": True,
                    },
                },
            }
        return None

    @staticmethod
    def validate_payload(action_type: str, payload: dict) -> list:
        """Validate payload for telegram actions."""
        errors = []

        if action_type == "message_telegram_bot":
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                errors.append("payload.text must be a non-empty string")

        elif action_type == "audio_telegram_bot":
            audio = payload.get("audio")
            if not isinstance(audio, str) or not audio:
                errors.append("payload.audio must be a non-empty string")
        else:
            return []

        interface_path = payload.get("interface_path")
        chat_name = payload.get("chat_name")

        if interface_path is None and chat_name is None:
            errors.append("payload.interface_path or payload.chat_name is required")
        else:
            if interface_path is not None and not isinstance(interface_path, str):
                errors.append("payload.interface_path must be a string")
            if chat_name is not None and not isinstance(chat_name, str):
                errors.append("payload.chat_name must be a string")

        return errors

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        """Execute non-message actions (audio, etc)."""
        action_type = action.get("type")
        payload = action.get("payload", {})

        if action_type == "audio_telegram_bot":
            interface_path = payload.get("interface_path")
            audio_path = payload.get("audio")
            caption = payload.get("caption") or payload.get("text")

            # Telegram has a 1024 char limit for captions
            if caption and len(caption) > 1024:
                log_warning(
                    f"[telegram_interface] Caption length {len(caption)} exceeds limit (1024). Sending as separate text message."
                )
                await self.send_message(
                    {
                        "text": caption,
                        "interface_path": interface_path,
                        "chat_name": payload.get("chat_name"),
                    }
                )
                caption = None

            target = None
            thread_id = None
            if interface_path:
                try:
                    from core.interface_path_utils import parse_interface_path

                    _, levels = parse_interface_path(interface_path)
                    # telegram_bot/chat_id/thread_id
                    if len(levels) >= 1:
                        target = levels[0]
                    if len(levels) >= 2:
                        thread_id = levels[1]
                except Exception as e:
                    log_warning(
                        f"[telegram_interface] Failed to parse path {interface_path}: {e}"
                    )

            chat_name = payload.get("chat_name")
            if not target and chat_name:
                # TODO: resolve chat name logic if needed, skipping for now
                pass

            if not target:
                log_warning("[telegram_interface] Missing target for audio")
                return {"status": "failed", "message": "Missing target"}

            if not audio_path or not os.path.exists(audio_path):
                log_warning(f"[telegram_interface] Audio file missing: {audio_path}")
                return {"status": "failed", "message": "Audio missing"}

            try:
                log_debug(
                    f"[telegram_interface] Sending voice to {target} (thread={thread_id})"
                )
                with open(audio_path, "rb") as audio_file:
                    await self.bot.send_voice(
                        chat_id=target,
                        voice=audio_file,
                        caption=caption,
                        message_thread_id=thread_id,
                    )
                log_info(f"[telegram_interface] Sent audio to {target}")
                return {"status": "success"}
            except Exception as e:
                log_error(f"[telegram_interface] Failed to send audio: {e}")
                return {"status": "failed", "error": str(e)}

        return {"status": "failed", "message": f"Unknown action {action_type}"}

    def _register_custom_validation(self):
        """Register custom validation rules with the validation registry."""
        try:
            from core.validation_registry import ValidationRule, get_validation_registry

            def validate_telegram_message(payload):
                """Enhanced validation for Telegram message actions."""
                errors = []

                # Validate text content
                text = payload.get("text")
                if text is None or (isinstance(text, str) and not text.strip()):
                    errors.append("Message text cannot be empty")
                elif not isinstance(text, str):
                    errors.append("Message text must be a string")

                # Validate interface_path or chat_name
                interface_path = payload.get("interface_path")
                chat_name = payload.get("chat_name")

                if interface_path is None and chat_name is None:
                    errors.append("Either interface_path or chat_name must be provided")

                if interface_path is not None and not isinstance(interface_path, str):
                    errors.append("interface_path must be a string")
                elif interface_path is not None and not interface_path.strip():
                    errors.append("interface_path cannot be empty")

                if chat_name is not None and not isinstance(chat_name, str):
                    errors.append("chat_name must be a string")

                return errors

            def validate_telegram_audio(payload):
                """Enhanced validation for Telegram audio actions."""
                errors = []

                # Validate audio path
                audio = payload.get("audio")
                if audio is None or (isinstance(audio, str) and not audio.strip()):
                    errors.append("Audio path cannot be empty")
                elif not isinstance(audio, str):
                    errors.append("Audio path must be a string")

                # Validate interface_path or chat_name
                interface_path = payload.get("interface_path")
                chat_name = payload.get("chat_name")

                if interface_path is None and chat_name is None:
                    errors.append("Either interface_path or chat_name must be provided")

                if interface_path is not None and not isinstance(interface_path, str):
                    errors.append("interface_path must be a string")
                elif interface_path is not None and not interface_path.strip():
                    errors.append("interface_path cannot be empty")

                if chat_name is not None and not isinstance(chat_name, str):
                    errors.append("chat_name must be a string")

                return errors

            # Create validation rules for message_telegram_bot
            message_rule = ValidationRule(
                action_type="message_telegram_bot",
                required_fields=["text"],
                custom_validator=validate_telegram_message,
                component_name="telegram_bot",
            )

            # Create validation rules for audio_telegram_bot
            audio_rule = ValidationRule(
                action_type="audio_telegram_bot",
                required_fields=["audio"],
                custom_validator=validate_telegram_audio,
                component_name="telegram_bot",
            )

            # Register with validation registry
            registry = get_validation_registry()
            registry.register_component_rules(
                "telegram_bot", [message_rule, audio_rule]
            )

            log_debug(
                "[telegram_bot] Registered custom validation rules with validation registry"
            )

        except Exception as e:
            log_warning(f"[telegram_bot] Failed to register custom validation: {e}")

    async def _emit_system_error(
        self,
        step: str,
        details: str,
        payload: dict,
        original_message: object | None = None,
    ) -> None:
        """
        🚨 EMERGENCY FIX: COMPLETELY DISABLED to prevent infinite loops.

        System errors from delivery issues (retry_exhausted, copy_check)
        cause infinite recursive loops. This method now ONLY logs.
        """
        try:
            log_error(
                f"[telegram_interface] System error BLOCKED - step: {step}, details: {details}, payload: {payload}"
            )
            # ❌ NO SENDING ANYTHING - causes loops!
            # ❌ NO bot.send_message - causes loops!
            # ❌ NO system_message generation - causes loops!
            return  # Exit immediately after logging
        except Exception as e:
            log_error(f"[telegram_interface] Failed to emit system error: {e}")

    async def _verify_delivery(
        self,
        sent_message: object | None,
        payload: dict,
        original_message: object | None = None,
    ) -> None:
        """
        🚨 EMERGENCY FIX: COMPLETELY DISABLED to prevent infinite loops.

        This method was causing infinite recursive loops through _emit_system_error.
        The copy_message functionality is not critical for core operation.
        """
        # ❌ COMPLETELY DISABLED - was causing infinite loops
        # ❌ NO copy_message attempts - causes loops
        # ❌ NO _emit_system_error calls - causes loops
        # ❌ NO retry logic - causes loops
        return  # Exit immediately without any operations

    async def send_message(
        self, payload: dict, original_message: object | None = None
    ) -> None:
        """Send a message using the stored bot.

        Parameters
        ----------
        payload: dict
            Must contain at least ``text`` and ``target``. Optionally may include
            ``thread_id``.
        original_message: object | None
            The triggering message; used for reply fallback handling.

        ``thread_id`` is the correct Telegram parameter for replies in
        topics and replaces the legacy ``thread_id`` name.
        """
        import json

        if self.bot is None:
            log_warning("[telegram_interface] Bot not initialized, cannot send message")
            return

        text = payload.get("text", "")
        interface_path = payload.get("interface_path")
        chat_name = payload.get("chat_name")

        # Normalize text to recover mojibake or double-escaped unicode sequences
        try:
            from core.text_utils import normalize_for_outbound

            norm = normalize_for_outbound(text)
            if norm and norm != text:
                log_debug(
                    "[telegram_interface] Normalized text payload (mojibake/unescape)"
                )
                text = norm
        except Exception:
            pass

        # If no interface_path and no chat_name, silently ignore (likely from synthetic event message)
        if not interface_path and not chat_name:
            log_debug(
                "[telegram_interface] Skipping send: no interface_path or chat_name provided (likely synthetic event message)"
            )
            return

        # Extract chat_id and thread_id from interface_path if provided
        thread_id = None
        target = None
        if interface_path:
            from core.interface_path_utils import extract_legacy_ids

            legacy_ids = extract_legacy_ids(interface_path)
            target = legacy_ids.get("chat_id")
            thread_id = legacy_ids.get("thread_id")
            log_debug(
                f"[telegram_interface] Extracted from interface_path: chat_id={target}, thread_id={thread_id}"
            )

        log_debug(
            f"[telegram_interface] Sending to interface_path={interface_path} chat_name={chat_name} extracted: chat_id={target} thread_id={thread_id}"
        )

        if not text or (target is None and chat_name is None):
            log_warning("[telegram_interface] Missing text or destination, aborting")
            return

        chat_id = target

        if chat_id is None:
            try:
                row = await chat_link_store.resolve(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    chat_name=chat_name,
                    message_thread_name=None,
                )
            except ChatLinkMultipleMatches:
                # Use orchestrator instead of legacy corrector
                try:
                    from core import action_parser
                    from types import SimpleNamespace
                    from datetime import datetime
                except Exception:
                    action_parser = None
                correction_payload = {
                    "system_message": {
                        "type": "error",
                        "message": f"Multiple channels found with name {chat_name}, please repeat your previous message putting the chat_id instead of chat_name",
                        "your_reply": payload,
                    }
                }
                msg = SimpleNamespace()
                msg.chat_id = None
                msg.text = ""
                msg.original_text = json.dumps(correction_payload, ensure_ascii=False)
                msg.thread_id = None
                msg.date = datetime.utcnow()
                msg.from_cortex = False
                if action_parser is not None:
                    try:
                        await action_parser.corrector_orchestrator(
                            text=msg.original_text,
                            context={"interface": "telegram"},
                            bot=self.bot,
                            message=msg,
                        )
                    except Exception:
                        pass
                return
            if not row:
                # Use orchestrator instead of legacy corrector for not-found
                from core.transport_layer import notify_corrector_of_system_message

                await notify_corrector_of_system_message(
                    json.dumps(correction_payload, ensure_ascii=False),
                    self.bot,
                    chat_id=None,
                    thread_id=None,
                    interface="telegram",
                )
                return
            chat_id = row.get("chat_id", chat_id)
            thread_id = row.get("thread_id", thread_id)

        log_debug(
            f"[telegram_interface] Resolved: chat_id={chat_id}, final_thread_id={thread_id}"
        )

        # Ensure thread_id is a string if present
        if thread_id is not None:
            thread_id = str(thread_id)

        # Ensure chat_id is a string
        if chat_id is not None:
            chat_id = str(chat_id)

        await chat_link_store.update_names_from_resolver(
            chat_id, thread_id, interface="telegram_bot", bot=self.bot
        )

        reply_message_id = None
        if (
            original_message
            and hasattr(original_message, "chat_id")
            and hasattr(original_message, "message_id")
            and chat_id == getattr(original_message, "chat_id")
        ):
            _raw_mid = original_message.message_id
            try:
                reply_message_id = int(_raw_mid)
            except (ValueError, TypeError):
                reply_message_id = None
            log_debug(f"[telegram_interface] reply_to_message_id: {reply_message_id}")

            # Also set thread_id from original message if not already set
            if thread_id is None and hasattr(original_message, "thread_id"):
                orig_thread_id = getattr(original_message, "thread_id")
                if orig_thread_id is not None:
                    thread_id = orig_thread_id
                    log_debug(
                        f"[telegram_interface] thread_id from original message: {thread_id}"
                    )

        fallback_chat_id = None
        fallback_thread_id = None
        fallback_reply_to = None
        if (
            original_message
            and hasattr(original_message, "chat_id")
            and chat_id != getattr(original_message, "chat_id")
        ):
            fallback_chat_id = original_message.chat_id
            fallback_thread_id = getattr(original_message, "thread_id", None)
            if hasattr(original_message, "message_id"):
                try:
                    fallback_reply_to = int(original_message.message_id)
                except (ValueError, TypeError):
                    fallback_reply_to = None
        elif (
            original_message
            and hasattr(original_message, "chat_id")
            and chat_id == getattr(original_message, "chat_id")
            and thread_id is None
        ):
            # Same chat but no thread specified - try to use original message's thread
            if hasattr(original_message, "thread_id"):
                orig_thread_id = getattr(original_message, "thread_id")
                if orig_thread_id is not None:
                    thread_id = orig_thread_id
                    log_debug(
                        f"[telegram_interface] Using original message thread for same chat: {thread_id}"
                    )

        try:
            sent_message = await send_with_thread_fallback(
                self.bot,
                chat_id,
                text,
                parse_mode="Markdown",
                thread_id=thread_id,  # fixed: correct param is thread_id
                reply_to_message_id=reply_message_id,
                fallback_chat_id=fallback_chat_id,
                fallback_thread_id=fallback_thread_id,
                fallback_reply_to_message_id=fallback_reply_to,
            )

            # Save SyntH's response via core chat_context_manager
            try:
                from core.chat_context_manager import save_response_message
                from core.interface_path_utils import build_interface_path

                msg_interface_path = build_interface_path(
                    "telegram_bot", str(chat_id), str(thread_id) if thread_id else None
                )
                await save_response_message(msg_interface_path, text)
            except Exception as e:
                log_debug(
                    f"[telegram_interface] Failed to save response via context_manager: {e}"
                )

        except BadRequest as e:
            if "chat not found" in str(e).lower():
                # Use orchestrator instead of legacy corrector
                from core.transport_layer import notify_corrector_of_system_message

                await notify_corrector_of_system_message(
                    json.dumps(correction_payload, ensure_ascii=False),
                    self.bot,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    interface="telegram",
                )
                return
            else:
                # Generic error -> request correction via orchestrator
                from core.transport_layer import notify_corrector_of_system_message

                await notify_corrector_of_system_message(
                    json.dumps(correction_payload, ensure_ascii=False),
                    self.bot,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    interface="telegram",
                )
                return
        await self._verify_delivery(sent_message, payload, original_message)

    async def add_reaction(self, message, emoji: str) -> bool:
        """Add a reaction to a message.

        Args:
            message: The Telegram message object
            emoji: The emoji to use as reaction

        Returns:
            bool: True if reaction was added successfully
        """
        log_debug(
            f"[telegram_interface] add_reaction called with emoji '{emoji}' for message {getattr(message, 'message_id', 'unknown')}"
        )
        try:
            log_debug(f"[telegram_interface] self.bot is None: {self.bot is None}")
            if not self.bot:
                log_warning(
                    "[telegram_interface] Bot instance is None, cannot add reaction"
                )
                return False

            chat_id = getattr(message, "chat_id", None) or getattr(
                message.chat, "id", None
            )
            message_id = getattr(message, "message_id", None)

            if not chat_id or not message_id:
                log_warning(
                    "[telegram_interface] Cannot add reaction: missing chat_id or message_id"
                )
                return False

            log_debug(
                f"[telegram_interface] Adding reaction '{emoji}' to chat_id={chat_id}, message_id={message_id}"
            )

            # Check if method exists
            if not hasattr(self.bot, "set_message_reaction"):
                log_warning(
                    "[telegram_interface] set_message_reaction method not available in this version of python-telegram-bot"
                )
                return False

            await self.bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id, reaction=emoji, is_big=False
            )
            log_info(
                f"[telegram_interface] Successfully added reaction '{emoji}' to message {message_id}"
            )
            return True
        except Exception as e:
            log_warning(
                f"[telegram_interface] Failed to add reaction '{emoji}': {e} - chat_id={chat_id}, message_id={message_id}, type={type(e).__name__}"
            )
            import traceback

            log_debug(f"[telegram_interface] Traceback: {traceback.format_exc()}")
            return False


# Declare telegram_interface as None initially - will be created after config load
telegram_interface = None


def initialize_interface():
    """Initialize the Telegram interface after config has been loaded from DB.

    This function is called by the core initializer after all configurations
    have been loaded from the database. This ensures that config_registry.get_var()
    returns the correct values from the DB.

    Can also be called to reload the interface when configuration changes.
    """
    global telegram_interface

    # If interface already exists, clean it up first
    if telegram_interface is not None:
        log_info("[telegram_bot] Reloading interface with updated configuration...")
        shutdown_interface()

    log_info("[telegram_bot] Creating Telegram interface instance...")
    telegram_interface = TelegramInterface(
        None
    )  # Bot instance will be set later during startup
    register_interface("telegram_bot", telegram_interface)
    log_info("[telegram_bot] Telegram interface instance created and registered")

    return telegram_interface


def shutdown_interface():
    """Shutdown and cleanup the Telegram interface.

    Called before reload or shutdown to properly cleanup resources.
    """
    global telegram_interface, _polling_task

    if telegram_interface is None:
        log_debug("[telegram_bot] No interface to shutdown")
        return

    log_info("[telegram_bot] Shutting down Telegram interface...")

    try:
        # Cancel the polling task if it's running
        if _polling_task is not None:
            if not _polling_task.done():
                log_info("[telegram_bot] Cancelling polling task...")
                # _polling_task.cancel()
                try:
                    _polling_task.cancel()
                except Exception:
                    pass
            _polling_task = None

        # Stop the bot if it's running
        if telegram_interface.bot is not None:
            # The actual bot shutdown is handled by the application lifecycle
            log_debug("[telegram_bot] Bot instance cleanup")
            telegram_interface.bot = None

        # Unregister from interface registry
        from core.core_initializer import INTERFACE_REGISTRY

        if "telegram_bot" in INTERFACE_REGISTRY:
            del INTERFACE_REGISTRY["telegram_bot"]
            log_debug("[telegram_bot] Unregistered from interface registry")

        telegram_interface = None
        log_info("[telegram_bot] Telegram interface shutdown complete")

    except Exception as e:
        log_error(f"[telegram_bot] Error during interface shutdown: {e}")


def reload_interface():
    """Reload the interface with updated configuration.

    This is called when configuration variables change and the interface
    needs to be restarted with new values.
    """
    log_info("[telegram_bot] Reloading Telegram interface...")
    return initialize_interface()


# Auto-start Telegram bot at import time if configured
# This is only for backwards compatibility when running outside of core_initializer
# Normally, initialize_interface() will be called by the core after config load
if telegram_interface is None and BOTFATHER_TOKEN and _parse_trainer_id_from_config():
    log_info("[telegram_bot] Legacy autostart: creating interface at import time")
    initialize_interface()

    if telegram_interface and telegram_interface.is_enabled:
        log_info(
            "[telegram_bot] BOTFATHER_TOKEN and trainer ID configured - scheduling Telegram bot startup"
        )

        # Schedule the bot to start when an event loop becomes available
        def _schedule_telegram_startup():
            """Schedule Telegram bot startup in the event loop."""
            try:
                import asyncio

                loop = asyncio.get_running_loop()
                loop.create_task(start_bot())
                log_info("[telegram_bot] Telegram bot startup task scheduled")
            except RuntimeError:
                # No event loop yet - will be handled by the main application
                log_debug(
                    "[telegram_bot] No event loop running, bot will start when application initializes"
                )

        # Try to schedule immediately
        try:
            _schedule_telegram_startup()
        except Exception as e:
            log_debug(f"[telegram_bot] Could not schedule startup immediately: {e}")
            # This is expected during import - the main app will handle it
    else:
        reason = (
            telegram_interface.disabled_reason
            if telegram_interface
            else "interface not initialized"
        )
        log_info(f"[telegram_bot] Telegram bot will not auto-start: {reason}")
else:
    log_debug(
        "[telegram_bot] Waiting for core initializer to call initialize_interface()"
    )


# Expose the initialization function for the core
__all__ = [
    "initialize_interface",
    "shutdown_interface",
    "reload_interface",
    "TelegramInterface",
]
