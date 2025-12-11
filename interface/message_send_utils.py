from typing import Optional
import asyncio
from telegram.error import TimedOut
from core.logging_utils import (
    log_debug,
    log_info,
    log_warning,
    log_error,
    setup_logging,
)
import traceback
import time
from telegram.error import RetryAfter, NetworkError

# Track whether we've already warned about None bot to avoid log spam
_BOT_NONE_WARNED = False
_LAST_BOT_NONE_LOG_TIME = 0
_BOT_NONE_LOG_THROTTLE_SEC = 5  # Log at most once every 5 seconds

# Per-chat cooldowns to avoid retry storms when Telegram reports flood/network errors
_CHAT_COOLDOWNS: dict = {}
DEFAULT_COOLDOWN_SECONDS = 30

# Queue for messages waiting for cooldown to expire
_PENDING_MESSAGES: dict = {}  # {chat_id: [(bot, text, kwargs, timestamp), ...]}
_COOLDOWN_PROCESSOR_RUNNING = False

# Maximum preview length for logging failed messages
max_message_preview_len = 100


def truncate_message(text: Optional[str], limit: int = 4000) -> str:
    """Return ``text`` truncated to fit within Telegram limits."""
    if not text:
        return text or ""
    if len(text) > limit:
        return text[:limit] + "\n... (truncated)"
    return text


async def _process_pending_messages():
    """Process queued messages when cooldown expires."""
    global _COOLDOWN_PROCESSOR_RUNNING, _PENDING_MESSAGES, _CHAT_COOLDOWNS
    _COOLDOWN_PROCESSOR_RUNNING = True
    
    try:
        while True:
            await asyncio.sleep(5)  # Check every 5 seconds
            
            current_time = time.time()
            chats_to_process = []
            
            # Find chats whose cooldown has expired
            for chat_id in list(_PENDING_MESSAGES.keys()):
                cd_until = _CHAT_COOLDOWNS.get(chat_id)
                if not cd_until or current_time >= cd_until:
                    chats_to_process.append(chat_id)
            
            # Process pending messages for expired cooldowns
            for chat_id in chats_to_process:
                messages = _PENDING_MESSAGES.pop(chat_id, [])
                if not messages:
                    continue
                    
                log_info(f"[telegram_utils] Processing {len(messages)} queued message(s) for chat {chat_id}")
                
                for bot, text, kwargs, queued_at in messages:
                    try:
                        # Remove cooldown temporarily to allow send
                        _CHAT_COOLDOWNS.pop(chat_id, None)
                        
                        wait_time = int(current_time - queued_at)
                        log_debug(f"[telegram_utils] Sending queued message (waited {wait_time}s)")
                        
                        # Recursively call _send_with_retry (cooldown is cleared)
                        await _send_with_retry(bot, chat_id, text, **kwargs)
                        
                    except Exception as e:
                        log_error(f"[telegram_utils] Failed to send queued message to {chat_id}: {e}")
            
            # Stop processor if no more pending messages
            if not _PENDING_MESSAGES:
                log_debug("[telegram_utils] No more pending messages, stopping processor")
                break
                
    except Exception as e:
        log_error(f"[telegram_utils] Pending message processor error: {e}")
    finally:
        _COOLDOWN_PROCESSOR_RUNNING = False


async def _send_with_retry(
    bot,
    chat_id: int,
    text: str,
    retries: int = 5,
    delay: int = 3,
    **kwargs,
):
    """Send a single message with retry support."""
    global _BOT_NONE_WARNED, _PENDING_MESSAGES, _COOLDOWN_PROCESSOR_RUNNING
    # Cooldown check: queue message instead of skipping
    try:
        cd_until = _CHAT_COOLDOWNS.get(chat_id)
        if cd_until and time.time() < cd_until:
            wait_seconds = int(cd_until - time.time())
            log_warning(f"[telegram_utils] Chat {chat_id} is in cooldown for {wait_seconds}s; queueing message for later delivery")
            # Add to pending queue
            if chat_id not in _PENDING_MESSAGES:
                _PENDING_MESSAGES[chat_id] = []
            _PENDING_MESSAGES[chat_id].append((bot, text, kwargs, time.time()))
            # Start processor if not running
            if not _COOLDOWN_PROCESSOR_RUNNING:
                asyncio.create_task(_process_pending_messages())
            return None
    except Exception as ex:
        log_error(f"[telegram_utils] Error checking cooldown: {ex}")
        pass
    if bot is None:
        # Log a single diagnostic warning with stacktrace to find the caller, then suppress repeats
        if not _BOT_NONE_WARNED:
            log_warning("[telegram_utils] _send_with_retry called with None bot — capturing stack for diagnostics")
            stack = ''.join(traceback.format_stack(limit=10))
            log_debug(f"[telegram_utils] Caller stack (first occurrence) for _send_with_retry:\n{stack}")
            _BOT_NONE_WARNED = True
        else:
            current_time = time.time()
            if current_time - _LAST_BOT_NONE_LOG_TIME > _BOT_NONE_LOG_THROTTLE_SEC:
                log_debug("[telegram_utils] _send_with_retry called with None bot (suppressed)")
                _LAST_BOT_NONE_LOG_TIME = current_time
        return None
    # Accept either int or str chat identifiers (some interfaces use alphanumeric ids)
    if chat_id is None or not isinstance(chat_id, (int, str)):
        log_error("[telegram_utils] Cannot send message: chat_id is invalid")
        return None

    # Filter kwargs to only include valid Telegram bot parameters
    # Remove custom parameters that are not supported by bot.send_message()
    # Exclude internal transport-layer kwargs that Telegram's API does not accept
    excluded = {'event_id', 'interface', 'is_llm_response', 'context', 'error_retry_policy'}
    valid_kwargs = {k: v for k, v in kwargs.items() if k not in excluded and v is not None}

    # Map thread_id to message_thread_id for Telegram API compatibility
    if 'thread_id' in valid_kwargs:
        thread_id = valid_kwargs.pop('thread_id')
        # Convert thread_id to int if it's a string (for Telegram API compatibility)
        if isinstance(thread_id, str) and thread_id.isdigit():
            thread_id = int(thread_id)
        valid_kwargs['message_thread_id'] = thread_id

    # Diagnostic: log attempt and kwargs
    log_debug(f"[telegram_utils] _send_with_retry prepare send: chat_id={chat_id} type={type(chat_id)} len_text={len(text) if text else 0} valid_kwargs={valid_kwargs}")
    
    last_error = None
    logger = setup_logging()
    for attempt in range(1, retries + 1):
        try:
            result = await bot.send_message(chat_id=chat_id, text=text, **valid_kwargs)
            try:
                log_debug(f"[telegram_utils] _send_with_retry success: chat_id={chat_id} result_message_id={getattr(result, 'message_id', None)}")
            except Exception:
                log_debug("[telegram_utils] _send_with_retry success (unable to repr result)")
            return result
        except TimedOut as e:
            last_error = e
            base_delay = delay * (2 ** (attempt - 1))  # Exponential backoff
            actual_delay = min(base_delay, 10.0)  # Cap at 10 seconds
            
            log_warning(f"[telegram_utils] TimedOut on attempt {attempt}/{retries} for chat_id={chat_id}: {e}")
            if attempt < retries:
                log_debug(f"[telegram_utils] Waiting {actual_delay:.1f}s before retry {attempt + 1}")
                await asyncio.sleep(actual_delay)
            else:
                log_error(f"[telegram_utils] TimedOut persisted after {retries} retries for chat_id={chat_id}, final delay was {actual_delay:.1f}s")
        except Exception as e:
            error_message = str(e)
            # On network-like errors, set a cooldown for this chat to avoid tight retry loops
            try:
                if isinstance(e, RetryAfter):
                    seconds = getattr(e, 'retry_after', None) or getattr(e, 'retry_after_seconds', None)
                    if seconds is None:
                        # Try to parse number from message as fallback
                        import re
                        m = re.search(r"Retry in (\d+) second", error_message)
                        if m:
                            seconds = int(m.group(1))
                    cooldown = time.time() + (int(seconds) if seconds else DEFAULT_COOLDOWN_SECONDS)
                    _CHAT_COOLDOWNS[chat_id] = cooldown
                elif isinstance(e, NetworkError):
                    _CHAT_COOLDOWNS[chat_id] = time.time() + DEFAULT_COOLDOWN_SECONDS
            except Exception:
                pass
            log_warning(f"[telegram_utils] send_message exception on attempt {attempt}/{retries} for chat_id={chat_id}: {error_message}")
            # Retry without parse_mode if Markdown/HTML entities are malformed
            if "can't parse entities" in error_message.lower() and valid_kwargs.get("parse_mode"):
                log_warning(
                    f"[telegram_utils] Parse error with parse_mode={valid_kwargs['parse_mode']}; retrying without parse_mode"
                )
                valid_kwargs.pop("parse_mode", None)
                try:
                    result = await bot.send_message(chat_id=chat_id, text=text, **valid_kwargs)
                    log_debug(f"[telegram_utils] _send_with_retry success after removing parse_mode for chat_id={chat_id}")
                    return result
                except Exception as e2:
                    # If this retry fails due to network, apply cooldown
                    try:
                        if isinstance(e2, RetryAfter):
                            seconds = getattr(e2, 'retry_after', None) or getattr(e2, 'retry_after_seconds', None)
                            _CHAT_COOLDOWNS[chat_id] = time.time() + (int(seconds) if seconds else DEFAULT_COOLDOWN_SECONDS)
                        elif isinstance(e2, NetworkError):
                            _CHAT_COOLDOWNS[chat_id] = time.time() + DEFAULT_COOLDOWN_SECONDS
                    except Exception:
                        pass
                    log_error(f"[telegram_utils] Retry after parse_mode removal failed: {e2}")
                    # Don't raise thread errors immediately - let send_with_thread_fallback handle them
                    if "thread not found" not in str(e2).lower():
                        raise e2
                    else:
                        log_debug(f"[telegram_utils] Thread error after parse_mode retry, letting caller handle: {e2}")
                        raise e2
            else:
                # If it's a thread error, let send_with_thread_fallback handle it
                if "thread not found" in error_message.lower():
                    log_debug(f"[telegram_utils] Thread error in _send_with_retry, letting caller handle: {e}")
                    raise e
                # If it's a non-parse error and not recoverable, re-raise to be handled by caller
                raise
    trainer_id = TELEGRAM_TRAINER_ID
    if trainer_id:
        try:
            await bot.send_message(
                chat_id=trainer_id,
                text=f"\u274c Telegram send_message failed after {retries} retries"
            )
        except Exception:
            pass
    logger.critical(
        "[telegram_utils] Failed to send message after %d retries to chat_id=%s. Content preview: %r",
        retries,
        chat_id,
        text[:max_message_preview_len],
    )
    if last_error:
        raise last_error


async def safe_send(bot, chat_id: int, text: str, chunk_size: int = 4000, retries: int = 3, delay: int = 2, **kwargs):
    """Send ``text`` in chunks using the universal transport layer.

    This wrapper forwards to the transport layer's Telegram sender which handles
    JSON detection and chunking/retries. Any custom keyword arguments are
    forwarded as-is.
    """  # [FIX]
    global _BOT_NONE_WARNED
    if bot is None:
        if not _BOT_NONE_WARNED:
            log_warning("[telegram_utils] safe_send called with None bot — capturing stack for diagnostics")
            stack = ''.join(traceback.format_stack(limit=10))
            log_debug(f"[telegram_utils] Caller stack (first occurrence) for safe_send:\n{stack}")
            _BOT_NONE_WARNED = True
        else:
            current_time = time.time()
            if current_time - _LAST_BOT_NONE_LOG_TIME > _BOT_NONE_LOG_THROTTLE_SEC:
                log_debug("[telegram_utils] safe_send called with None bot (suppressed)")
                _LAST_BOT_NONE_LOG_TIME = current_time
        return None
    # Accept either int or str chat identifiers (some interfaces use alphanumeric ids)
    if chat_id is None or not isinstance(chat_id, (int, str)):
        log_error("[telegram_utils] Cannot send message: chat_id is invalid")
        return None

    # Diagnostic: log safe_send entry and kwargs
    log_debug(f"[telegram_utils] safe_send called: chat_id={chat_id} type={type(chat_id)} kwargs_keys={list(kwargs.keys())} chunk_size={chunk_size} retries={retries} delay={delay}")

    result = await llm_response_send(bot, chat_id, text, chunk_size, retries, delay, **kwargs)

    # Diagnostic: log return value
    log_debug(f"[telegram_utils] safe_send result for chat_id={chat_id}: {repr(result)}")
    return result


async def safe_edit(bot, chat_id: int, message_id: int, text: str, retries: int = 3, delay: int = 2, **kwargs):
    """Edit a Telegram message with retry support."""  # [FIX][telegram retry]
    
    # Filter kwargs to only include valid Telegram bot parameters
    # Remove custom parameters that are not supported by bot.edit_message_text()
    valid_kwargs = {k: v for k, v in kwargs.items() if k not in ['event_id']}
    
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            log_debug(f"[telegram_utils] edit_message_text attempt {attempt}/{retries} chat_id={chat_id} message_id={message_id} kwargs={valid_kwargs}")
            return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **valid_kwargs)
        except TimedOut as e:
            last_error = e
            if attempt < retries:
                print(f"[telegram retry] edit_message_text timed out ({attempt}/{retries}), retrying...")
                await asyncio.sleep(delay)
            else:
                print(f"[telegram retry] edit_message_text failed after {retries} retries: {e}")
        except Exception:
            raise
    trainer_id = TELEGRAM_TRAINER_ID
    if trainer_id:
        try:
            await bot.send_message(
                chat_id=trainer_id,
                text=f"\u274c Telegram edit_message_text failed after {retries} retries (TimedOut)"
            )
        except Exception:
            pass
    if last_error:
        raise last_error



async def send_with_thread_fallback(
    bot,
    chat_id: int | str,
    text: str,
    *,
    thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    fallback_chat_id: int | None = None,
    fallback_thread_id: int | None = None,
    fallback_reply_to_message_id: int | None = None,
    **kwargs,
) -> object | None:
    """Send a Telegram message with automatic thread fallback.

    ``thread_id`` is the correct Telegram Bot API parameter.  This
    helper mirrors the old ``_send_telegram_message`` logic so that any
    interface can reuse the same behaviour.  It first tries to send with the
    provided ``thread_id`` and falls back to sending without it if the
    thread does not exist.  If ``fallback_chat_id`` is provided it will then try
    sending to that chat using the accompanying fallback parameters.
    """

    global _BOT_NONE_WARNED
    if bot is None:
        # Log a single warning to avoid flooding logs; subsequent calls are debug.
        if not _BOT_NONE_WARNED:
            log_warning("[telegram_utils] send_with_thread_fallback called with None bot — capturing stack for diagnostics")
            stack = ''.join(traceback.format_stack(limit=10))
            log_debug(f"[telegram_utils] Caller stack (first occurrence) for send_with_thread_fallback:\n{stack}")
            _BOT_NONE_WARNED = True
        else:
            current_time = time.time()
            if current_time - _LAST_BOT_NONE_LOG_TIME > _BOT_NONE_LOG_THROTTLE_SEC:
                log_debug("[telegram_utils] send_with_thread_fallback called with None bot (suppressed)")
                _LAST_BOT_NONE_LOG_TIME = current_time
        return

    # Respect per-chat cooldowns to avoid retry storms - queue message instead of skipping
    try:
        cd = _CHAT_COOLDOWNS.get(chat_id)
        if cd and time.time() < cd:
            wait_seconds = int(cd - time.time())
            log_warning(f"[telegram_utils] send_with_thread_fallback: chat {chat_id} in cooldown for {wait_seconds}s; queueing message")
            # Add to pending queue
            global _PENDING_MESSAGES, _COOLDOWN_PROCESSOR_RUNNING
            if chat_id not in _PENDING_MESSAGES:
                _PENDING_MESSAGES[chat_id] = []
            send_kwargs = {**kwargs}
            if thread_id is not None:
                if isinstance(thread_id, str) and thread_id.isdigit():
                    thread_id = int(thread_id)
                send_kwargs["message_thread_id"] = thread_id
            if reply_to_message_id is not None:
                send_kwargs["reply_to_message_id"] = reply_to_message_id
            _PENDING_MESSAGES[chat_id].append((bot, text, send_kwargs, time.time()))
            # Start processor if not running
            if not _COOLDOWN_PROCESSOR_RUNNING:
                asyncio.create_task(_process_pending_messages())
            return None
    except Exception:
        pass

    # Do not coerce/convert chat_id: allow string identifiers for non-Telegram
    # interfaces (e.g., Revolt/Mastodon). Validation is handled downstream.
    # chat_id remains as provided (int or str).

    send_kwargs = {**kwargs}
    if thread_id is not None:
        # Convert thread_id to int if it's a string (for Telegram API compatibility)
        if isinstance(thread_id, str) and thread_id.isdigit():
            thread_id = int(thread_id)
        send_kwargs["message_thread_id"] = thread_id
    if reply_to_message_id is not None:
        send_kwargs["reply_to_message_id"] = reply_to_message_id

    try:
        log_debug(f"[telegram_utils] send_with_thread_fallback calling llm_response_send chat_id={chat_id} send_kwargs={send_kwargs}")
        message = await llm_response_send(
            bot,
            chat_id,
            text,
            **send_kwargs,
        )
        if message is not None:
            log_info(
                f"[telegram_utils] Message sent to {chat_id}"
                f" (thread: {thread_id}, reply_message_id: {reply_to_message_id})"
            )
        else:
            log_warning(
                f"[telegram_utils] Message to {chat_id} queued/skipped due to cooldown or error"
                f" (thread: {thread_id}, reply_message_id: {reply_to_message_id})"
            )
        log_debug(f"[telegram_utils] llm_response_send returned: {repr(message)}")
        return message
    except Exception as e:
        # On network/flood errors, set a cooldown for this chat
        try:
            if isinstance(e, RetryAfter):
                seconds = getattr(e, 'retry_after', None) or getattr(e, 'retry_after_seconds', None)
                _CHAT_COOLDOWNS[chat_id] = time.time() + (int(seconds) if seconds else DEFAULT_COOLDOWN_SECONDS)
            elif isinstance(e, NetworkError):
                _CHAT_COOLDOWNS[chat_id] = time.time() + DEFAULT_COOLDOWN_SECONDS
        except Exception:
            pass
        
        error_message = str(e)
        if "chat not found" in error_message.lower():
            log_error(f"[telegram_utils] send_with_thread_fallback caught error: {repr(e)}")
            log_error(
                f"[telegram_utils] Failed to send to {chat_id} (thread {thread_id}): {repr(e)}"
            )
            raise

        if thread_id and "thread not found" in error_message.lower():
            # Don't log as ERROR since this is expected behavior - thread fallback is normal
            log_debug(f"[telegram_utils] send_with_thread_fallback caught thread error: {repr(e)}")
            log_warning(
                f"[telegram_utils] Thread {thread_id} not found; retrying without thread"
            )
            send_kwargs.pop("message_thread_id", None)
            message = await llm_response_send(bot, chat_id, text, **send_kwargs)
            if message is not None:
                log_info(
                    f"[telegram_utils] Message sent to {chat_id} without thread"
                )
            else:
                log_warning(
                    f"[telegram_utils] Message to {chat_id} queued/failed (cooldown or other error)"
                )
            return message

        # Log as error for all other cases  
        log_error(f"[telegram_utils] send_with_thread_fallback caught error: {repr(e)}")
        log_error(
            f"[telegram_utils] Failed to send to {chat_id} (thread {thread_id}): {repr(e)}"
        )
        raise

    if fallback_chat_id and fallback_chat_id != chat_id:
        fallback_kwargs = {**kwargs}
        if fallback_thread_id is not None:
            # Convert fallback_thread_id to int if it's a string (for Telegram API compatibility)
            if isinstance(fallback_thread_id, str) and fallback_thread_id.isdigit():
                fallback_thread_id = int(fallback_thread_id)
            fallback_kwargs["message_thread_id"] = fallback_thread_id
        if fallback_reply_to_message_id is not None:
            fallback_kwargs["reply_to_message_id"] = fallback_reply_to_message_id
        log_debug(
            f"[telegram_utils] Retrying in fallback chat {fallback_chat_id}"
        )
        try:
            message = await llm_response_send(bot, fallback_chat_id, text, **fallback_kwargs)
            if message is not None:
                log_info(
                    f"[telegram_utils] Message sent to fallback chat {fallback_chat_id}"
                )
            else:
                log_warning(
                    f"[telegram_utils] Message to fallback chat {fallback_chat_id} queued/failed"
                )
            return message
        except Exception as fallback_error:
            log_error(
                f"[telegram_utils] Final fallback failed: {fallback_error}"
            )
    return None


async def llm_response_send(bot, chat_id: int, text: str, chunk_size: int = 4000, retries: int = 3, delay: int = 2, **kwargs):
    """Universal LLM response sender with chunking, retry support, and action processing.
    
    This function is used by all interfaces (Telegram, WebUI, Matrix, etc.) to send
    LLM-generated responses safely. It provides:
    1. Automatic chunking for long messages (4000 chars default)
    2. Built-in retry logic with delays
    3. Telegram-specific error handling
    4. Direct bot instance access
    """
    if text is None:
        text = ""

    # Import json utilities
    try:
        from core.json_utils import extract_json_from_text
    except ImportError:
        def extract_json_from_text(text):
            return None

    # Log call information
    try:
        # Hide sensitive token information in logs
        bot_repr = str(bot)
        if 'token=' in bot_repr:
            # Extract token and show only last 4 characters
            import re
            token_match = re.search(r'token=([^]]+)', bot_repr)
            if token_match:
                token = token_match.group(1)
                if len(token) > 4:
                    masked_token = '*' * (len(token) - 4) + token[-4:]
                    bot_repr = bot_repr.replace(token, masked_token)
        log_debug(f"[llm_response_send] Called with bot={bot_repr}, chat_id={chat_id}, kwargs={kwargs}")
    except Exception:
        log_debug(f"[llm_response_send] Called with chat_id={chat_id}, kwargs_keys={list(kwargs.keys())}")

    # Log text content for debugging
    if text:
        # For JSON content, always log fully without truncation for debugging
        if text.strip().startswith(('{', '[')):
            log_debug(f"[llm_response_send] JSON content ({len(text)} chars, full dump below):\n{text}")
        else:
            log_debug(f"[llm_response_send] Text ({len(text)} chars): {text}")

    if 'reply_to_message_id' in kwargs and not kwargs['reply_to_message_id']:
        log_warning("[llm_response_send] reply_to_message_id not found. Sending without replying.")
        kwargs.pop('reply_to_message_id')

    # Validate chat_id
    if chat_id is None or not isinstance(chat_id, (int, str)):
        log_error(f"[llm_response_send] Invalid chat_id provided: {chat_id}")
        return None

    # Convert string chat_id to int if possible
    if isinstance(chat_id, str) and chat_id.strip().lstrip('-').isdigit():
        try:
            chat_id = int(chat_id)
        except Exception:
            pass

    # Don't try to parse JSON from system/error messages
    is_system_message = text.startswith(('[ERROR]', '[WARNING]', '[INFO]', '[DEBUG]')) or "system_message" in text

    json_data = None
    if not is_system_message:
        json_data = extract_json_from_text(text)
        if json_data:
            log_debug(f"[llm_response_send] JSON parsed successfully: {json_data}")
        elif ('{' in text and '}' in text) or ('[' in text and ']' in text):
            log_debug(f"[llm_response_send] Text contains JSON-like content but failed to parse: {text[:200]}...")
            # Try to process with action parser if available
            try:
                from types import SimpleNamespace
                from datetime import datetime
                
                message = SimpleNamespace()
                message.chat_id = chat_id
                message.text = ""
                message.original_text = text
                message.thread_id = kwargs.get('thread_id')
                message.date = datetime.utcnow()

                current_interface = 'telegram'
                corrector_context = {
                    'interface': current_interface,
                    'original_chat_id': chat_id,
                    'original_thread_id': kwargs.get('thread_id'),
                    'original_text': text[:500] if text else ''
                }

                from core import action_parser
                orchestrator_result = await action_parser.corrector_orchestrator(text, corrector_context, bot, message)

                if orchestrator_result is True:
                    log_debug("[llm_response_send] corrector_orchestrator executed actions; not forwarding text")
                    return
                elif orchestrator_result is False:
                    log_warning("[llm_response_send] corrector_orchestrator blocked message")
                    return None
                else:
                    log_debug("[llm_response_send] corrector_orchestrator returned None -> forwarding as normal text")

            except Exception as e:
                log_debug(f"[llm_response_send] corrector_orchestrator failed: {e}")
                return None
        else:
            log_debug("[llm_response_send] No JSON-like content detected, sending as normal text")

    if json_data:
        try:
            from types import SimpleNamespace
            
            # Handle different JSON formats
            if isinstance(json_data, dict) and "actions" in json_data:
                actions = json_data["actions"]
                if not isinstance(actions, list):
                    log_warning("[llm_response_send] actions field must be a list")
                    actions = []
            elif isinstance(json_data, list):
                actions = json_data
            elif isinstance(json_data, dict) and "type" in json_data:
                actions = [json_data]
            else:
                log_warning(f"[llm_response_send] Unrecognized JSON structure: {json_data}")
                actions = []
            
            if actions:
                # Create message context for actions
                message = SimpleNamespace()
                message.chat_id = chat_id
                message.text = ""
                message.original_text = text
                message.thread_id = kwargs.get('thread_id')
                if 'event_id' in kwargs:
                    message.event_id = kwargs['event_id']

                # Use action parser if available
                try:
                    from core.action_parser import run_actions
                    
                    context = {
                        "interface": "telegram",
                        "original_chat_id": chat_id,
                        "original_thread_id": kwargs.get('thread_id'),
                        "original_text": text[:500] if text else "",
                        "thread_defaults": {
                            "telegram": None,
                            "discord": None,
                            "default": None
                        }
                    }
                    if 'event_id' in kwargs:
                        context['event_id'] = kwargs['event_id']
                    
                    # Remove duplicate actions
                    unique_actions = []
                    seen_actions = set()
                    for action in actions:
                        action_id = str(action)
                        if action_id not in seen_actions:
                            unique_actions.append(action)
                            seen_actions.add(action_id)

                    await run_actions(unique_actions, context, bot, message)
                    log_info(f"[telegram_safe_send] Processed {len(unique_actions)} unique JSON actions")
                    return
                    
                except Exception as e:
                    log_warning(f"[telegram_safe_send] Failed to process JSON actions: {e}")

        except Exception as e:
            log_warning(f"[telegram_safe_send] Failed to process JSON actions: {e}")

    # Send as normal text with chunking
    log_debug(f"[telegram_safe_send] Sending as normal text with chunking")
    try:
        for i in range(0, len(text), chunk_size):
            chunk = text[i : i + chunk_size]
            log_debug(f"[llm_response_send] Sending chunk {i//chunk_size + 1} (len={len(chunk)}) to chat_id={chat_id}")
            await _send_with_retry(bot, chat_id, chunk, retries, delay, **kwargs)
    except Exception as e:
        # Log as WARNING if it's a thread error (will be handled by fallback), ERROR otherwise
        error_msg = str(e).lower()
        if "thread not found" in error_msg or "message thread not found" in error_msg:
            log_warning(f"[llm_response_send] Thread error (will retry without thread): {repr(e)}")
        else:
            log_error(f"[llm_response_send] Failed to send text chunks: {repr(e)}")
        raise


# Backward compatibility alias (deprecated, use llm_response_send instead)
telegram_safe_send = llm_response_send
