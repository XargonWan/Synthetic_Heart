# cortex/external_engines/dev/manual.py

from plugins.message_map import (
    init_message_map_table,
    store_message_mapping,
    get_original_message,
    cleanup_old_mappings,
)

# say_proxy removed: /say command deprecated
import asyncio
from core.config import get_trainer_id
from core.ai_plugin_base import AIPluginBase
import json

try:
    from telegram.constants import ParseMode
except Exception:
    # Provide a lightweight fallback for tests where python-telegram-bot is not available
    from types import SimpleNamespace

    ParseMode = SimpleNamespace(MARKDOWN="MARKDOWN")
from core.logging_utils import log_debug, log_info, log_warning, log_error
from interface.message_send_utils import safe_send
import time

# Manual AI-specific configuration
# Note: max_prompt_chars will be read from the active LLM engine at runtime
# This is just a safe fallback if the active LLM isn't available
MANUAL_CONFIG = {
    "max_prompt_chars": 128001,  # Fallback - will be overridden by active LLM engine limits
    "max_response_chars": 4000,
    "supports_images": False,
    "supports_functions": False,
    "model_name": "manual",
    "default_model": "manual",
    "log_throttle_sec": 5,
    "rate_limit_delay": 1,
}


def get_manual_config() -> dict:
    """Get Manual AI-specific configuration."""
    return MANUAL_CONFIG.copy()


def get_max_prompt_chars() -> int:
    """Get maximum prompt characters for Manual AI."""
    return MANUAL_CONFIG["max_prompt_chars"]


def get_max_response_chars() -> int:
    """Get maximum response characters for Manual AI."""
    return MANUAL_CONFIG["max_response_chars"]


def supports_images() -> bool:
    """Check if Manual AI supports images."""
    return MANUAL_CONFIG["supports_images"]


def supports_functions() -> bool:
    """Check if Manual AI supports functions."""
    return MANUAL_CONFIG["supports_functions"]


def get_interface_limits() -> dict:
    """Get the limits and capabilities for Manual LLM interface."""
    return {
        "max_prompt_chars": MANUAL_CONFIG["max_prompt_chars"],
        "max_response_chars": MANUAL_CONFIG["max_response_chars"],
        "supports_images": MANUAL_CONFIG["supports_images"],
        "supports_functions": MANUAL_CONFIG["supports_functions"],
        "model_name": MANUAL_CONFIG["model_name"],
    }


# Global variable for throttling manual logs
_last_manual_log_time = 0
_manual_log_throttle_sec = 5
_last_bot_none_manual_log_time = 0


class ManualAIPlugin(AIPluginBase):
    display_name = "Manual"

    def __init__(self, notify_fn=None):
        from core.notifier import set_notifier

        # Initialize the persistent mapping table
        # Initialize the persistent mapping table. Best-effort: don't raise if DB or aiomysql is missing.
        try:
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                loop.create_task(init_message_map_table())
            else:
                try:
                    asyncio.run(init_message_map_table())
                except Exception as e:
                    log_warning(
                        f"[manual] Could not init message map table synchronously: {e}"
                    )
        except Exception as e:
            log_warning(f"[manual] Could not init message map table: {e}")

        if notify_fn:
            log_debug("[manual] Using custom notification function.")
            set_notifier(notify_fn)
        else:
            log_debug("[manual] No notification function provided, using fallback.")
            set_notifier(
                lambda chat_id, message: log_info(f"[NOTIFY fallback] {message}")
            )

    def get_interface_limits(self) -> dict:
        """Get the limits and capabilities for Manual LLM interface."""
        return get_interface_limits()

    async def track_message(
        self, trainer_message_id, original_chat_id, original_message_id
    ):
        """Persist the mapping for a forwarded message."""
        await store_message_mapping(
            trainer_message_id, original_chat_id, original_message_id
        )

    def get_target(self, trainer_message_id):
        return get_original_message(trainer_message_id)

    def clear(self, trainer_message_id):
        asyncio.create_task(cleanup_old_mappings(trainer_message_id))

    def get_rate_limit(self):
        return (80, 10800, 0.5)

    async def handle_incoming_message(self, bot, message, prompt):
        from core.notifier import notify_trainer

        notify_trainer("🚨 Generating the reply...")

        global _last_manual_log_time, _manual_log_throttle_sec
        now = time.time()
        if now - _last_manual_log_time >= _manual_log_throttle_sec:
            log_debug(
                f"[manual] Message received in manual mode from chat_id={message.chat_id}"
            )
            _last_manual_log_time = now

        # /say support removed — no interactive trainer forwarding (previously forwarded trainer text to target chat)

        # Resolve trainer id from core.config at runtime to respect test monkeypatches
        try:
            from core import config as _config

            log_debug(
                f"[manual] core.config.get_trainer_id callable: {_config.get_trainer_id}"
            )
            trainer_id = _config.get_trainer_id("telegram_bot")
        except Exception:
            trainer_id = get_trainer_id("telegram_bot")
        log_debug(f"[manual] trainer id resolved: {trainer_id}")
        if not trainer_id:
            # Fallback: try reading from core.config.get_trainer_ids() in case the imported
            # symbol was not updated by tests or runtime configuration
            try:
                from core import config as _config

                trainer_id = _config.get_trainer_ids().get("telegram_bot")
                log_debug(
                    f"[manual] fallback trainer id resolved via config.get_trainer_ids(): {trainer_id}"
                )
            except Exception:
                pass
        if not trainer_id:
            # No trainer configured; log and continue in test environments where we still want
            # to exercise the message-forwarding/mapping behavior. _safe_send will be a
            # test stub that can handle a None chat_id.
            log_warning(
                "[manual] Missing trainer ID for telegram_bot; proceeding with fallback (test/limited env)"
            )

        prompt_json = json.dumps(prompt, ensure_ascii=False, indent=2)
        try:
            # Use telegram_bot.safe_send if tests monkeypatch it, otherwise use imported safe_send
            from interface import telegram_bot as _telegram_bot_module

            _safe_send = getattr(_telegram_bot_module, "safe_send", safe_send)

            # Send heading and JSON chunks, capturing the last sent message for mapping
            sent_heading = await _safe_send(
                bot,
                trainer_id,
                "\U0001f4e6 *Generated JSON prompt:*",
                parse_mode=ParseMode.MARKDOWN,
            )
            log_debug(
                f"[manual] sent heading to trainer (trainer_id={trainer_id}) -> {repr(sent_heading)}"
            )
            last_sent = None
            for i in range(0, len(prompt_json), 4000):
                chunk = prompt_json[i : i + 4000]
                sent_chunk = await _safe_send(
                    bot,
                    trainer_id,
                    f"```json\n{chunk}\n```",
                    parse_mode=ParseMode.MARKDOWN,
                )
                log_debug(f"[manual] sent chunk to trainer -> {repr(sent_chunk)}")
                if sent_chunk is not None:
                    last_sent = sent_chunk

                # === Inoltra il messaggio originale per facilitare la risposta ===
                sender = message.from_user
                uname = getattr(sender, "username", None)
                fullname = (
                    getattr(sender, "full_name", None)
                    or getattr(sender, "first_name", None)
                    or str(getattr(sender, "id", ""))
                )
                user_ref = f"@{uname}" if uname else fullname
                ref_sent = await _safe_send(bot, trainer_id, f"{user_ref}:")
                log_debug(f"[manual] sent user_ref to trainer -> {repr(ref_sent)}")
                if ref_sent is not None:
                    last_sent = ref_sent

                # Attempt to forward original message (preferred), capture forward result
                if bot is not None:
                    try:
                        forwarded = await bot.forward_message(
                            chat_id=trainer_id,
                            from_chat_id=message.chat_id,
                            message_id=getattr(message, "message_id", None),
                        )
                        log_debug(
                            f"[manual] forward_message result -> {repr(forwarded)}"
                        )
                    except Exception as forward_exc:
                        log_warning(
                            f"[manual] forward_message failed, sending fallback link: {forward_exc}"
                        )
                        # Fallback: send textual reference to original message
                        fallback_sent = await _safe_send(
                            bot,
                            trainer_id,
                            f"(original message from chat {message.chat_id} id {getattr(message, 'message_id', 'unknown')})",
                        )
                        log_debug(f"[manual] fallback_sent -> {repr(fallback_sent)}")
                        forwarded = None
                        if fallback_sent is not None:
                            last_sent = fallback_sent
                else:
                    log_warning("[manual] Bot is None, skipping forward_message")
                    global _last_bot_none_manual_log_time
                    now = time.time()
                    if now - _last_bot_none_manual_log_time >= _manual_log_throttle_sec:
                        log_warning("[manual] Bot is None, skipping forward_message")
                        _last_bot_none_manual_log_time = now
                    fallback_sent = await _safe_send(
                        bot,
                        trainer_id,
                        f"(original message from chat {message.chat_id} id {getattr(message, 'message_id', 'unknown')})",
                    )
                    forwarded = None
                    if fallback_sent is not None:
                        last_sent = fallback_sent

                # If we have a forwarded message id prefer it; otherwise map the last sent message (chunk/ref/fallback)
                if forwarded and getattr(forwarded, "message_id", None) is not None:
                    trainer_mid = getattr(forwarded, "message_id", None)
                    log_debug(
                        f"[manual] using forwarded message id for mapping: {trainer_mid}"
                    )
                    await self.track_message(
                        trainer_mid,
                        message.chat_id,
                        getattr(message, "message_id", None),
                    )
                elif last_sent and getattr(last_sent, "message_id", None) is not None:
                    trainer_mid = getattr(last_sent, "message_id", None)
                    log_debug(
                        f"[manual] using last_sent message id for mapping: {trainer_mid}"
                    )
                    await self.track_message(
                        trainer_mid,
                        message.chat_id,
                        getattr(message, "message_id", None),
                    )
                else:
                    log_warning(
                        f"[manual] Forwarding failed or trainer message id missing; skipping mapping for original chat={message.chat_id}, msg={getattr(message, 'message_id', None)}"
                    )

        except Exception as e:  # pragma: no cover - best effort
            log_error(f"[manual] Failed to notify trainer: {repr(e)}")

    async def generate_response(self, messages):
        """In manual mode the reply is not generated automatically."""
        return "\U0001f570\ufe0f Waiting for manual input."


# Manual plugin is a trainer-facing conduit. It does not synthesize LLM output itself
# and therefore does not need to call `llm_to_interface` here. Replies created by the
# trainer (human) will be injected by the LLM plugin via the normal flow.

PLUGIN_CLASS = ManualAIPlugin
