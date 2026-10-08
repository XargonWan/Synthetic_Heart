# interface/telegram/telegram.py
"""Telegram *user account* interface (MTProto via Telethon, real phone number).

Counterpart of ``interface/telegram_bot`` (BotFather token): this one logs in as
a regular Telegram user, so SyntH appears as a normal contact.  Incoming
messages are wrapped in PTB-shaped objects (see ``_compat``) and flow through
the same single message chain as every other interface.
"""

import asyncio
import os
import sys
import time
from types import SimpleNamespace
from typing import Any, Optional

from core import message_queue, response_proxy
from core.chat_attention import evaluate_triggers, get_attention, set_attention
from core.command_registry import handle_command_message
from core.config_manager import config_registry
from core.core_initializer import register_interface
from core.interface_paths import resolve_and_touch, set_name_resolver
from core.interfaces_registry import get_interface_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.mention_utils import is_message_for_bot
from core.message_sender import detect_media_type, send_content
from core.variables_engine import register_exposed_var
from interface.message_send_utils import (
    safe_send,
    send_with_thread_fallback,
    telegram_parse_mode_for,
)
from interface.telegram._compat import (
    INTERFACE_ID,
    BadRequest,
    TelethonBot,
    TgContext,
    TgMessage,
    TgUpdate,
    wrap_message,
)
import core.plugin_instance as plugin_instance

_interface_registry = get_interface_registry()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

register_exposed_var(
    "TELEGRAM_API_ID",
    label="Telegram API ID",
    default=None,
    value_type=str,
    ui_type="password",
    description="API id from https://my.telegram.org (API development tools).",
    scope="interface",
    tags=["sensitive"],
    needs_component_reload=True,
    component=INTERFACE_ID,
)
register_exposed_var(
    "TELEGRAM_API_HASH",
    label="Telegram API Hash",
    default=None,
    value_type=str,
    ui_type="password",
    description="API hash from https://my.telegram.org (API development tools).",
    scope="interface",
    tags=["sensitive"],
    needs_component_reload=True,
    component=INTERFACE_ID,
)
register_exposed_var(
    "TELEGRAM_PHONE",
    label="Telegram Phone Number",
    default=None,
    value_type=str,
    ui_type="string",
    description="Phone number of the Telegram account, international format (+39...).",
    scope="interface",
    component=INTERFACE_ID,
)
# Written only by the WebUI login flow (never through POST /api/config).
register_exposed_var(
    "TELEGRAM_SESSION",
    label="Telegram Session",
    default=None,
    value_type=str,
    ui_type="password",
    description="Telethon StringSession created by the login panel.",
    scope="interface",
    tags=["sensitive"],
    needs_component_reload=True,
    hidden=True,
    readonly=True,
    component=INTERFACE_ID,
)
# Opt-in: this is a real person's account, so its profile picture is visible to
# every contact. Off by default; turning it on applies the core synth avatar.
register_exposed_var(
    "TELEGRAM_USE_SYNTH_AVATAR",
    label="Use Synth Avatar",
    default=False,
    value_type=bool,
    ui_type="bool",
    description=(
        "Set the synth avatar (Settings → Synth Avatar) as this account's profile "
        "picture, and keep it updated when the avatar changes. Visible to all your "
        "Telegram contacts."
    ),
    scope="interface",
    needs_component_reload=True,
    component=INTERFACE_ID,
)
# Version of the avatar this interface last applied (avoids re-uploading on every
# start, which Telegram rate-limits).
register_exposed_var(
    "TELEGRAM_AVATAR_APPLIED",
    label="Telegram Applied Avatar Version",
    default="",
    value_type=str,
    ui_type="string",
    description="Internal: synth avatar version last applied to this account.",
    scope="interface",
    hidden=True,
    readonly=True,
    component=INTERFACE_ID,
)


def _declare(key: str, label: str, sensitive: bool = False, **extra: Any) -> Any:
    return config_registry.get_var(
        key,
        None,
        label=label,
        group="interface",
        component=INTERFACE_ID,
        sensitive=sensitive,
        **extra,
    )


TELEGRAM_API_ID = _declare("TELEGRAM_API_ID", "Telegram API ID", sensitive=True)
TELEGRAM_API_HASH = _declare("TELEGRAM_API_HASH", "Telegram API Hash", sensitive=True)
TELEGRAM_PHONE = _declare("TELEGRAM_PHONE", "Telegram Phone Number")
TELEGRAM_SESSION = _declare(
    "TELEGRAM_SESSION",
    "Telegram Session",
    sensitive=True,
    hidden=True,
    readonly=True,
    allow_env_override=False,
    needs_component_reload=True,
)


def _cfg(key: str) -> str:
    value = config_registry.get_value(key, None)
    return str(value).strip() if value else ""


def _cfg_bool(key: str) -> bool:
    value = config_registry.get_value(key, None)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_trainer_id_from_config() -> Optional[int]:
    """Extract the trainer id for this interface from TRAINER_IDS (``telegram:<id>``)."""
    raw = config_registry.get_var(
        "TRAINER_IDS",
        "",
        label="Trainer IDs",
        description="Comma-separated list of trainer IDs for each interface (format: interface_name:user_id)",
        group="core",
        component=INTERFACE_ID,
    )
    for entry in (str(raw) if raw else "").split(","):
        entry = entry.strip()
        if entry.startswith(f"{INTERFACE_ID}:"):
            try:
                return int(entry.split(":", 1)[1])
            except (ValueError, IndexError):
                log_warning(
                    f"[telegram] Invalid trainer ID format in TRAINER_IDS: {entry}"
                )
                return None
    return None


def is_trainer(user_id: int) -> bool:
    return _interface_registry.is_trainer(INTERFACE_ID, user_id)


def get_trainer_id() -> Optional[int]:
    raw = _interface_registry.get_trainer_id(INTERFACE_ID)
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


class MessageWrapper:
    """Thin wrapper adding attributes (text override, flags) to a message."""

    def __init__(self, message: Any, **extra_attrs: Any) -> None:
        self._message = message
        self._extra = extra_attrs

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        if name in self._extra:
            return self._extra[name]
        return getattr(self._message, name)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _ensure_plugin_loaded() -> bool:
    """Make sure an LLM plugin is loaded (same fallback ladder as telegram_bot)."""
    if plugin_instance.plugin is not None:
        return True
    try:
        from core.config import get_active_cortex_engine

        current = await get_active_cortex_engine()
        if current:
            await plugin_instance.load_plugin(current, notify_fn=telegram_notify)
    except Exception as exc:  # pragma: no cover - runtime safeguard
        log_warning(f"[telegram] Failed to autoload Cortex: {exc}")
    if plugin_instance.plugin is None:
        try:
            await plugin_instance.load_plugin("manual", notify_fn=telegram_notify)
        except Exception as exc:
            log_error(f"[telegram] Manual plugin fallback failed: {exc}")
            return False
    return True


def telegram_notify(
    chat_id: int, message: str, reply_to_message_id: Optional[int] = None
) -> None:
    """Notify the trainer (and the log chat) through the user account."""
    if chat_id != get_trainer_id():
        log_debug(f"[telegram] notify ignored: {chat_id} is not the trainer")
        return
    bot = telegram_interface.bot if telegram_interface is not None else None
    if bot is None:
        log_debug("[telegram] notify skipped: client not connected")
        return

    async def _runner() -> None:
        from core.config import get_log_chat_id_sync

        targets = [chat_id]
        log_chat = get_log_chat_id_sync()
        if log_chat and log_chat not in targets:
            targets.append(log_chat)
        for target in targets:
            try:
                await safe_send(
                    bot,
                    chat_id=target,
                    text=message,
                    reply_to_message_id=reply_to_message_id
                    if target == chat_id
                    else None,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                log_error(f"[telegram] notify failed for {target}: {exc!r}")

    try:
        asyncio.get_running_loop().create_task(_runner())
    except RuntimeError:
        asyncio.run(_runner())


async def _resolve_original_from_reply(
    reply_message: Any,
) -> tuple[Optional[int], Optional[int]]:
    """Resolve the original (chat_id, message_id) of a forwarded/relayed message."""
    candidates = [getattr(reply_message, "message_id", None)]
    inner = getattr(reply_message, "reply_to_message", None)
    if inner is not None:
        candidates.append(getattr(inner, "message_id", None))

    for mid in candidates:
        if not mid:
            continue
        try:
            tracked = plugin_instance.get_target(mid)
            if asyncio.iscoroutine(tracked):
                tracked = await tracked
        except Exception as exc:
            log_error(f"[telegram] plugin mapping lookup failed: {exc}")
            tracked = None
        if isinstance(tracked, (list, tuple)) and len(tracked) >= 2:
            return int(tracked[0]), int(tracked[1])
        if (
            isinstance(tracked, dict)
            and "chat_id" in tracked
            and "message_id" in tracked
        ):
            return int(tracked["chat_id"]), int(tracked["message_id"])
        try:
            from plugins.message_map import get_original_message

            mapped = await get_original_message(int(mid))
            if mapped and isinstance(mapped, (list, tuple)) and len(mapped) >= 2:
                return int(mapped[0]), int(mapped[1])
        except Exception as exc:
            log_debug(f"[telegram] message_map lookup failed for {mid}: {exc}")

    chat = getattr(reply_message, "forward_from_chat", None)
    origin_mid = getattr(reply_message, "forward_from_message_id", None)
    if chat is not None and origin_mid:
        return int(chat.id), int(origin_mid)

    import re

    text = getattr(reply_message, "text", "") or ""
    match = re.search(r"original message from chat\s+(-?\d+)\s+id\s+(\d+)", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


async def _forward_to_queue(
    bot: TelethonBot,
    message: Any,
    wrapped: Any,
    *,
    directed: bool,
    media_future: Optional[asyncio.Future] = None,
) -> None:
    kwargs: dict[str, Any] = {
        "interface_id": INTERFACE_ID,
        "original_message": message,
        "skip_mention_check": directed,
    }
    if media_future is not None:
        kwargs["media_future"] = media_future
    await message_queue.enqueue(bot, wrapped, **kwargs)


# ---------------------------------------------------------------------------
# Media (voice / video notes / video)
# ---------------------------------------------------------------------------


async def handle_media_live(update: Any, context: TgContext) -> None:
    """Transcribe incoming voice/video via Auris (fallback: media dispatcher)."""
    from core.reaction_handler import get_reaction_emoji, react_when_mentioned
    from core.core_initializer import INTERFACE_REGISTRY

    message = update.message
    bot = context.bot
    try:
        directed, _reason = await is_message_for_bot(message, bot)
    except Exception as exc:
        log_debug(f"[telegram] mention check failed: {exc}")
        directed = False
    if not directed:
        log_debug("[telegram] media message not directed; skipping")
        return

    emoji = get_reaction_emoji()
    try:
        iface = INTERFACE_REGISTRY.get(INTERFACE_ID)
        if emoji and iface:
            await react_when_mentioned(iface, message, emoji)
    except Exception as exc:
        log_debug(f"[telegram] reaction skipped/failed: {exc}")

    file_ref = None
    media_hint = "audio"
    if message.voice:
        file_ref, media_hint = message.voice, "audio/ogg"
        if not emoji:
            await message.set_reaction("👂")
        await bot.send_chat_action(message.chat_id, "record_voice")
    elif message.video_note:
        file_ref, media_hint = message.video_note, "video/mp4"
        await message.set_reaction("👀")
        await bot.send_chat_action(message.chat_id, "record_video")
    elif message.video:
        file_ref, media_hint = message.video, "video/mp4"
        await message.set_reaction("👀")
        await bot.send_chat_action(message.chat_id, "record_video")
    if file_ref is None:
        log_warning("[telegram] live_media: no attachment available")
        return

    # Reserve a NORMAL_PRIORITY slot now so Grillo beats cannot start while we
    # download/transcribe (same trick as telegram_bot).
    media_future: asyncio.Future = asyncio.get_event_loop().create_future()
    await _forward_to_queue(
        bot, message, message, directed=True, media_future=media_future
    )

    input_path = None
    try:
        temp_dir = os.path.join(os.getcwd(), "tmp", "live_io")
        os.makedirs(temp_dir, exist_ok=True)
        ext = ".oga" if "audio" in media_hint else ".mp4"
        input_path = os.path.join(
            temp_dir, f"in_{message.message_id}_{int(time.time())}{ext}"
        )
        download = await bot.get_file(file_ref.file_id)
        await download.download_to_drive(custom_path=input_path)

        transcribed: Optional[str] = None
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            auris = PLUGIN_REGISTRY.get("auris_plugin")
            if auris is not None:
                result = await auris.transcribe_audio(input_path, media_hint)
                transcribed = result.text if result else None
        except Exception as exc:
            log_warning(f"[telegram] Auris path failed ({exc}); trying fallback")
        if not transcribed:
            try:
                from core.media_dispatcher import dispatch_media

                transcribed = await dispatch_media(input_path, media_hint)
            except Exception as exc:
                log_warning(f"[telegram] dispatch_media failed: {exc}")

        wrapped = MessageWrapper(
            message,
            text=transcribed or (getattr(message, "caption", "") or ""),
            is_voice_input=True,
            request_tts=True,
        )
        if not media_future.done():
            media_future.set_result(wrapped)
    except Exception as exc:
        log_error(f"[telegram] Error handling live media: {exc}")
        if not media_future.done():
            media_future.set_exception(exc)
        try:
            await message.reply_text(f"⚠️ Error processing media: {exc}")
        except Exception:
            pass
    finally:
        if input_path and os.path.exists(input_path):
            try:
                os.remove(input_path)
            except OSError as exc:
                log_error(f"[telegram] Failed to cleanup {input_path}: {exc}")


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

_BYPASS_SLEEP_TRIGGERS = (
    "weather report",
    "weather check",
    "how is the weather",
    "weather forecast",
    "check weather",
    "weather status",
)


async def handle_message(update: Any, context: TgContext) -> None:
    message: Optional[TgMessage] = update.message if update else None
    bot = context.bot
    if message is None or not message.from_user:
        log_debug("[telegram] message ignored (empty or no sender)")
        return
    if not await _ensure_plugin_loaded():
        log_error("[telegram] plugin loading failed, aborting message processing")
        return

    # Reply to someone's media addressed to us: transcribe/promote that media.
    reply_msg = message.reply_to_message
    if reply_msg is not None and any(
        getattr(reply_msg, a, None)
        for a in ("voice", "video", "video_note", "photo", "sticker", "document")
    ):
        directed, _ = await is_message_for_bot(message, bot)
        if directed:
            if any(
                getattr(reply_msg, a, None) for a in ("voice", "video", "video_note")
            ):
                try:
                    await handle_media_live(SimpleNamespace(message=reply_msg), context)
                except Exception as exc:
                    log_error(f"[telegram] live media handler failed: {exc}")
                return
            for attr in ("photo", "sticker", "document"):
                value = getattr(reply_msg, attr, None)
                if value is not None:
                    setattr(message, attr, value)

    if message.voice or message.video_note or message.video:
        try:
            directed, _ = await is_message_for_bot(message, bot)
        except Exception as exc:
            log_debug(f"[telegram] mention check failed on media routing: {exc}")
            directed = False
        if not directed:
            return
        try:
            await handle_media_live(update, context)
        except Exception as exc:
            log_error(f"[telegram] live media handler failed: {exc}")
        return

    user = message.from_user
    user_id = user.id
    username = user.full_name
    text = message.text or message.caption or ""
    thread_id = message.thread_id

    from core.interface_path_utils import build_interface_path

    interface_path = build_interface_path(
        INTERFACE_ID, str(message.chat_id), str(thread_id) if thread_id else None
    )

    reply_meta: Optional[dict] = None
    if reply_msg is not None and not (
        message.is_topic_message and reply_msg.message_id == thread_id
    ):
        reply_from = reply_msg.from_user
        reply_meta = {
            "reply_to": {
                "sender_name": (reply_from.full_name if reply_from else None)
                or "Unknown",
                "text": reply_msg.text or reply_msg.caption or "",
                "message_id": reply_msg.message_id,
            }
        }
    try:
        from core.chat_context_manager import add_message_to_context

        await add_message_to_context(
            interface_path=interface_path,
            message_text=text,
            sender_name=username,
            sender_id=str(user_id),
            message_id=message.message_id,
            timestamp=message.date.isoformat() if message.date else None,
            metadata=reply_meta,
        )
    except Exception as exc:
        log_warning(f"[telegram] Failed to add message to context: {exc}")

    try:
        await resolve_and_touch(
            interface_path,
            str(message.chat_id),
            str(thread_id) if thread_id else None,
            bot=bot,
        )
    except Exception as exc:
        log_debug(f"[telegram] resolve_and_touch failed (non-fatal): {exc}")
    try:
        from core.peer_policy import notify_message_arrived

        notify_message_arrived(interface_path)
    except Exception as exc:
        log_debug(f"[telegram] notify_message_arrived failed (non-fatal): {exc}")

    # --- PRIORITY 2: trainer sends media for a pending response target ---
    if message.chat.type == "private" and is_trainer(user_id):
        media_type = detect_media_type(message)
        target = response_proxy.get_target(get_trainer_id())
        if not target and message.reply_to_message:
            try:
                chat_id, orig_msg_id = await _resolve_original_from_reply(
                    message.reply_to_message
                )
                if chat_id and orig_msg_id:
                    target = {
                        "chat_id": chat_id,
                        "message_id": orig_msg_id,
                        "type": media_type,
                    }
            except Exception as exc:
                log_debug(f"[telegram] reply target resolution failed: {exc}")
        if target:
            success, feedback = await send_content(
                bot, target["chat_id"], message, target["type"], target["message_id"]
            )
            await message.reply_text(feedback)
            if success:
                response_proxy.clear_target(get_trainer_id())
            return

    # --- Wake / sleep gating ---
    chat_id = message.chat.id
    is_awake = get_attention(chat_id, True)
    text_lower = text.lower().strip()
    should_sleep, is_wake_word, is_wake_sleep_command = evaluate_triggers(text_lower)
    bot_username = bot.username
    is_mention = bool(bot_username and f"@{bot_username.lower()}" in text_lower)
    should_wake = (is_wake_word or is_mention) and not should_sleep

    if should_sleep and is_awake:
        set_attention(chat_id, False)
        is_awake = True  # let the goodbye through; the next message sees "asleep"
        await message.set_reaction("😴")
    elif should_wake and not is_awake:
        set_attention(chat_id, True)
        is_awake = True
        await message.set_reaction("👀")

    if any(t in text_lower for t in _BYPASS_SLEEP_TRIGGERS) and not should_sleep:
        is_awake = True

    if not is_awake and not should_wake and not should_sleep:
        if not (is_trainer(user_id) and message.chat.type == "private"):
            log_debug(f"[telegram] chat {chat_id} is asleep; ignoring message")
            return

    # In groups never fall back on the single-human heuristic (false positives).
    human_count = (
        None if message.chat.type in ("group", "supergroup") else message.human_count
    )
    directed, reason = await is_message_for_bot(
        message, bot, bot_username=bot_username, human_count=human_count
    )
    if directed:
        try:
            from core.reaction_handler import get_reaction_emoji, react_when_mentioned
            from core.core_initializer import INTERFACE_REGISTRY

            emoji = get_reaction_emoji()
            iface = INTERFACE_REGISTRY.get(INTERFACE_ID)
            if emoji and iface:
                await react_when_mentioned(iface, message, emoji)
        except Exception as exc:
            log_debug(f"[telegram] reaction skipped/failed: {exc}")
    if not directed:
        log_debug(
            f"[telegram] message not directed to us ({reason or 'no reason'}); ignoring"
        )
        return

    # --- PRIORITY 3: trainer replies to a relayed message ---
    trainer_id = get_trainer_id()
    if (
        message.chat.type == "private"
        and user_id == trainer_id
        and message.reply_to_message
    ):
        orig_chat, orig_msg = await _resolve_original_from_reply(
            message.reply_to_message
        )
        if orig_chat and orig_msg:
            await safe_send(
                bot, chat_id=orig_chat, text=message.text, reply_to_message_id=orig_msg
            )
            await message.reply_text("✅ Reply sent.")
            return
        replied = message.reply_to_message
        import re as _re

        looks_relayed = bool(
            replied.forward_from_chat
            or replied.forward_from_message_id
            or _re.search(
                r"original message from chat\s+(-?\d+)\s+id\s+(\d+)", replied.text or ""
            )
        )
        if looks_relayed:
            await message.reply_text("⚠️ No message found to reply to.")
            return

    wrapped = MessageWrapper(
        message,
        text=text,
        is_wake_sleep_command=is_wake_sleep_command,
        is_voice_input=False,
    )

    async def _forward() -> None:
        try:
            await _forward_to_queue(bot, message, wrapped, directed=directed)
        except Exception as exc:
            log_error(f"[telegram] message_queue enqueue failed: {exc!r}")
            await message.reply_text("⚠️ Error processing message.")

    # --- PRIORITY 3.5: peer turn coordination (shared group roleplay) ---
    if message.chat.type in ("group", "supergroup"):
        try:
            from core.peer_policy import (
                get_peer_ids,
                get_relay_wait_peer,
                is_peer_mode_enabled,
                peer_already_responded,
                wait_for_peer_reply,
            )

            if is_peer_mode_enabled() and get_peer_ids():
                relay_peer = get_relay_wait_peer(text) if text else None
                if relay_peer is not None:

                    async def _wait_then_forward(peer: int = relay_peer) -> None:
                        await wait_for_peer_reply(
                            interface_path, peer, since=message.date
                        )
                        await _forward()

                    asyncio.create_task(_wait_then_forward())
                    return
                floor = float(
                    config_registry.get_value("SYNTH_PEER_TURN_FLOOR_SECONDS", 0.0)
                )
                if floor > 0:
                    await asyncio.sleep(floor)
                    if await peer_already_responded(interface_path, since=message.date):
                        log_debug("[telegram] peer already responded; yielding turn")
                        return
        except Exception as exc:
            log_debug(f"[telegram] peer turn coordination skipped (non-fatal): {exc}")

    await _forward()


async def handle_command(update: Any, context: TgContext) -> None:
    """Delegate ``/commands`` to the centralized command registry."""
    message = update.message
    if message is None or not message.text:
        return
    user_id = message.from_user.id if message.from_user else None
    interface_context = {
        "update": update,
        "context": context,
        "bot": context.bot,
        "interface_id": INTERFACE_ID,
    }
    parts = message.text.split()
    context.args = parts[1:]
    try:
        response = await handle_command_message(
            message.text, user_id, INTERFACE_ID, interface_context
        )
        if response is not None:
            try:
                await message.reply_text(response, parse_mode="Markdown")
            except Exception as md_err:
                log_error(
                    f"[telegram] Markdown parse error, retrying plain text: {md_err}"
                )
                await message.reply_text(response)
    except Exception as exc:
        log_error(f"[telegram] Error handling command: {exc}")
        await message.reply_text("❌ Error processing command.")


async def _on_new_message(bot: TelethonBot, event: Any) -> None:
    """Telethon ``NewMessage`` entry point."""
    try:
        message = await wrap_message(bot, event.message)
        update = TgUpdate(message=message)
        context = TgContext(bot=bot)
        text = message.text or ""
        if text.startswith("/"):
            await handle_command(update, context)
        else:
            await handle_message(update, context)
    except Exception as exc:
        log_error(f"[telegram] Exception while handling an update: {exc!r}")


# ---------------------------------------------------------------------------
# Delivery failure recording
# ---------------------------------------------------------------------------


async def _record_telegram_delivery_failure(
    *,
    chat_id: str | int | None,
    thread_id: str | int | None,
    interface_path: str | None,
    reason: str,
    payload: Optional[dict],
) -> None:
    """Persist a failed delivery so a lost reply is never silent (best effort)."""
    try:
        from core.llm_failure_log import build_failure_entry, record_failure_entry

        text = payload.get("text") if isinstance(payload, dict) else None
        entry = build_failure_entry(
            reason=f"Telegram delivery failed: {reason}",
            stage="delivery",
            failure_code="delivery_failed",
            interface_path=(
                interface_path
                if isinstance(interface_path, str) and interface_path.strip()
                else (f"{INTERFACE_ID}/{chat_id}" if chat_id is not None else None)
            ),
            chat_id=chat_id,
            thread_id=thread_id,
            content_preview=(text[:300] if isinstance(text, str) else None),
            metadata={
                "payload_keys": sorted(payload.keys())
                if isinstance(payload, dict)
                else []
            },
        )
        await record_failure_entry(entry)
    except Exception as exc:  # pragma: no cover - diagnostics only
        log_debug(f"[telegram] Could not record delivery failure: {exc}")


# ---------------------------------------------------------------------------
# Interface class
# ---------------------------------------------------------------------------


class TelegramUserInterface:
    """Interface wrapper around a Telethon user client."""

    display_name = "Telegram (User)"

    # Credentials are required for the interface to load; the login session is
    # handled separately (grey "login required" state, see start()).
    required_config_vars = ["TELEGRAM_API_ID", "TELEGRAM_API_HASH"]

    def __init__(self) -> None:
        self.bot: Optional[TelethonBot] = None
        self.client: Any = None
        self.is_enabled = False
        self.disabled_reason: Optional[str] = None
        self._start_lock = asyncio.Lock()

        if not self._have_credentials():
            self.disabled_reason = "TELEGRAM_API_ID / TELEGRAM_API_HASH not configured"
        elif not _cfg("TELEGRAM_SESSION"):
            self.disabled_reason = "Telegram login required"
        else:
            self.is_enabled = True

        async def _resolver(
            chat_id: Any, thread_id: Any, bot_instance: Any = None
        ) -> dict:
            b = bot_instance or self.bot
            chat_name = thread_name = None
            if b is None:
                return {"chat_name": None, "message_thread_name": None}
            try:
                chat = await b.get_chat(chat_id)
                chat_name = chat.effective_name
            except Exception as exc:  # pragma: no cover - network failures
                log_warning(f"[telegram] chat name lookup failed: {exc}")
            if thread_id:
                thread_name = await self._lookup_topic_name(chat_id, thread_id)
            return {"chat_name": chat_name, "message_thread_name": thread_name}

        set_name_resolver(INTERFACE_ID, _resolver)
        self._register_custom_validation()

    @staticmethod
    def _have_credentials() -> bool:
        return bool(_cfg("TELEGRAM_API_ID") and _cfg("TELEGRAM_API_HASH"))

    async def _lookup_topic_name(self, chat_id: Any, thread_id: Any) -> Optional[str]:
        """Forum-topic title (MTProto exposes it, unlike the Bot API)."""
        if self.client is None:
            return None
        try:
            from telethon import functions

            peer = await self.client.get_input_entity(TelethonBot._peer(chat_id))
            res = await self.client(
                functions.messages.GetForumTopicsByIDRequest(
                    peer=peer, topics=[int(thread_id)]
                )
            )
            topics = getattr(res, "topics", None) or []
            return getattr(topics[0], "title", None) if topics else None
        except Exception as exc:
            log_debug(f"[telegram] topic name lookup failed: {exc}")
            return None

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        async with self._start_lock:
            if self.client is not None:
                return
            if not self._have_credentials():
                self._disable("TELEGRAM_API_ID / TELEGRAM_API_HASH not configured")
                return
            session = _cfg("TELEGRAM_SESSION")
            if not session:
                self._disable("Telegram login required")
                log_info("[telegram] no session stored: log in from the WebUI panel")
                return
            try:
                api_id = int(_cfg("TELEGRAM_API_ID"))
            except ValueError:
                self._disable("TELEGRAM_API_ID must be a number")
                return

            from telethon import TelegramClient, events
            from telethon.sessions import StringSession

            client = TelegramClient(
                StringSession(session), api_id, _cfg("TELEGRAM_API_HASH")
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    self._disable("Telegram session expired: log in again")
                    return
                bot = TelethonBot(client)
                me = await bot.refresh_me()
            except Exception as exc:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                self._disable(f"Startup failed: {type(exc).__name__}")
                log_error(f"[telegram] startup failed: {exc!r}")
                return

            trainer_id = _parse_trainer_id_from_config()
            if trainer_id:
                _interface_registry.set_trainer_id(INTERFACE_ID, trainer_id)
            else:
                log_warning(
                    "[telegram] No trainer ID in TRAINER_IDS - trainer-only features unavailable"
                )

            async def _handler(event: Any) -> None:
                await _on_new_message(bot, event)

            client.add_event_handler(_handler, events.NewMessage(incoming=True))
            self.client, self.bot = client, bot
            self.is_enabled, self.disabled_reason = True, None

            # Warm the entity cache so sends to known dialogs resolve by id.
            asyncio.create_task(self._warm_dialogs(client))
            asyncio.create_task(self._sync_avatar())
            await message_queue.run()
            try:
                from core.core_initializer import core_initializer

                await core_initializer.refresh_actions_block()
            except Exception as exc:
                log_debug(f"[telegram] refresh_actions_block failed: {exc}")
            log_info(f"[telegram] connected as {me.full_name} (id={me.id})")

    @staticmethod
    async def _warm_dialogs(client: Any) -> None:
        try:
            await client.get_dialogs(limit=200)
        except Exception as exc:
            log_debug(f"[telegram] dialog warm-up failed: {exc}")

    # -- synth avatar (core/synth_avatar.py) ---------------------------------------

    @staticmethod
    def uses_synth_avatar() -> bool:
        return _cfg_bool("TELEGRAM_USE_SYNTH_AVATAR")

    async def _sync_avatar(self) -> None:
        """Bring the account picture in line with the core avatar after startup."""
        try:
            if not self.uses_synth_avatar():
                return
            from core import synth_avatar

            loaded = synth_avatar.load_avatar()
            if loaded is None:
                return
            data, version = loaded
            await self.set_avatar(data, synth_avatar.AVATAR_MIME, version)
        except Exception as exc:  # never let the avatar break startup
            log_warning(f"[telegram] avatar sync failed: {type(exc).__name__}: {exc}")

    async def set_avatar(
        self,
        image_bytes: Optional[bytes],
        mime: Optional[str] = None,
        version: Optional[str] = None,
    ) -> bool:
        """Set (or, for ``None``, remove) the account profile picture.

        Only acts when the interface opted in. A picture this interface did not
        set is never deleted: removal needs a recorded applied version.
        """
        client = self.client
        if client is None or not self.uses_synth_avatar():
            return False
        applied = _cfg("TELEGRAM_AVATAR_APPLIED")
        from telethon import errors as tl_errors
        from telethon import functions, utils

        try:
            if image_bytes is None:
                if not applied:
                    return False
                photos = await client.get_profile_photos("me", limit=1)
                if photos:
                    await client(
                        functions.photos.DeletePhotosRequest(
                            id=[utils.get_input_photo(photos[0])]
                        )
                    )
                await config_registry.set_value("TELEGRAM_AVATAR_APPLIED", "")
                log_info("[telegram] synth avatar removed from the account")
                return True
            if version and version == applied:
                log_debug("[telegram] synth avatar already applied; skipping upload")
                return False
            uploaded = await client.upload_file(image_bytes, file_name="avatar.png")
            await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
            await config_registry.set_value("TELEGRAM_AVATAR_APPLIED", version or "")
            log_info("[telegram] synth avatar applied to the account")
            return True
        except tl_errors.FloodWaitError as exc:
            log_warning(
                f"[telegram] avatar update rate-limited, retry in {exc.seconds}s"
            )
            return False
        except Exception as exc:
            log_warning(f"[telegram] avatar update failed: {type(exc).__name__}")
            return False

    async def stop(self) -> None:
        client, self.client, self.bot = self.client, None, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:
                log_debug(f"[telegram] disconnect error: {exc}")

    def _disable(self, reason: str) -> None:
        self.is_enabled = False
        self.disabled_reason = reason

    # -- WebUI hooks ------------------------------------------------------------

    async def run_action(
        self, action: str, payload: Optional[dict] = None, context: Any = None
    ) -> dict:
        """Generic ``POST /api/components/run`` entry point (status + logout)."""
        if action == "logout":
            return await logout_account()
        return {"status": "ok", "login": login_status()}

    # -- contract -----------------------------------------------------------------

    @staticmethod
    def get_interface_id() -> str:
        return INTERFACE_ID

    @staticmethod
    def get_supported_actions() -> dict:
        from core.message_registry import get_send_message_schema

        return {"send_message": get_send_message_schema([INTERFACE_ID])}

    @staticmethod
    def get_prompt_instructions(action_name: str) -> Optional[dict]:
        from plugins.vox_plugin import is_vox_enabled

        if action_name != "send_message":
            return None
        payload: dict[str, Any] = {
            "text": {
                "type": "string",
                "example": "Hello!",
                "description": "The message text to send; also the caption for media.",
            },
            "interface_path": {
                "type": "string",
                "example": "telegram/123456789/456",
                "description": (
                    "Destination path 'telegram/chat_id' or 'telegram/chat_id/thread_id'. "
                    "OPTIONAL when replying to an incoming message (auto-routes to the origin "
                    "conversation); REQUIRED for spontaneous messages. Use "
                    "input.payload.source.interface_path verbatim when present."
                ),
            },
            "media": {
                "type": "array",
                "example": ["data/photo.png"],
                "description": (
                    "Optional list of file paths to attach (image/video/audio/document, "
                    "auto-detected). Must be inside Synth's filesystem sandbox."
                ),
                "optional": True,
            },
            "reply_to": {
                "type": "integer",
                "example": 12345,
                "description": "Optional ID of the message to reply to",
                "optional": True,
            },
        }
        notes = [
            "CRITICAL: ALWAYS use interface_path from input.payload.source.interface_path to reply in same conversation!",
            "Format: 'telegram/chat_id' for regular chats or 'telegram/chat_id/thread_id' for topics",
            "Never use just chat_id or target - always use the complete interface_path format",
            "You are writing from a real user account, not a bot: write like a person, no bot commands.",
        ]
        if is_vox_enabled():
            payload["send_as_voice"] = {
                "type": "boolean",
                "example": True,
                "description": (
                    "Optional, defaults to false. When true, 'text' is synthesised into a voice note. "
                    "Use SPARINGLY: only when the user explicitly asked for voice/audio or sent a voice note."
                ),
                "optional": True,
            }
            notes.append(
                "send_as_voice defaults to false. Reply with voice only when the user asked for audio or sent a voice message."
            )
        return {
            "description": "Send a message from the Telegram user account",
            "payload": payload,
            "important_notes": notes,
        }

    @staticmethod
    def validate_payload(action_type: str, payload: dict) -> list:
        errors: list = []
        if action_type != "send_message":
            return errors
        text, media = payload.get("text"), payload.get("media")
        media_list = media if isinstance(media, list) else ([media] if media else [])
        if not (isinstance(text, str) and text) and not media_list:
            errors.append("payload.text or payload.media is required")
        elif text is not None and not isinstance(text, str):
            errors.append("payload.text must be a string")

        voice = payload.get("send_as_voice")
        if voice is not None and not isinstance(voice, bool):
            if isinstance(voice, str) and voice.strip().lower() in (
                "true",
                "1",
                "yes",
                "on",
            ):
                payload["send_as_voice"] = True
            elif isinstance(voice, str) and voice.strip().lower() in (
                "false",
                "0",
                "no",
                "off",
                "",
            ):
                payload["send_as_voice"] = False
            else:
                errors.append("payload.send_as_voice must be a boolean")
        if payload.get("interface_path") is not None and not isinstance(
            payload["interface_path"], str
        ):
            errors.append("payload.interface_path must be a string")
        if payload.get("chat_name") is not None and not isinstance(
            payload["chat_name"], str
        ):
            errors.append("payload.chat_name must be a string")
        return errors

    async def execute_action(
        self, action: dict, context: dict, bot: Any, original_message: Any
    ) -> dict:
        action_type = action.get("type")
        log_warning(
            f"[telegram] execute_action called for unknown action {action_type}"
        )
        return {"status": "failed", "message": f"Unknown action {action_type}"}

    def _register_custom_validation(self) -> None:
        try:
            from core.validation_registry import ValidationRule, get_validation_registry

            def _validate(payload: dict) -> list:
                return self.validate_payload("send_message", dict(payload))

            rule = ValidationRule(
                action_type="send_message",
                required_fields=[],
                one_of_groups=[["text", "media"]],
                custom_validator=_validate,
                component_name=INTERFACE_ID,
                applies_to_interface=INTERFACE_ID,
            )
            get_validation_registry().register_component_rules(INTERFACE_ID, [rule])
        except Exception as exc:
            log_warning(f"[telegram] Failed to register custom validation: {exc}")

    # -- sending ---------------------------------------------------------------------

    @staticmethod
    def _normalize_media_list(raw_media: Any) -> list:
        if not raw_media:
            return []
        if isinstance(raw_media, (list, tuple)):
            return [str(m) for m in raw_media if m]
        return [str(raw_media)]

    async def _ensure_client(self) -> bool:
        if self.bot is None:
            await self.start()
        return self.bot is not None

    @staticmethod
    def _numeric_thread(thread_id: Any) -> Optional[str]:
        if thread_id is None:
            return None
        cleaned = str(thread_id).strip()
        return cleaned if cleaned.isdigit() else None

    @staticmethod
    def _explicit_reply(reply_to: Any, target: Any) -> Optional[int]:
        """A usable message id for ``reply_to`` (models often put the chat id there)."""
        if reply_to is None:
            return None
        try:
            value = int(reply_to)
        except (TypeError, ValueError):
            log_warning(f"[telegram] Discarding non-numeric reply_to {reply_to!r}")
            return None
        if str(value) == str(target):
            log_warning(
                "[telegram] reply_to is the chat id, not a message id; ignoring it"
            )
            return None
        return value

    async def _send_media(
        self,
        media_items: list,
        interface_path: Optional[str],
        chat_name: Optional[str],
        caption: str = "",
        send_as_voice: bool = False,
        reply_to: Any = None,
        original_message: Any = None,
        skip_history: bool = False,
    ) -> bool:
        from core.outbound_file_utils import (
            MEDIA_AUDIO,
            MEDIA_IMAGE,
            MEDIA_VIDEO,
            classify_media,
            resolve_safe_outbound_path,
        )

        if not await self._ensure_client() or self.bot is None:
            log_warning("[telegram] client not connected for media send")
            return False
        bot = self.bot

        target = thread_id = None
        if interface_path:
            from core.interface_path_utils import extract_legacy_ids

            ids = extract_legacy_ids(interface_path)
            target, thread_id = ids.get("chat_id"), ids.get("thread_id")
        if not target and isinstance(chat_name, str) and chat_name:
            log_warning(
                f"[telegram] Cannot resolve media destination from chat_name {chat_name!r}"
            )
            return False
        if not target:
            origin_chat = getattr(original_message, "chat_id", None)
            if origin_chat is not None:
                target = str(origin_chat)
                thread_id = thread_id or getattr(original_message, "thread_id", None)
        if not target:
            log_warning("[telegram] Missing target for media send")
            return False
        thread_id = self._numeric_thread(thread_id)

        explicit_reply = self._explicit_reply(reply_to, target)
        if (
            explicit_reply is None
            and original_message is not None
            and str(target) == str(getattr(original_message, "chat_id", ""))
        ):
            try:
                explicit_reply = int(original_message.message_id)
            except (AttributeError, TypeError, ValueError):
                explicit_reply = None

        first_caption: Optional[str] = caption or None
        if first_caption and len(first_caption) > 1024:
            await self.send_message(
                {
                    "text": first_caption,
                    "interface_path": interface_path,
                    "chat_name": chat_name,
                }
            )
            first_caption = None

        sent_any, ok_all = False, True
        for item in media_items:
            resolved, err = resolve_safe_outbound_path(item)
            if err or resolved is None:
                log_warning(f"[telegram] Rejected media path {item!r}: {err}")
                ok_all = False
                continue
            kind = classify_media(resolved)
            common: dict[str, Any] = {
                "chat_id": target,
                "caption": first_caption,
                "message_thread_id": thread_id,
                "reply_to_message_id": explicit_reply,
            }
            try:
                with open(resolved, "rb") as handle:
                    if send_as_voice and kind == MEDIA_AUDIO:
                        await bot.send_voice(voice=handle, **common)
                    elif kind == MEDIA_IMAGE:
                        await bot.send_photo(photo=handle, **common)
                    elif kind == MEDIA_VIDEO:
                        await bot.send_video(video=handle, **common)
                    elif kind == MEDIA_AUDIO:
                        await bot.send_audio(audio=handle, **common)
                    else:
                        await bot.send_document(document=handle, **common)
                sent_any = True
            except Exception as exc:
                log_error(f"[telegram] Failed to send media {item!r}: {exc}")
                ok_all = False
            finally:
                first_caption = None

        if sent_any and not skip_history:
            try:
                from core.chat_context_manager import save_response_message
                from core.interface_path_utils import build_interface_path

                await save_response_message(
                    build_interface_path(INTERFACE_ID, str(target), thread_id or None),
                    caption or "[media]",
                )
            except Exception as exc:
                log_debug(f"[telegram] Failed to save media response: {exc}")
        return ok_all and sent_any

    async def send_message(
        self, payload: dict, original_message: object | None = None
    ) -> bool:
        import json

        if not await self._ensure_client():
            log_warning("[telegram] client not connected, cannot send message")
            return False

        text = payload.get("text", "")
        interface_path = payload.get("interface_path")
        chat_name = payload.get("chat_name")

        media_items = self._normalize_media_list(payload.get("media"))
        if media_items:
            return await self._send_media(
                media_items,
                interface_path,
                chat_name,
                caption=text if isinstance(text, str) else "",
                send_as_voice=bool(payload.get("send_as_voice")),
                reply_to=payload.get("reply_to") or payload.get("reply_to_message_id"),
                original_message=original_message,
                skip_history=bool(payload.get("skip_history", False)),
            )

        try:
            from core.text_utils import normalize_for_outbound

            normalized = normalize_for_outbound(text)
            if normalized and normalized != text:
                text = normalized
        except Exception:
            pass

        if not interface_path and not chat_name and payload.get("target") is None:
            log_debug(
                "[telegram] Skipping send: no destination (likely synthetic event message)"
            )
            return True

        thread_id = payload.get("thread_id")
        target = payload.get("target")
        if interface_path:
            from core.interface_path_utils import extract_legacy_ids

            ids = extract_legacy_ids(interface_path)
            target, thread_id = ids.get("chat_id"), ids.get("thread_id")

        if not text or (target is None and chat_name is None):
            log_warning("[telegram] Missing text or destination, aborting")
            return False

        from core.transport_layer import notify_corrector_of_system_message

        if target is None:
            correction = {
                "system_message": {
                    "type": "error",
                    "message": f"Cannot resolve a destination from chat_name {chat_name!r}; please repeat your previous message using the numeric chat_id instead.",
                    "your_reply": payload,
                }
            }
            await notify_corrector_of_system_message(
                json.dumps(correction, ensure_ascii=False),
                self.bot,
                chat_id=None,
                thread_id=None,
                interface=INTERFACE_ID,
            )
            return False

        chat_id = str(target)
        thread_id = self._numeric_thread(thread_id)
        if (
            thread_id is None
            and payload.get("thread_id") is not None
            and not interface_path
        ):
            log_warning(
                f"[telegram] Discarding non-numeric thread_id {payload.get('thread_id')!r}"
            )

        if interface_path:
            await resolve_and_touch(interface_path, chat_id, thread_id, bot=self.bot)

        explicit_raw = payload.get("reply_to") or payload.get("reply_to_message_id")
        reply_message_id = self._explicit_reply(explicit_raw, chat_id)
        if explicit_raw is not None and reply_message_id is None:
            explicit_raw = None

        orig_chat = (
            getattr(original_message, "chat_id", None) if original_message else None
        )
        orig_thread = (
            getattr(original_message, "thread_id", None) if original_message else None
        )
        fallback_chat_id = fallback_thread_id = fallback_reply_to = None
        if original_message is not None and orig_chat is not None:
            if chat_id == str(orig_chat):
                if explicit_raw is None:
                    try:
                        reply_message_id = int(original_message.message_id)  # type: ignore[attr-defined]
                    except (AttributeError, TypeError, ValueError):
                        reply_message_id = None
                if thread_id is None and orig_thread is not None:
                    thread_id = str(orig_thread)
            else:
                fallback_chat_id, fallback_thread_id = orig_chat, orig_thread
                try:
                    fallback_reply_to = int(original_message.message_id)  # type: ignore[attr-defined]
                except (AttributeError, TypeError, ValueError):
                    fallback_reply_to = None

        correction_payload = {
            "system_message": {
                "type": "error",
                "message": "Telegram delivery failed. Please repeat your previous message using an explicit chat_id or interface_path.",
                "your_reply": payload,
            }
        }
        try:
            await send_with_thread_fallback(
                self.bot,
                chat_id,
                text,
                parse_mode=telegram_parse_mode_for(chat_id),
                thread_id=int(thread_id) if thread_id else None,
                reply_to_message_id=reply_message_id,
                fallback_chat_id=fallback_chat_id,
                fallback_thread_id=fallback_thread_id,
                fallback_reply_to_message_id=fallback_reply_to,
                interface_path=interface_path
                or f"{INTERFACE_ID}/{chat_id}" + (f"/{thread_id}" if thread_id else ""),
            )
            if not payload.get("skip_history", False):
                try:
                    from core.chat_context_manager import save_response_message
                    from core.interface_path_utils import build_interface_path

                    await save_response_message(
                        build_interface_path(INTERFACE_ID, chat_id, thread_id), text
                    )
                except Exception as exc:
                    log_debug(
                        f"[telegram] Failed to save response via context_manager: {exc}"
                    )
        except BadRequest as exc:
            await _record_telegram_delivery_failure(
                chat_id=chat_id,
                thread_id=int(thread_id) if thread_id else None,
                interface_path=interface_path,
                reason=str(exc),
                payload=payload,
            )
            await notify_corrector_of_system_message(
                json.dumps(correction_payload, ensure_ascii=False),
                self.bot,
                chat_id=chat_id,
                thread_id=int(thread_id) if thread_id else None,
                interface=INTERFACE_ID,
            )
            return False
        except Exception as exc:
            await _record_telegram_delivery_failure(
                chat_id=chat_id,
                thread_id=int(thread_id) if thread_id else None,
                interface_path=interface_path,
                reason=f"{type(exc).__name__}: {exc}",
                payload=payload,
            )
            raise
        return True

    async def add_reaction(self, message: Any, emoji: str) -> bool:
        if not await self._ensure_client() or self.bot is None:
            return False
        chat_id = getattr(message, "chat_id", None) or getattr(
            getattr(message, "chat", None), "id", None
        )
        message_id = getattr(message, "message_id", None)
        if not chat_id or not message_id:
            log_warning("[telegram] Cannot add reaction: missing chat_id or message_id")
            return False
        try:
            return await self.bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id, reaction=emoji, is_big=False
            )
        except Exception as exc:
            log_warning(f"[telegram] Failed to add reaction '{emoji}': {exc}")
            return False


# ---------------------------------------------------------------------------
# Login / status helpers used by the WebUI panel
# ---------------------------------------------------------------------------


def login_status() -> dict:
    from interface.telegram._login import FLOW

    iface = telegram_interface
    connected = bool(iface and iface.bot is not None)
    return {
        "connected": connected,
        "account": (
            iface.bot.me.full_name
            if connected and iface and iface.bot and iface.bot.me
            else None
        ),
        "has_credentials": TelegramUserInterface._have_credentials(),
        "has_session": bool(_cfg("TELEGRAM_SESSION")),
        "phone": _cfg("TELEGRAM_PHONE") or None,
        "login": FLOW.snapshot(),
    }


async def logout_account() -> dict:
    """Terminate the session server-side and forget it locally."""
    iface = telegram_interface
    try:
        if iface is not None and iface.client is not None:
            await iface.client.log_out()
    except Exception as exc:
        log_warning(f"[telegram] server-side log_out failed: {type(exc).__name__}")
    await config_registry.set_value("TELEGRAM_SESSION", "", require_persist=True)
    return {"status": "ok", "login": login_status()}


# ---------------------------------------------------------------------------
# Module-level lifecycle (mirrors telegram_bot)
# ---------------------------------------------------------------------------

telegram_interface: Optional[TelegramUserInterface] = None


def initialize_interface() -> TelegramUserInterface:
    """Create (or recreate) the interface after config has been loaded."""
    global telegram_interface
    if telegram_interface is not None:
        log_info("[telegram] Reloading interface with updated configuration...")
        shutdown_interface()
    telegram_interface = TelegramUserInterface()
    register_interface(INTERFACE_ID, telegram_interface)
    _register_webui_panel()
    return telegram_interface


def shutdown_interface() -> None:
    global telegram_interface
    iface = telegram_interface
    if iface is None:
        return
    client, iface.client, iface.bot = iface.client, None, None
    if client is not None:
        try:
            asyncio.get_running_loop().create_task(client.disconnect())
        except RuntimeError:
            pass
    try:
        from core.core_initializer import INTERFACE_REGISTRY

        INTERFACE_REGISTRY.pop(INTERFACE_ID, None)
    except Exception as exc:
        log_debug(f"[telegram] unregister failed: {exc}")
    telegram_interface = None


def reload_interface() -> TelegramUserInterface:
    log_info("[telegram] Reloading Telegram user interface...")
    return initialize_interface()


def _register_webui_panel() -> None:
    try:
        from interface.telegram.webui_login import schedule_registration

        schedule_registration()
    except Exception as exc:
        log_debug(f"[telegram] WebUI panel registration skipped: {exc}")


# Register at import time when API credentials exist so the login panel is
# reachable before any session exists. Never under pytest.
if (
    "pytest" not in sys.modules
    and telegram_interface is None
    and TelegramUserInterface._have_credentials()
):
    initialize_interface()


__all__ = [
    "initialize_interface",
    "shutdown_interface",
    "reload_interface",
    "TelegramUserInterface",
]
