# interface/telegram_bot.py

import os
import re
import asyncio
import subprocess
import time
from typing import Optional
from telegram import Update, Bot
from telegram.error import TelegramError, RetryAfter, BadRequest, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    CommandHandler,
    filters,
)
from dotenv import load_dotenv  # type: ignore
from llm_engines.manual import ManualAIPlugin
from plugins.blocklist import block_user, unblock_user, get_blocked_users
from plugins.message_map import init_message_map_table, cleanup_old_mappings
from core import response_proxy
from core import say_proxy, message_queue
from core.context import context_command
from core import recent_chats  # For command functions only, not for tracking
from core.mention_utils import is_message_for_bot
from collections import deque
import json
from core.logging_utils import log_debug, log_info, log_warning, log_error
from interface.telegram_utils import (
    safe_send,
    send_with_thread_fallback,
)
from core.message_sender import (
    send_content,
    detect_media_type,
    extract_response_target,
)
from core.config import (
    get_active_llm,
    set_active_llm,
    list_available_llms,
    get_log_chat_id,
    set_log_chat_id_and_thread,
    get_log_chat_id_sync,
    get_log_chat_thread_id_sync,
)
from core.command_registry import execute_command, handle_command_message

from plugins.chat_link import ChatLinkStore
from core.prompt_engine import build_full_json_instructions

chat_link_store = ChatLinkStore()
import core.plugin_instance as plugin_instance
import traceback
from core.action_parser import initialize_core
from core.core_initializer import register_interface
from typing import Any
from types import SimpleNamespace
from core.interfaces_registry import get_interface_registry
from core.config_manager import config_registry

# Get interface registry for trainer verification
_interface_registry = get_interface_registry()

# Load environment variables
load_dotenv()

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
    
    for trainer_config in trainer_ids_str.split(','):
        trainer_config = trainer_config.strip()
        if trainer_config.startswith('telegram_bot:'):
            try:
                return int(trainer_config.split(':')[1])
            except (ValueError, IndexError):
                log_warning(f"[telegram_bot] Invalid trainer ID format in TRAINER_IDS: {trainer_config}")
                return None
    
    log_debug("[telegram_bot] No trainer ID found for telegram_bot in TRAINER_IDS")
    return None

def is_trainer(user_id: int) -> bool:
    """Check if user is the trainer for this Telegram interface."""
    return _interface_registry.is_trainer('telegram_bot', user_id)

def get_trainer_id() -> Optional[int]:
    """Get the trainer ID for this Telegram interface."""
    return _interface_registry.get_trainer_id('telegram_bot')

say_sessions = {}
context_memory = {}
last_selected_chat = {}
message_id = None

# Throttling for bot None lookup warnings
_last_bot_none_lookup_log_time = 0
_bot_none_log_throttle_sec = 5

from core.config import LLM_MODE

async def ensure_plugin_loaded(update: Update):
    """
    Check that an LLM plugin has been loaded correctly.
    If absent, reply to the user with an error message and log the issue.
    """
    log_debug(f"[telegram_bot] Checking if plugin is loaded: {plugin_instance.plugin is not None}")
    if plugin_instance.plugin is None:
        log_warning("[telegram_bot] No plugin loaded, attempting to load...")
        try:
            current = await get_active_llm()
            log_debug(f"[telegram_bot] Active LLM from config: {current}")
            if current:
                log_debug(f"[telegram_bot] Loading plugin: {current}")
                await plugin_instance.load_plugin(current, notify_fn=telegram_notify)
                log_debug(f"[telegram_bot] Plugin loaded successfully: {plugin_instance.plugin is not None}")
        except Exception as e:  # pragma: no cover - runtime safeguard
            log_warning(f"[telegram_interface] Failed to autoload LLM: {e}")
        if plugin_instance.plugin is None:
            log_warning("[telegram_bot] Plugin still None, trying manual fallback...")
            try:
                await plugin_instance.load_plugin("manual", notify_fn=telegram_notify)
                log_warning("[telegram_interface] Falling back to ManualAIPlugin")
            except Exception as e:
                log_error(f"[telegram_bot] Manual plugin fallback failed: {e}")
                log_error("No LLM plugin loaded.")
                from core.notifier import notify_trainer
                notify_trainer("⚠️ No LLM plugin active. Use /llm to select one.")
                return False
    else:
        log_debug(f"[telegram_bot] Plugin already loaded: {plugin_instance.plugin.__class__.__name__}")
    return True

def resolve_forwarded_target(message):
    """
    Given a message (presumably a reply to a forwarded message), try to
    reconstruct the original ``chat_id`` and ``message_id`` of the forwarded
    message.
    """

    if hasattr(message, "forward_from_chat") and hasattr(message, "forward_from_message_id"):
        if message.forward_from_chat and message.forward_from_message_id:
            return message.forward_from_chat.id, message.forward_from_message_id

    tracked = plugin_instance.get_target(message.message_id)
    if tracked:
        return tracked["chat_id"], tracked["message_id"]

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
        await update.message.reply_text("\U0001f6ab Blocked users:\n" + "\n".join(map(str, blocked)))

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
        confirmation = f"This chat is now set as logchat [{chat_id}, {thread_id}] on telegram_bot"
        await safe_send(context.bot, chat_id, confirmation, thread_id=thread_id)
    except Exception as e:
        log_error(f"[telegram_interface] Failed to set log chat: {e}")
        await update.message.reply_text("❌ Unable to set log chat.")

# === Generic command for sticker/audio/photo/file/video ===

async def handle_response_command(update: Update, context: ContextTypes.DEFAULT_TYPE, content_type: str):

    if not await ensure_plugin_loaded(update):
        return

    if not is_trainer(update.effective_user.id):
        return

    message = update.message
    if not message.reply_to_message:
        await message.reply_text("⚠️ You must use this command in reply to a message forwarded by Rekku.")
        return

    chat_id, message_id = resolve_forwarded_target(message.reply_to_message)

    if not chat_id or not message_id:
        await message.reply_text("❌ Invalid message for this command.")
        return

    response_proxy.set_target(get_trainer_id(), chat_id, message_id, content_type)
    log_debug(f"Target {content_type} set: chat_id={chat_id}, message_id={message_id}")
    await safe_send(
        context.bot,
        chat_id=get_trainer_id(),
        text=f"📎 Send me the {content_type.upper()} file to use as response."
    )  # [FIX]

async def cancel_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return
    if response_proxy.has_pending(get_trainer_id()):
        response_proxy.clear_target(get_trainer_id())
        say_proxy.clear(get_trainer_id())
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
        "\U0001f553 Last active chats:\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_info(f"[telegram_bot] 🔔 HANDLE_MESSAGE CALLED! Update: {update}")
    log_debug(f"[telegram_bot] Update type: {type(update)}, Message: {update.message if update else 'None'}")
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

    user = message.from_user
    user_id = user.id
    username = user.full_name
    usertag = f"@{user.username}" if user.username else "(no tag)"
    text = message.text or message.caption or ""
    
    # Log with proper content type
    content_description = ""
    if message.photo:
        content_description = f" [photo with caption: '{text}']"
    elif message.document:
        content_description = f" [document: {message.document.file_name or 'unknown'} with caption: '{text}']"
    elif text:
        content_description = f": {text}"
    
    log_info(f"[telegram_bot] Processing message from {username} ({user_id}){content_description}")

    # Track context
    log_debug(f"[telegram_bot] Tracking context for chat {message.chat_id}")
    if message.chat_id not in context_memory:
        context_memory[message.chat_id] = deque(maxlen=10)
    context_memory[message.chat_id].append({
        "message_id": message.message_id,
        "user_id": user_id,
        "username": username,
        "usertag": usertag,
        "text": text,
        "timestamp": message.date.isoformat()
    })
    log_debug(f"[telegram_bot] Context added to memory")
    log_debug(f"context_memory[{message.chat_id}] = {list(context_memory[message.chat_id])}")

    # === PRIORITY 1: Handle /say step (chat selection) ===
    log_debug(f"Checking say_step conditions - chat_type: {message.chat.type}, user_id: {user_id}, trainer_id: {get_trainer_id()}, say_choices: {context.user_data.get('say_choices') is not None}")
    if message.chat.type == "private" and user_id == get_trainer_id() and context.user_data.get("say_choices"):
        log_debug(f"Message intercepted by say_step handler")
        target_chat = say_proxy.get_target(user_id)
        
        if target_chat == "EXPIRED":
            await message.reply_text("⏳ Time expired. Use /say again.")
            return
        
        # If target not yet chosen, try to interpret text as number
        if not target_chat and message.text:
            stripped = message.text.strip()
            if stripped.isdigit():
                try:
                    index = int(stripped) - 1
                    choices = context.user_data.get("say_choices", [])
                    if 0 <= index < len(choices):
                        selected_chat_id = choices[index][0]
                        say_proxy.set_target(user_id, selected_chat_id)
                        context.user_data.pop("say_choices", None)
                        await message.reply_text(
                            "✅ Chat selected.\n\nNow send me the *message*, a *photo*, a *file*, an *audio* or any other content to forward.",
                            parse_mode="Markdown"
                        )
                        return
                except Exception:
                    pass
            
            await message.reply_text("❌ Invalid selection. Send a correct number.")
            return
        
        # Chat selected → forward content through plugin
        if target_chat:
            log_debug(f"Forwarding via plugin_instance.handle_incoming_message (chat_id={target_chat})")
            try:
                await plugin_instance.handle_incoming_message(context.bot, message, context.user_data, "telegram_bot")
                response_proxy.clear_target(get_trainer_id())
                say_proxy.clear(get_trainer_id())
                return
            except Exception as e:
                log_error(f"Error during plugin_instance.handle_incoming_message in /say: {e}", e)
                await message.reply_text("❌ Error sending message.")
                return
    
    # === PRIORITY 2: Handle trainer incoming responses (stickers, media with target) ===
    if message.chat.type == "private" and is_trainer(user_id):
        media_type = detect_media_type(message)
        log_debug(f"Trainer message detected: media_type={media_type}")
        
        # Check if there's a target set (from /say or reply)
        target = response_proxy.get_target(get_trainer_id())
        log_debug(f"Initial target from response_proxy = {target}")
        
        # If replying to a message, search in plugin mapping
        if not target and message.reply_to_message:
            reply = message.reply_to_message
            log_debug(f"Reply to trainer_message_id={reply.message_id}")
            possible_ids = [reply.message_id]
            if reply.reply_to_message:
                possible_ids.append(reply.reply_to_message.message_id)
            
            for mid in possible_ids:
                tracked = plugin_instance.get_target(mid)
                if tracked:
                    target = {
                        "chat_id": tracked["chat_id"],
                        "message_id": tracked["message_id"],
                        "type": media_type
                    }
                    log_debug(f"Found target via plugin_instance.get_target({mid}): {target}")
                    break
        
        # Fallback from /say
        if not target:
            fallback = say_proxy.get_target(get_trainer_id())
            log_debug(f"Fallback from say_proxy = {fallback}")
            if fallback and fallback != "EXPIRED":
                target = {
                    "chat_id": fallback,
                    "message_id": None,
                    "type": media_type
                }
                log_debug(f"Target set from say_proxy: {target}")
            elif fallback == "EXPIRED":
                await message.reply_text("⏳ Timeout expired, run /say again.")
                return
        
        # If we have a target, send the content
        if target:
            chat_id = target["chat_id"]
            reply_message_id = target["message_id"]
            content_type = target["type"]
            
            log_debug(f"Sending media_type={content_type} to chat_id={chat_id}, reply_message_id={reply_message_id}")
            success, feedback = await send_content(context.bot, chat_id, message, content_type, reply_message_id)
            log_debug(f"send_content returned: success={success}, feedback={feedback}")
            
            await message.reply_text(feedback)
            
            if success:
                log_debug("✅ Sending successful. Cleaning proxy.")
                response_proxy.clear_target(get_trainer_id())
                say_proxy.clear(get_trainer_id())
            return

    log_debug(f"After trainer-specific checks - continuing to message processing")
    log_debug(f"After trainer-specific checks - continuing to message processing")
    log_debug(f"Checking if message is for bot - calling is_message_for_bot")
    
    # Check if message is directed to bot
    human_count = getattr(message, "human_count", None)
    if human_count is None and hasattr(message, "chat"):
        human_count = getattr(message.chat, "human_count", None)
    
    log_debug(f"human_count={human_count}, message.chat.type={message.chat.type}")
    
    # Get bot username for mention checking
    bot_username = None
    try:
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username if bot_info else None
        log_debug(f"Bot username: {bot_username}")
    except Exception as e:
        log_debug(f"Could not get bot username: {e}")
    
    directed, reason = await is_message_for_bot(message, context.bot, bot_username=bot_username, human_count=human_count)
    log_debug(f"is_message_for_bot returned directed={directed}, reason='{reason}'")
    
    if not directed:
        log_debug(f"[telegram_bot] DEBUG: Message not directed to bot - ignoring")
        if reason == "missing_human_count":
            log_debug("[telegram_bot] DEBUG: Reason: missing_human_count")
        elif reason == "multiple_humans":
            log_debug("[telegram_bot] DEBUG: Reason: multiple_humans")
        else:
            log_debug(f"[telegram_bot] DEBUG: Reason: {reason or 'not directed to bot'}")
        return
    
    log_debug(f"[telegram_bot] DEBUG: Message is directed to bot - continuing processing")
    
    log_debug(f"[telegram_bot] Message from {user_id} ({message.chat.type}): {text}")

    # === PRIORITY 3: Trainer reply to forwarded message ===
    trainer_id = get_trainer_id()
    log_debug(f"Checking trainer reply conditions - chat_type: {message.chat.type}, user_id: {user_id}, trainer_id: {trainer_id}, has_reply: {bool(message.reply_to_message)}")
    if message.chat.type == "private" and user_id == trainer_id and message.reply_to_message:
        log_debug(f"Processing trainer reply to forwarded message")
        reply_msg_id = message.reply_to_message.message_id
        log_debug(f"Reply to trainer_message_id={reply_msg_id}")
        original = plugin_instance.get_target(reply_msg_id)
        if original:
            log_debug(f"Trainer replies to message {original}")
            await safe_send(
                context.bot,
                chat_id=original["chat_id"],
                text=message.text,
                reply_to_message_id=original["message_id"]
            )
            await message.reply_text("✅ Reply sent.")
        else:
            log_warning("⚠️ No target found for reply. Ensure plugin mapping is correct.")
            await message.reply_text("⚠️ No message found to reply to.")
        return
    else:
        log_debug(f"Not a trainer reply - continuing to queue forwarding")

    # === PRIORITY 4: Forward to centralized queue (default behavior) ===
    log_debug(f"About to forward message to queue: '{text}' from user {user_id}")
    log_debug(f"Checking message_queue module availability")
    
    try:
        log_debug(f"Calling message_queue.enqueue...")
        log_debug(f"Parameters: bot={type(context.bot)}, message={type(message)}, context_memory={type(context_memory)}, interface_id='telegram_bot'")
        
        await message_queue.enqueue(context.bot, message, context_memory, interface_id="telegram_bot", original_message=message)
        
        log_debug(f"Message successfully enqueued - processing should continue in queue")
        
    except Exception as e:
        log_error(f"message_queue enqueue failed: {repr(e)}", e)
        log_error(f"Exception type: {type(e)}", e)
        await message.reply_text("⚠️ Error processing message.")
        

async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic command handler that delegates to centralized command registry."""
    if not update.message or not update.message.text:
        return
    
    command_text = update.message.text
    user_id = update.effective_user.id if update.effective_user else None
    
    # Create interface context for commands that need it
    interface_context = {
        'update': update,
        'context': context,
        'bot': context.bot
    }
    
    try:
        response = await handle_command_message(command_text, user_id, "telegram_bot", interface_context)
        # Only send response if it's not None (meaning command was recognized)
        if response is not None:
            await update.message.reply_text(response, parse_mode="Markdown")
    except Exception as e:
        log_error(f"[telegram_bot] Error handling command: {e}")
        await update.message.reply_text("❌ Error processing command.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return
    help_text = await execute_command("help")
    await update.message.reply_text(help_text, parse_mode="Markdown")

def escape_markdown(text):
    return re.sub(r'([_*\[\]()~`>#+=|{}.!-])', r'\\\1', text)

async def last_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return

    entries = await recent_chats.get_last_active_chats_verbose(10, context.bot)
    if not entries:
        await update.message.reply_text("⚠️ No recent chat found.")
        return

    lines = [f"[{escape_markdown(name)}](tg://user?id={cid}) — `{cid}`" for cid, name in entries]
    await update.message.reply_text(
        "\U0001f553 Last active chats:\n" + "\n".join(lines),
        parse_mode="Markdown"
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
                lines.append(f"{escape_markdown(name)} — `{cid}` -> {escape_markdown(path)}")
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
        await update.message.reply_text(f"✅ Reset mapping for `{cid}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Usage: /manage_chat_id [reset <id>|reset this>")

async def say_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return

    args = context.args
    bot = context.bot

    # Case 1: /say <chat_id> <message>
    if len(args) >= 2:
        try:
            chat_id = int(args[0])
            text = " ".join(args[1:])
            await safe_send(bot, chat_id=chat_id, text=text)  # [FIX]
            await update.message.reply_text("✅ Message sent.")
        except Exception as e:
            log_error(f"Error /say direct: {repr(e)}", e)
            await update.message.reply_text("❌ Error sending.")
        return

    # Case 2: /say @username -> select private chat
    if len(args) == 1 and args[0].startswith("@"):  # /say @username
        username = args[0]
        log_debug(f"Resolving username {username} via bot.get_chat")
        try:
            chat = await bot.get_chat(username)
            log_debug(
                f"Resolved to chat_id = {chat.id}, type = {chat.type}"
            )
            if chat.type == "private":
                say_proxy.set_target(update.effective_user.id, chat.id)
                context.user_data.pop("say_choices", None)
                await update.message.reply_text(
                    f"\u2709\ufe0f What do you want to send to {username}?",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"\u274c Cannot send to {username}. They must start the chat with the bot first."
                )
        except Exception as e:
            log_error(f"Error /say @username: {repr(e)}", e)
            await update.message.reply_text(
                f"❌ Cannot send to {username}. They must start the chat with the bot first."
            )
        return

    # Case 3: /say (no arguments) -> show recent chats
    all_entries = await recent_chats.get_last_active_chats_verbose(20, bot)
    entries = all_entries[:10]
    if not entries:
        await update.message.reply_text("⚠️ No recent chat found.")
        return

    # Save list in memory and show options
    numbered = "\n".join(
        f"{i+1}. {escape_markdown(name)} — `{cid}`"
        for i, (cid, name) in enumerate(entries)
    )

    # Additional list of recent private chats
    privates = [(cid, name) for cid, name in all_entries if cid > 0][:5]
    if privates:
        private_lines = "\n".join(
            f"{i+1}. {escape_markdown(name)} — `{cid}`"
            for i, (cid, name) in enumerate(privates)
        )
        numbered += "\n\n🔒 Recent private chats:\n" + private_lines

    numbered += "\n\n✏️ Reply with the number to choose the chat."

    say_proxy.clear(update.effective_user.id)  # Ensure cleanup before choice
    context.user_data["say_choices"] = entries

    await update.message.reply_text(numbered, parse_mode="Markdown")

async def llm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_info(f"[telegram_bot] LLM command received from user {update.effective_user.id}")
    
    if not is_trainer(update.effective_user.id):
        log_warning(f"[telegram_bot] LLM command rejected: user {update.effective_user.id} != get_trainer_id() {get_trainer_id()}")
        return

    args = context.args
    log_info(f"[telegram_bot] LLM command args: {args}")
    
    current = await get_active_llm()
    available = list_available_llms()

    if not args:
        msg = f"*Active LLM:* `{current}`\n\n*Available:*"
        msg += "\n" + "\n".join(f"• `{name}`" for name in available)
        msg += "\n\nTo change: `/llm <name>`"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    choice = args[0]
    if choice not in available:
        await update.message.reply_text(f"❌ LLM `{choice}` not found.")
        return

    try:
        from core.config import set_active_llm
        await set_active_llm(choice)
        
        # Reload system with new LLM
        from core.core_initializer import core_initializer
        await core_initializer.initialize_all(notify_fn=telegram_notify)
        
        await update.message.reply_text(f"✅ LLM mode dynamically updated to `{choice}`.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error loading plugin: {e}")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_trainer(update.effective_user.id):
        return

    try:
        models = plugin_instance.get_supported_models()
    except Exception:
        await update.message.reply_text("⚠️ This plugin does not support model selection.")
        return

    if not models:
        await update.message.reply_text("⚠️ No models available for this plugin.")
        return

    if not context.args:
        current = plugin_instance.get_current_model() or models[0]
        msg = f"*Available models:*\n" + "\n".join(f"• `{m}`" for m in models)
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


async def start_bot():
    """Start the Telegram bot application.
    
    This function assumes the core has already been initialized.
    It should be called from TelegramInterface.start() or during autostart.
    """
    log_info("[telegram_bot] start_bot() function called")
    
    if not BOTFATHER_TOKEN:
        log_warning("[telegram_bot] BOTFATHER_TOKEN not configured - skipping Telegram bot startup")
        return
    
    # Parse trainer ID from configuration
    trainer_id = _parse_trainer_id_from_config()
    if not trainer_id:
        log_warning("[telegram_bot] No trainer ID found in TRAINER_IDS - skipping Telegram bot startup")
        return
    
    # Set trainer ID in the registry (interface is already registered at import time)
    _interface_registry.set_trainer_id('telegram_bot', trainer_id)
    log_info(f"[telegram_bot] Set trainer ID {trainer_id} for telegram_bot interface")

    try:
        log_info("[telegram_bot] Building Telegram application...")
        
        # Check if we should disable SSL verification (for dev environments with cert issues)
        import os
        disable_ssl = os.getenv("TELEGRAM_DISABLE_SSL_VERIFY", "0") == "1"
        
        if disable_ssl:
            log_warning("[telegram_bot] ⚠️ SSL verification DISABLED - use only in development!")
            # Monkey-patch httpx to disable SSL verification globally for this process
            import httpx
            import ssl
            # Store original client init
            original_client_init = httpx.AsyncClient.__init__
            # Create wrapper that forces verify=False
            def patched_client_init(self, *args, **kwargs):
                kwargs['verify'] = False
                return original_client_init(self, *args, **kwargs)
            # Apply monkey patch
            httpx.AsyncClient.__init__ = patched_client_init
            log_debug("[telegram_bot] Patched httpx.AsyncClient to disable SSL verification")
        
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
            .read_timeout(30.0)     # Increased from default ~5s to 30s
            .write_timeout(30.0)    # Increased from default ~5s to 30s
            .pool_timeout(10.0)     # Connection pool timeout
            .build()
        )
        log_info("[telegram_bot] Telegram application built successfully")
        log_info(f"[telegram_bot] get_trainer_id() configured as: {get_trainer_id()}")
        bot_token_status = 'Yes' if str(BOTFATHER_TOKEN).strip() else 'No'
        log_info(f"[telegram_bot] BOTFATHER_TOKEN configured: {bot_token_status}")

        log_info("[telegram_bot] Adding command handlers...")
        # Use generic command handler for all commands
        app.add_handler(MessageHandler(filters.COMMAND, handle_command))
        
        # Single unified message handler for ALL non-command messages
        log_info("[telegram_bot] Adding unified MessageHandler for all messages...")
        app.add_handler(MessageHandler(
            (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL | 
            filters.Sticker.ALL | filters.AUDIO | filters.VOICE | filters.VIDEO, 
            handle_message
        ))
        log_info("[telegram_bot] All handlers added successfully")
        
        # Add error handler to catch any exceptions
        async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            """Log errors caused by updates."""
            log_error(f"[telegram_bot] Exception while handling an update: {context.error}")
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
        global telegram_interface
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
        
        # Keep running until interrupted
        log_info("[telegram_bot] Starting polling...")
        await app.updater.start_polling()
        log_info("[telegram_bot] Polling started successfully")
        
        # This keeps the application running
        log_info("[telegram_bot] Bot is now running and listening for messages...")
        await asyncio.Event().wait()  # Wait forever until interrupted
    except Exception as e:
        log_error(f"[telegram_bot] Error in bot polling: {repr(e)}")
        raise
    finally:
        log_info("[telegram_bot] Shutting down Telegram application...")
        await app.stop()
        await app.shutdown()
        log_info("[telegram_bot] Telegram application shutdown completed")

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
            log_warning(f"[telegram_interface] Interface loaded in disabled state: {self.disabled_reason}")
        elif not _parse_trainer_id_from_config():
            self.disabled_reason = "No trainer ID configured in TRAINER_IDS"
            log_warning(f"[telegram_interface] Interface loaded in disabled state: {self.disabled_reason}")
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
                    if now - _last_bot_none_lookup_log_time >= _bot_none_log_throttle_sec:
                        log_warning("[telegram_interface] Bot is None, cannot lookup chat name")
                        _last_bot_none_lookup_log_time = now
                except NameError:
                    # Variable not defined, log without throttling
                    log_warning("[telegram_interface] Bot is None, cannot lookup chat name")
                return {"chat_name": None, "message_thread_name": None}
            try:
                chat = await b.getChat(chat_id)
                chat_name = getattr(chat, "title", None) or getattr(chat, "username", None)
            except Exception as e:  # pragma: no cover - network failures
                log_warning(f"[telegram_interface] chat name lookup failed: {e}")
            if thread_id:
                try:
                    # Check if getForumTopic method exists (available in newer versions of python-telegram-bot)
                    if hasattr(b, 'getForumTopic'):
                        topic = await b.getForumTopic(chat_id, thread_id)
                        thread_name = getattr(topic, "name", None) or getattr(topic, "title", None)
                    else:
                        log_debug("[telegram_interface] getForumTopic method not available, skipping thread name lookup")
                        thread_name = None
                except Exception as e:  # pragma: no cover
                    log_warning(f"[telegram_interface] thread name lookup failed: {e}")
                    thread_name = None
            return {"chat_name": chat_name, "message_thread_name": thread_name}

        ChatLinkStore.set_name_resolver("telegram", _resolver)
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
            log_warning("[telegram_interface] Telegram interface disabled: no token configured")
            return
        
        trainer_id = _parse_trainer_id_from_config()
        if not trainer_id:
            self._disable("No trainer ID configured in TRAINER_IDS")
            log_warning("[telegram_interface] Telegram interface disabled: no trainer ID")
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
                "required_fields": ["text"],
                "optional_fields": [
                    "target",
                    "chat_name",
                    "thread_id",
                    "message_thread_name",
                ],
                "description": "Send a text message via Telegram",
            },
            "audio_telegram_bot": {
                "required_fields": ["audio"],
                "optional_fields": [
                    "target",
                    "chat_name",
                    "thread_id",
                    "message_thread_name",
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
                    "text": {"type": "string", "example": "Hello!", "description": "The message text to send"},
                    "target": {
                        "type": "string",
                        "example": "-123456789",
                        "description": "Numeric chat_id or chat_name of the recipient. Use input.payload.source.chat_id to reply in the same chat.",
                        "optional": True,
                    },
                    "chat_name": {
                        "type": "string",
                        "example": "Il covo di Rekku",
                        "description": "Alternative to target for specifying the chat by name",
                        "optional": True,
                    },
                    "thread_id": {
                        "type": "integer",
                        "example": 456,
                        "description": "Thread ID when replying in a topic/thread. OMIT this field for main chat replies (interface will use default). Only include when replying IN a specific thread!",
                        "optional": True,
                    },
                    "message_thread_name": {
                        "type": "string",
                        "example": "Generale",
                        "description": "Alternative to thread_id to specify the thread by name",
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
                    "ALWAYS specify target field - use input.payload.source.chat_id to reply in the same chat",
                    "When replying to a message that was in a thread/topic, ALWAYS include thread_id to ensure the reply appears in the correct thread",
                    "If you omit thread_id when it should be included, the message may appear in the main chat instead of the thread",
                    "For group chats with topics enabled, check if the original message has a thread_id and include it in your response"
                ]
            }
        if action_name == "audio_telegram_bot":
            return {
                "description": "Send a voice message via Telegram bot",
                "payload": {
                    "audio": {"type": "string", "example": "/path/to/file.ogg", "description": "Path to the voice file"},
                    "target": {
                        "type": "string",
                        "example": "-123456789",
                        "description": "Numeric chat_id or chat_name of the recipient",
                        "optional": True,
                    },
                    "chat_name": {
                        "type": "string",
                        "example": "Il covo di Rekku",
                        "description": "Alternative to target for specifying the chat by name",
                        "optional": True,
                    },
                    "thread_id": {
                        "type": "integer",
                        "example": 456,
                        "description": "Optional thread ID for group chats",
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

        target = payload.get("target")
        chat_name = payload.get("chat_name")
        if target is None and chat_name is None:
            errors.append("payload.target or payload.chat_name is required")
        else:
            if target is not None:
                if isinstance(target, dict):
                    chat_id = target.get("chat_id")
                    thread_id = target.get("thread_id")
                    if chat_id is not None and not isinstance(chat_id, (int, str)):
                        errors.append("payload.target.chat_id must be an int or string")
                    if thread_id is not None and not isinstance(thread_id, int):
                        errors.append("payload.target.thread_id must be an int")
                elif not isinstance(target, (int, str)):
                    errors.append("payload.target must be an int, string or dict")

        thread_id = payload.get("thread_id")
        if thread_id is not None and not isinstance(thread_id, int):
            errors.append("payload.thread_id must be an int")

        thread_name = payload.get("message_thread_name")
        if thread_name is not None and not isinstance(thread_name, str):
            errors.append("payload.message_thread_name must be a string")

        return errors

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
            log_error(f"[telegram_interface] System error BLOCKED - step: {step}, details: {details}, payload: {payload}")
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

    async def send_message(self, payload: dict, original_message: object | None = None) -> None:
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
        if self.bot is None:
            log_warning("[telegram_interface] Bot not initialized, cannot send message")
            return

        text = payload.get("text", "")
        target = payload.get("target")
        chat_name = payload.get("chat_name")
        thread_id = payload.get("thread_id")
        thread_name = payload.get("message_thread_name")

        # LLM must explicitly specify target - no auto-injection
        # Auto-inject thread_id if missing and available (thread_id auto-inject is still useful)
        if original_message is not None and thread_id is None and hasattr(original_message, "thread_id"):
            thread_id = original_message.thread_id
            log_debug(f"[telegram_interface] Auto-injected thread_id from original message: {thread_id}")

        log_debug(
            f"[telegram_interface] Sending to target={target} chat_name={chat_name} thread_id={thread_id} thread_name={thread_name}"
        )

        if not text or (target is None and chat_name is None):
            log_warning("[telegram_interface] Missing text or destination, aborting")
            return

        chat_id = None

        if isinstance(target, dict):
            chat_id = target.get("chat_id")
            thread_id = target.get("thread_id", thread_id)
            thread_name = target.get("message_thread_name", thread_name)
        elif target is not None:
            if isinstance(target, str) and not target.lstrip("-").isdigit():
                chat_name = target
            else:
                try:
                    chat_id = int(target)
                except Exception:
                    chat_name = target

        if chat_id is None or (thread_id is None and thread_name is not None):
            try:
                row = await chat_link_store.resolve(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    chat_name=chat_name,
                    message_thread_name=thread_name,
                )
            except ChatLinkMultipleMatches:
                # Use orchestrator instead of legacy corrector
                try:
                    from core import action_parser
                    import json
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
                msg.from_llm = False
                if action_parser is not None:
                    try:
                        await action_parser.corrector_orchestrator(text=msg.original_text, context={"interface": "telegram"}, bot=self.bot, message=msg)
                    except Exception:
                        pass
                return
            if not row:
                # Use orchestrator instead of legacy corrector for not-found
                try:
                    from core import action_parser
                    import json
                    from types import SimpleNamespace
                    from datetime import datetime
                except Exception:
                    action_parser = None
                correction_payload = {
                    "system_message": {
                        "type": "error",
                        "message": f"Channel or thread not found for name {chat_name or thread_name}",
                        "your_reply": payload,
                    }
                }
                msg = SimpleNamespace()
                msg.chat_id = None
                msg.text = ""
                msg.original_text = json.dumps(correction_payload, ensure_ascii=False)
                msg.thread_id = None
                msg.date = datetime.utcnow()
                msg.from_llm = False
                if action_parser is not None:
                    try:
                        await action_parser.corrector_orchestrator(text=msg.original_text, context={"interface": "telegram"}, bot=self.bot, message=msg)
                    except Exception:
                        pass
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
            reply_message_id = original_message.message_id
            log_debug(f"[telegram_interface] reply_to_message_id: {reply_message_id}")
            
            # Also set thread_id from original message if not already set
            if thread_id is None and hasattr(original_message, "thread_id"):
                orig_thread_id = getattr(original_message, "thread_id")
                if orig_thread_id is not None:
                    thread_id = orig_thread_id
                    log_debug(f"[telegram_interface] thread_id from original message: {thread_id}")

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
                fallback_reply_to = original_message.message_id
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
                    log_debug(f"[telegram_interface] Using original message thread for same chat: {thread_id}")

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
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                # Use orchestrator instead of legacy corrector
                try:
                    from core import action_parser
                    import json
                    from types import SimpleNamespace
                    from datetime import datetime
                except Exception:
                    action_parser = None
                correction_payload = {
                    "system_message": {
                        "type": "error",
                        "message": f"Chat {chat_id} not found",
                        "your_reply": payload,
                    }
                }
                msg = SimpleNamespace()
                msg.chat_id = chat_id
                msg.text = ""
                msg.original_text = json.dumps(correction_payload, ensure_ascii=False)
                msg.thread_id = thread_id
                msg.date = datetime.utcnow()
                msg.from_llm = False
                if action_parser is not None:
                    try:
                        await action_parser.corrector_orchestrator(text=msg.original_text, context={"interface": "telegram"}, bot=self.bot, message=msg)
                    except Exception:
                        pass
                return
            else:
                # Generic error -> request correction via orchestrator
                try:
                    from core import action_parser
                    import json
                    from types import SimpleNamespace
                    from datetime import datetime
                except Exception:
                    action_parser = None
                correction_payload = {
                    "system_message": {
                        "type": "error",
                        "message": str(e),
                        "your_reply": payload,
                    }
                }
                msg = SimpleNamespace()
                msg.chat_id = chat_id
                msg.text = ""
                msg.original_text = json.dumps(correction_payload, ensure_ascii=False)
                msg.thread_id = thread_id
                msg.date = datetime.utcnow()
                msg.from_llm = False
                if action_parser is not None:
                    try:
                        await action_parser.corrector_orchestrator(text=msg.original_text, context={"interface": "telegram"}, bot=self.bot, message=msg)
                    except Exception:
                        pass
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
        log_debug(f"[telegram_interface] add_reaction called with emoji '{emoji}' for message {getattr(message, 'message_id', 'unknown')}")
        try:
            log_debug(f"[telegram_interface] self.bot is None: {self.bot is None}")
            if not self.bot:
                log_warning("[telegram_interface] Bot instance is None, cannot add reaction")
                return False
            
            chat_id = getattr(message, 'chat_id', None) or getattr(message.chat, 'id', None)
            message_id = getattr(message, 'message_id', None)
            
            if not chat_id or not message_id:
                log_warning("[telegram_interface] Cannot add reaction: missing chat_id or message_id")
                return False
            
            log_debug(f"[telegram_interface] Adding reaction '{emoji}' to chat_id={chat_id}, message_id={message_id}")
            
            # Check if method exists
            if not hasattr(self.bot, 'set_message_reaction'):
                log_warning("[telegram_interface] set_message_reaction method not available in this version of python-telegram-bot")
                return False
            
            await self.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=emoji,
                is_big=False
            )
            log_info(f"[telegram_interface] Successfully added reaction '{emoji}' to message {message_id}")
            return True
        except Exception as e:
            log_warning(f"[telegram_interface] Failed to add reaction '{emoji}': {e} - chat_id={chat_id}, message_id={message_id}, type={type(e).__name__}")
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
    telegram_interface = TelegramInterface(None)  # Bot instance will be set later during startup
    register_interface("telegram_bot", telegram_interface)
    log_info("[telegram_bot] Telegram interface instance created and registered")
    
    return telegram_interface


def shutdown_interface():
    """Shutdown and cleanup the Telegram interface.
    
    Called before reload or shutdown to properly cleanup resources.
    """
    global telegram_interface
    
    if telegram_interface is None:
        log_debug("[telegram_bot] No interface to shutdown")
        return
    
    log_info("[telegram_bot] Shutting down Telegram interface...")
    
    try:
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
        log_info("[telegram_bot] BOTFATHER_TOKEN and trainer ID configured - scheduling Telegram bot startup")
        
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
                log_debug("[telegram_bot] No event loop running, bot will start when application initializes")
        
        # Try to schedule immediately
        try:
            _schedule_telegram_startup()
        except Exception as e:
            log_debug(f"[telegram_bot] Could not schedule startup immediately: {e}")
            # This is expected during import - the main app will handle it
    else:
        reason = telegram_interface.disabled_reason if telegram_interface else "interface not initialized"
        log_info(f"[telegram_bot] Telegram bot will not auto-start: {reason}")
else:
    log_debug("[telegram_bot] Waiting for core initializer to call initialize_interface()")


# Expose the initialization function for the core
__all__ = ['initialize_interface', 'shutdown_interface', 'reload_interface', 'TelegramInterface']



