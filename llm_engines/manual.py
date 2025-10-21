# llm_engines/manual.py

from plugins.message_map import MessageMapPlugin, init_message_map_table, store_message_mapping, get_original_message, cleanup_old_mappings
from core import say_proxy
import asyncio
from core.config import get_trainer_id
from core.ai_plugin_base import AIPluginBase
import json
from telegram.constants import ParseMode
from core.logging_utils import log_debug, log_info, log_warning, log_error
from interface.telegram_utils import safe_send
import time

# Manual AI-specific configuration
MANUAL_CONFIG = {
    "max_prompt_chars": 8000,   # Manual input, keep it short
    "max_response_chars": 2000,
    "supports_images": False,
    "supports_functions": False,
    "model_name": "manual",
    "default_model": "manual",
    "log_throttle_sec": 5,
    "rate_limit_delay": 1
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
    log_info(f"[manual] Interface limits: max_prompt_chars={MANUAL_CONFIG['max_prompt_chars']}, supports_images={MANUAL_CONFIG['supports_images']}")
    return {
        "max_prompt_chars": MANUAL_CONFIG["max_prompt_chars"],
        "max_response_chars": MANUAL_CONFIG["max_response_chars"],
        "supports_images": MANUAL_CONFIG["supports_images"],
        "supports_functions": MANUAL_CONFIG["supports_functions"],
        "model_name": MANUAL_CONFIG["model_name"]
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
        try:
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                loop.create_task(init_message_map_table())
            else:
                asyncio.run(init_message_map_table())
        except RuntimeError:
            asyncio.run(init_message_map_table())

        if notify_fn:
            log_debug("[manual] Using custom notification function.")
            set_notifier(notify_fn)
        else:
            log_debug("[manual] No notification function provided, using fallback.")
            set_notifier(lambda chat_id, message: log_info(f"[NOTIFY fallback] {message}"))

    def get_interface_limits(self):
        """Get the limits and capabilities for Manual LLM interface."""
        log_info(f"[manual] Interface limits: max_prompt_chars={MANUAL_CONFIG['max_prompt_chars']}, supports_images={MANUAL_CONFIG['supports_images']}")
        return {
            "max_prompt_chars": MANUAL_CONFIG["max_prompt_chars"],
            "max_response_chars": MANUAL_CONFIG["max_response_chars"],
            "supports_images": MANUAL_CONFIG["supports_images"],
            "supports_functions": MANUAL_CONFIG["supports_functions"],
            "model_name": MANUAL_CONFIG["model_name"]
        }

    async def track_message(self, trainer_message_id, original_chat_id, original_message_id):
        """Persist the mapping for a forwarded message."""
        await store_message_mapping(trainer_message_id, original_chat_id, original_message_id)

    def get_target(self, trainer_message_id):
        return get_original_message(trainer_message_id)

    def clear(self, trainer_message_id):
        asyncio.create_task(cleanup_old_mappings(trainer_message_id))

    def get_rate_limit(self):
        return (80, 10800, 0.5)

    async def handle_incoming_message(self, bot, message, prompt):
        from core.notifier import notify_trainer
        notify_trainer("🚨 Generating the reply...")

        user_id = message.from_user.id
        text = message.text or ""
        global _last_manual_log_time, _manual_log_throttle_sec
        now = time.time()
        if now - _last_manual_log_time >= _manual_log_throttle_sec:
            log_debug(f"[manual] Message received in manual mode from chat_id={message.chat_id}")
            _last_manual_log_time = now

        # === Caso speciale: /say attivo ===
        target_chat = say_proxy.get_target(user_id)
        if target_chat and target_chat != "EXPIRED":
            log_debug(f"[manual] Invio da /say: chat_id={target_chat}")
            for i in range(0, len(text), 4000):
                chunk = text[i:i+4000]
                await safe_send(bot, target_chat, chunk)
            say_proxy.clear(user_id)
            return

        # === Invia prompt JSON al trainer ===
        import json
        from telegram.constants import ParseMode

        trainer_id = get_trainer_id("telegram_bot")
        if not trainer_id:
            log_warning("[manual] Missing trainer ID for telegram_bot; skipping notification")
            return

        prompt_json = json.dumps(prompt, ensure_ascii=False, indent=2)
        try:
            await safe_send(bot, trainer_id, "\U0001f4e6 *Generated JSON prompt:*", parse_mode=ParseMode.MARKDOWN)
            for i in range(0, len(prompt_json), 4000):
                chunk = prompt_json[i:i+4000]
                await safe_send(bot, trainer_id, f"```json\n{chunk}\n```", parse_mode=ParseMode.MARKDOWN)

            # === Inoltra il messaggio originale per facilitare la risposta ===
            sender = message.from_user
            # Use getattr to tolerate SimpleNamespace-like objects without username/full_name
            uname = getattr(sender, "username", None)
            fullname = getattr(sender, "full_name", None) or getattr(sender, "first_name", None) or str(getattr(sender, "id", ""))
            user_ref = f"@{uname}" if uname else fullname
            await safe_send(bot, trainer_id, f"{user_ref}:")

            if bot is not None:
                try:
                    sent = await bot.forward_message(
                        chat_id=trainer_id,
                        from_chat_id=message.chat_id,
                        message_id=getattr(message, 'message_id', None),
                    )
                except Exception as forward_exc:
                    log_warning(f"[manual] forward_message failed, sending fallback link: {forward_exc}")
                    # Fallback: send textual reference to original message
                    await safe_send(bot, trainer_id, f"(original message from chat {message.chat_id} id {getattr(message, 'message_id', 'unknown')})")
                    sent = None
            else:
                log_warning("[manual] Bot is None, skipping forward_message")
                # Throttle logs to reduce spam
                global _last_bot_none_manual_log_time
                now = time.time()
                if now - _last_bot_none_manual_log_time >= _manual_log_throttle_sec:
                    log_warning("[manual] Bot is None, skipping forward_message")
                    _last_bot_none_manual_log_time = now
                await safe_send(bot, trainer_id, f"(original message from chat {message.chat_id} id {getattr(message, 'message_id', 'unknown')})")
                sent = None

            if sent and getattr(sent, 'message_id', None) is not None:
                await self.track_message(getattr(sent, 'message_id', None), message.chat_id, getattr(message, 'message_id', None))
            else:
                # If forwarding not available, do not create a mapping with a
                # null trainer_message_id. Log and continue; mapping cannot be
                # created without a trainer message to reference.
                log_warning(f"[manual] Forwarding failed or trainer message id missing; skipping mapping for original chat={message.chat_id}, msg={getattr(message, 'message_id', None)}")
            log_debug("[manual] Message forwarded and tracked")
        except Exception as e:  # pragma: no cover - best effort
            log_error(f"[manual] Failed to notify trainer: {repr(e)}")

    async def generate_response(self, messages):
        """In manual mode the reply is not generated automatically."""
        return "\U0001f570\ufe0f Waiting for manual input."


# Manual plugin is a trainer-facing conduit. It does not synthesize LLM output itself
# and therefore does not need to call `llm_to_interface` here. Replies created by the
# trainer (human) will be injected by the LLM plugin via the normal flow.

PLUGIN_CLASS = ManualAIPlugin
