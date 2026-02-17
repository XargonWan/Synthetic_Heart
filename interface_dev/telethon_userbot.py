from telethon import TelegramClient, events

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover - fallback if python-dotenv not installed

    def load_dotenv(*args, **kwargs):
        return False


import os
import re
import asyncio
import core.plugin_instance as plugin_instance
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config import set_active_llm, list_available_llms, get_active_llm

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover - fallback if python-dotenv not installed

    def load_dotenv(*args, **kwargs):
        return False


from core.interfaces_registry import get_interface_registry

# from core import blocklist, response_proxy, say_proxy, recent_chats  # Moved to plugins
from plugins.blocklist import block_user, unblock_user, get_blocked_users
from core import recent_chats  # For command functions only, not for tracking
from core.auto_response import request_llm_delivery
from core.core_initializer import register_interface, core_initializer

# Load environment variables and get trainer ID
load_dotenv()
_interface_registry = get_interface_registry()

from core.config_manager import config_registry

# Read TRAINER_IDS from the central config registry so runtime changes are respected
TELEGRAM_TRAINER_ID_STR = []
TELEGRAM_TRAINER_ID = None


def _update_trainer_ids(raw_value: str | None):
    global TELEGRAM_TRAINER_ID_STR, TELEGRAM_TRAINER_ID
    s = str(raw_value) if raw_value is not None else ""
    TELEGRAM_TRAINER_ID_STR = s.split(",") if s else []
    TELEGRAM_TRAINER_ID = None
    for trainer_config in TELEGRAM_TRAINER_ID_STR:
        if trainer_config.startswith("telegram_userbot:"):
            try:
                TELEGRAM_TRAINER_ID = int(trainer_config.split(":")[1])
                break
            except (ValueError, IndexError):
                log_warning(
                    f"[telethon_userbot] Invalid trainer ID format in TRAINER_IDS: {trainer_config}"
                )


# Initialize from current config and watch for updates
_update_trainer_ids(config_registry.get_value("TRAINER_IDS", ""))
config_registry.add_listener("TRAINER_IDS", _update_trainer_ids)


def is_trainer(user_id: int) -> bool:
    """Check if user is the trainer for this Telegram interface."""
    return _interface_registry.is_trainer("telegram_userbot", user_id)


def get_trainer_id() -> int:
    """Get the trainer ID for this Telegram interface."""
    return _interface_registry.get_trainer_id("telegram_userbot") or TELEGRAM_TRAINER_ID


load_dotenv()

# Defer TelegramClient initialization if credentials are missing
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION = os.getenv("SESSION", "synth_userbot")

context_memory = {}
last_selected_chat = {}
message_id = None

client = None
if API_ID and API_HASH and TELEGRAM_TRAINER_ID:
    client = TelegramClient(SESSION, int(API_ID), API_HASH)
    register_interface("telegram_userbot", client)

    # Register in the new registry system (avoid duplicate registration)
    _interface_registry.register_interface("telegram_userbot", client)
    _interface_registry.set_trainer_id("telegram_userbot", TELEGRAM_TRAINER_ID)
    log_info(
        f"[telethon_userbot] Registered telegram_userbot interface with trainer ID {TELEGRAM_TRAINER_ID}"
    )

    log_info("[telethon_userbot] Registered TelethonUserbot")
elif not API_ID or not API_HASH:
    log_warning("[telethon_userbot] API_ID or API_HASH missing; userbot disabled")
else:
    log_warning(
        "[telethon_userbot] TELEGRAM_TRAINER_ID not configured; userbot disabled"
    )


def optional_on(*args, **kwargs):
    def decorator(func):
        if client:
            client.on(*args, **kwargs)(func)
        return func

    return decorator


def escape_markdown(text):
    return re.sub(r"([_*\[\]()~`>#+=|{}.!-])", r"\\\1", text)


async def ensure_plugin_loaded(event):
    if plugin_instance.plugin is None:
        try:
            current = await get_active_llm()
            if current:
                await plugin_instance.load_plugin(current)
        except Exception as e:
            log_warning(f"[telethon_userbot] Failed to autoload LLM: {e}")
        if plugin_instance.plugin is None:
            try:
                await plugin_instance.load_plugin("manual")
                log_warning("[telethon_userbot] Falling back to ManualAIPlugin")
            except Exception:
                log_error("No LLM plugin loaded.")
                await event.reply("⚠️ No active LLM plugin. Use .llm to select one.")
                return False
    return True


def resolve_forwarded_target(message):
    if getattr(message, "fwd_from", None):
        if getattr(message.fwd_from, "from_id", None):
            return message.fwd_from.from_id.user_id, message.fwd_from.channel_post
    tracked = plugin_instance.get_target(message.id)
    if tracked:
        return tracked["chat_id"], tracked["message_id"]
    return None, None


@optional_on(events.NewMessage(pattern=r"\.block (\d+)"))
async def block_user_command(event):
    if not is_trainer(event.sender_id):
        return
    try:
        to_block = int(event.pattern_match.group(1))
        await block_user(to_block, "Blocked via command")
        await event.reply(f"🚫 User {to_block} blocked.")
    except Exception:
        await event.reply("❌ Use: .block <user_id>")


@optional_on(events.NewMessage(pattern=r"\.block_list"))
async def block_list(event):
    if not is_trainer(event.sender_id):
        return
    blocked = get_blocked_users()
    if not blocked:
        await event.reply("✅ No blocked users.")
    else:
        await event.reply("🚫 Blocked users:\n" + "\n".join(map(str, blocked)))


@optional_on(events.NewMessage(pattern=r"\.unblock (\d+)"))
async def unblock_user(event):
    if not is_trainer(event.sender_id):
        return
    try:
        to_unblock = int(event.pattern_match.group(1))
        blocklist.unblock_user(to_unblock)
        await event.reply(f"✅ User {to_unblock} unblocked.")
    except Exception:
        await event.reply("❌ Use: .unblock <user_id>")


@optional_on(events.NewMessage(pattern=r"\.last_chats"))
async def last_chats_command(event):
    if not is_trainer(event.sender_id):
        return
    entries = await recent_chats.get_last_active_chats_verbose(10, client)
    if not entries:
        await event.reply("⚠️ No recent chat found.")
        return
    lines = [
        f"[{escape_markdown(name)}](tg://user?id={cid}) — `{cid}`"
        for cid, name in entries
    ]
    await event.reply("🕓 Recent active chats:\n" + "\n".join(lines), parse_mode="md")


@optional_on(events.NewMessage(pattern=r"\.help"))
async def help_command(event):
    if not is_trainer(event.sender_id):
        return
    from core.context import get_context_state

    context_status = "active ✅" if get_context_state() else "inactive ❌"
    llm_mode = "LLM managed centrally in initialize_core_components"
    help_text = (
        f"🧞‍♀️ *synth – Available Commands*\n\n"
        "*🧠 Context Mode*\n"
        f"`.context` – Toggle history in forwarded messages, currently *{context_status}*\n\n"
        "*🧩 Manual Mode*\n"
        "Reply to a forwarded message with text or content (stickers, photos, audio, files, etc.)\n"
        "`.cancel` – Cancel a pending send\n\n"
        "*🧱 User Management*\n"
        "`.block <user_id>` – Block a user\n"
        "`.unblock <user_id>` – Unblock a user\n"
        "`.block_list` – List blocked users\n\n"
        "*⚙️ LLM Mode*\n"
        f"`.llm` – Show and select current engine (active: `{llm_mode}`)\n"
        "\n*📋 Miscellaneous*\n"
        "`.last_chats` – Recent active chats\n"
    )
    await event.reply(help_text, parse_mode="md")


@optional_on(events.NewMessage(pattern=r"\.llm(?: (.+))?"))
async def llm_command(event):
    if not is_trainer(event.sender_id):
        return
    args = event.pattern_match.group(1)
    current = await get_active_llm()
    available = list_available_llms()
    if not args:
        msg = f"*Active LLM:* `{current}`\n\n*Available:*"
        msg += "\n" + "\n".join(f"• `{name}`" for name in available)
        msg += "\n\nTo change: `.llm <name>`"
        await event.reply(msg, parse_mode="md")
        return
    choice = args.strip()
    if choice not in available:
        await event.reply(f"❌ LLM `{choice}` not found.")
        return
    try:
        await set_active_llm(choice)

        # We don't load plugins here - that's the core's job
        # The system will restart with the new LLM on next restart

        await event.reply(
            f"✅ LLM mode updated to `{choice}`. Restart to apply changes."
        )
    except Exception as e:
        await event.reply(f"❌ Error changing LLM: {e}")


# .say command removed — interactive forward-to-chat functionality deprecated.


@optional_on(events.NewMessage())
async def handle_message(event):
    if not await ensure_plugin_loaded(event):
        return
    message = event.message
    if not message or not message.sender_id:
        return
    user_id = message.sender_id
    text = message.message or ""

    # Trainer reply to forwarded message
    if is_trainer(user_id) and message.is_reply:
        reply_msg_id = message.reply_to_msg_id
        original = plugin_instance.get_target(reply_msg_id)
        if original:
            await client.send_message(
                original["chat_id"], text, reply_message_id=original["message_id"]
            )
            await event.reply("✅ Reply sent.")
        else:
            await event.reply("⚠️ No message to reply to found.")
        return
    # Pass to plugin via auto-response system for autonomous userbot interactions
    try:
        await request_llm_delivery(
            message=message,
            interface=client,
            context=context_memory,
            reason="telethon_userbot_autonomous",
        )
    except Exception as e:
        log_error(
            f"auto-response delivery failed for telethon userbot: {e}",
            e,
        )


async def main():
    if not client:
        log_error("Telethon client not initialized.")
        return

    def telegram_notify(chat_id: int, message: str, reply_to_message_id: int = None):
        async def send():
            try:
                await client.send_message(
                    chat_id, message, reply_message_id=reply_to_message_id
                )
                log_debug(f"[notify] Telegram message sent to {chat_id}")
            except Exception as e:
                log_error(f"[notify] Failed to send Telegram message: {repr(e)}", e)

        import asyncio

        asyncio.create_task(send())

    # Initialize core system with notify function
    await core_initializer.initialize_all(notify_fn=telegram_notify)

    log_info("🧞‍♀️ synth Userbot (Telethon) is online.")

    # Register this interface with the core
    core_initializer.register_interface("telegram_userbot")

    client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
