from typing import Optional
import asyncio

try:
    from telegram.error import TimedOut
except Exception:

    class TimedOut(Exception):
        pass


from core.logging_utils import (
    log_debug,
    log_info,
    log_warning,
    log_error,
    setup_logging,
)
import traceback
import time

try:
    from telegram.error import RetryAfter, NetworkError
except Exception:

    class RetryAfter(Exception):
        pass

    class NetworkError(Exception):
        pass


# Track whether we've already warned about None bot to avoid log spam
_BOT_NONE_WARNED = False
_LAST_BOT_NONE_LOG_TIME = 0
_BOT_NONE_LOG_THROTTLE_SEC = 5  # Log at most once every 5 seconds

# Short-lived dedupe cache for outgoing LLM-originated messages
_OUTGOING_DEDUPE: dict = {}  # key -> last_sent_timestamp
_DEFAULT_DEDUPE_WINDOW = 120  # seconds (configurable via OUTGOING_DEDUPE_WINDOW)

# Per-chat cooldowns to avoid retry storms when Telegram reports flood/network errors
_CHAT_COOLDOWNS: dict = {}
DEFAULT_COOLDOWN_SECONDS = 30

# Queue for messages waiting for cooldown to expire
_PENDING_MESSAGES: dict = {}  # {chat_id: [(bot, text, kwargs, timestamp), ...]}
_COOLDOWN_PROCESSOR_RUNNING = False

# Maximum preview length for logging failed messages
max_message_preview_len = 100


def _is_thread_or_chat_not_found(error_message: str) -> bool:
    """True if ``error_message`` is a stale thread/chat id, not a real network problem.

    python-telegram-bot's ``BadRequest`` is a ``NetworkError`` subclass, so these
    errors would otherwise trigger the same chat-wide cooldown as genuine
    connectivity/flood issues, even though they are immediately retried by the
    caller (``send_with_thread_fallback``) without the offending thread id.
    """
    lowered = error_message.lower()
    return "thread not found" in lowered or "chat not found" in lowered


def _is_stale_reply_target(error_message: str) -> bool:
    """True when ``reply_to_message_id`` points at a message Telegram cannot quote.

    The original message may be missing/deleted (or the id may never have
    existed — e.g. the model put the *chat* id in ``reply_to``), making the
    reply reference permanently undeliverable. Like a stale thread id this is a
    data problem, not a connectivity issue: no chat-wide cooldown must be set,
    and ``send_with_thread_fallback`` retries without the reply reference.

    Telegram's canonical wording is "Message to be replied not found", but the
    same condition reaches us with other wordings, and an unmatched wording
    used to escalate: the text was handed to the corrector and the user's reply
    was lost entirely (live incident 2026-09-17 06:57Z). Any "repl..." +
    "not found" pair counts. Kept deliberately narrow — unrelated BadRequests
    such as "Message is too long" must keep raising.
    """
    lowered = (error_message or "").lower()
    if "message to be replied not found" in lowered:
        return True
    return "not found" in lowered and "repl" in lowered


def _is_stale_identifier_error(error_message: str) -> bool:
    """True for any stale Telegram identifier (thread/chat/reply target)."""
    return _is_thread_or_chat_not_found(error_message) or _is_stale_reply_target(
        error_message
    )


def _get_telegram_trainer_id() -> int | None:
    """Resolve the Telegram trainer id from config for failure alerts."""
    try:
        from core.config import get_trainer_id

        tid = get_trainer_id("telegram_bot")
        if isinstance(tid, (list, tuple)):
            tid = tid[0] if tid else None
        return int(tid) if tid is not None else None
    except Exception:
        return None


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

                log_info(
                    f"[telegram_utils] Processing {len(messages)} queued message(s) for chat {chat_id}"
                )

                for bot, text, kwargs, queued_at in messages:
                    try:
                        # Remove cooldown temporarily to allow send
                        _CHAT_COOLDOWNS.pop(chat_id, None)

                        wait_time = int(current_time - queued_at)
                        log_debug(
                            f"[telegram_utils] Sending queued message (waited {wait_time}s)"
                        )

                        # Recursively call _send_with_retry (cooldown is cleared)
                        await _send_with_retry(bot, chat_id, text, **kwargs)

                    except Exception as e:
                        log_error(
                            f"[telegram_utils] Failed to send queued message to {chat_id}: {e}"
                        )

            # Stop processor if no more pending messages
            if not _PENDING_MESSAGES:
                log_debug(
                    "[telegram_utils] No more pending messages, stopping processor"
                )
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
            log_warning(
                f"[telegram_utils] Chat {chat_id} is in cooldown for {wait_seconds}s; queueing message for later delivery"
            )
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
    global _BOT_NONE_WARNED, _LAST_BOT_NONE_LOG_TIME
    if bot is None:
        # Log a single diagnostic warning with stacktrace to find the caller, then suppress repeats
        if not _BOT_NONE_WARNED:
            log_warning(
                "[telegram_utils] _send_with_retry called with None bot — capturing stack for diagnostics"
            )
            stack = "".join(traceback.format_stack(limit=10))
            log_debug(
                f"[telegram_utils] Caller stack (first occurrence) for _send_with_retry:\n{stack}"
            )
            _BOT_NONE_WARNED = True
        else:
            current_time = time.time()
            if current_time - _LAST_BOT_NONE_LOG_TIME > _BOT_NONE_LOG_THROTTLE_SEC:
                log_debug(
                    "[telegram_utils] _send_with_retry called with None bot (suppressed)"
                )
                _LAST_BOT_NONE_LOG_TIME = current_time
        return None
    # Accept either int or str chat identifiers (some interfaces use alphanumeric ids)
    if chat_id is None or not isinstance(chat_id, (int, str)):
        log_error("[telegram_utils] Cannot send message: chat_id is invalid")
        return None

    # Filter kwargs to only include valid Telegram bot parameters
    # Remove custom parameters that are not supported by bot.send_message()
    # Exclude internal transport-layer kwargs that Telegram's API does not accept
    excluded = {
        "event_id",
        "interface",
        "is_llm_response",
        "context",
        "error_retry_policy",
        "skip_history",
    }
    valid_kwargs = {
        k: v for k, v in kwargs.items() if k not in excluded and v is not None
    }

    # Map thread_id to message_thread_id for Telegram API compatibility
    if "thread_id" in valid_kwargs:
        thread_id = valid_kwargs.pop("thread_id")
        # Convert thread_id to int if it's a string (for Telegram API compatibility)
        if isinstance(thread_id, str) and thread_id.isdigit():
            thread_id = int(thread_id)
        valid_kwargs["message_thread_id"] = thread_id

    # Diagnostic: log attempt and kwargs
    log_debug(
        f"[telegram_utils] _send_with_retry prepare send: chat_id={chat_id} type={type(chat_id)} len_text={len(text) if text else 0} valid_kwargs={valid_kwargs}"
    )

    last_error = None
    logger = setup_logging()
    for attempt in range(1, retries + 1):
        try:
            result = await bot.send_message(chat_id=chat_id, text=text, **valid_kwargs)
            try:
                log_debug(
                    f"[telegram_utils] _send_with_retry success: chat_id={chat_id} result_message_id={getattr(result, 'message_id', None)}"
                )
            except Exception:
                log_debug(
                    "[telegram_utils] _send_with_retry success (unable to repr result)"
                )
            return result
        except TimedOut as e:
            last_error = e
            base_delay = delay * (2 ** (attempt - 1))  # Exponential backoff
            actual_delay = min(base_delay, 10.0)  # Cap at 10 seconds

            log_warning(
                f"[telegram_utils] TimedOut on attempt {attempt}/{retries} for chat_id={chat_id}: {e}"
            )
            if attempt < retries:
                log_debug(
                    f"[telegram_utils] Waiting {actual_delay:.1f}s before retry {attempt + 1}"
                )
                await asyncio.sleep(actual_delay)
            else:
                log_error(
                    f"[telegram_utils] TimedOut persisted after {retries} retries for chat_id={chat_id}, final delay was {actual_delay:.1f}s"
                )
        except Exception as e:
            error_message = str(e)
            # On network-like errors, set a cooldown for this chat to avoid tight retry loops
            try:
                if isinstance(e, RetryAfter):
                    seconds = getattr(e, "retry_after", None) or getattr(
                        e, "retry_after_seconds", None
                    )
                    if seconds is None:
                        # Try to parse number from message as fallback
                        import re

                        m = re.search(r"Retry in (\d+) second", error_message)
                        if m:
                            seconds = int(m.group(1))
                    cooldown = time.time() + (
                        int(seconds) if seconds else DEFAULT_COOLDOWN_SECONDS
                    )
                    _CHAT_COOLDOWNS[chat_id] = cooldown
                elif isinstance(e, NetworkError) and not _is_stale_identifier_error(
                    error_message
                ):
                    # BadRequest is a NetworkError subclass in python-telegram-bot,
                    # but stale thread/chat/reply-target ids are data problems, not
                    # connectivity issues — don't cooldown the whole chat for them,
                    # since send_with_thread_fallback retries immediately without
                    # the offending identifier and would otherwise walk straight
                    # into this cooldown.
                    _CHAT_COOLDOWNS[chat_id] = time.time() + DEFAULT_COOLDOWN_SECONDS
            except Exception:
                pass
            log_warning(
                f"[telegram_utils] send_message exception on attempt {attempt}/{retries} for chat_id={chat_id}: {error_message}"
            )
            # Retry without parse_mode if Markdown/HTML entities are malformed
            if "can't parse entities" in error_message.lower() and valid_kwargs.get(
                "parse_mode"
            ):
                log_warning(
                    f"[telegram_utils] Parse error with parse_mode={valid_kwargs['parse_mode']}; retrying without parse_mode"
                )
                valid_kwargs.pop("parse_mode", None)
                try:
                    result = await bot.send_message(
                        chat_id=chat_id, text=text, **valid_kwargs
                    )
                    log_debug(
                        f"[telegram_utils] _send_with_retry success after removing parse_mode for chat_id={chat_id}"
                    )
                    return result
                except Exception as e2:
                    # If this retry fails due to network, apply cooldown
                    try:
                        if isinstance(e2, RetryAfter):
                            seconds = getattr(e2, "retry_after", None) or getattr(
                                e2, "retry_after_seconds", None
                            )
                            _CHAT_COOLDOWNS[chat_id] = time.time() + (
                                int(seconds) if seconds else DEFAULT_COOLDOWN_SECONDS
                            )
                        elif isinstance(
                            e2, NetworkError
                        ) and not _is_stale_identifier_error(str(e2)):
                            _CHAT_COOLDOWNS[chat_id] = (
                                time.time() + DEFAULT_COOLDOWN_SECONDS
                            )
                    except Exception:
                        pass
                    log_error(
                        f"[telegram_utils] Retry after parse_mode removal failed: {e2}"
                    )
                    # Don't raise thread errors immediately - let send_with_thread_fallback handle them
                    if "thread not found" not in str(e2).lower():
                        raise e2
                    else:
                        log_debug(
                            f"[telegram_utils] Thread error after parse_mode retry, letting caller handle: {e2}"
                        )
                        raise e2
            else:
                # If it's a thread error, let send_with_thread_fallback handle it
                if "thread not found" in error_message.lower():
                    log_debug(
                        f"[telegram_utils] Thread error in _send_with_retry, letting caller handle: {e}"
                    )
                    raise e
                # If it's a non-parse error and not recoverable, re-raise to be handled by caller
                raise
    trainer_id = _get_telegram_trainer_id()
    if trainer_id:
        try:
            await bot.send_message(
                chat_id=trainer_id,
                text=f"\u274c Telegram send_message failed after {retries} retries",
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


async def safe_send(
    bot,
    chat_id: int,
    text: str,
    chunk_size: int = 4000,
    retries: int = 3,
    delay: int = 2,
    **kwargs,
):
    """Send ``text`` in chunks using the universal transport layer.

    This wrapper forwards to the transport layer's Telegram sender which handles
    JSON detection and chunking/retries. Any custom keyword arguments are
    forwarded as-is.
    """  # [FIX]
    global _BOT_NONE_WARNED
    global _BOT_NONE_WARNED, _LAST_BOT_NONE_LOG_TIME
    if bot is None:
        if not _BOT_NONE_WARNED:
            log_warning(
                "[telegram_utils] safe_send called with None bot — capturing stack for diagnostics"
            )
            stack = "".join(traceback.format_stack(limit=10))
            log_debug(
                f"[telegram_utils] Caller stack (first occurrence) for safe_send:\n{stack}"
            )
            _BOT_NONE_WARNED = True
        else:
            current_time = time.time()
            if current_time - _LAST_BOT_NONE_LOG_TIME > _BOT_NONE_LOG_THROTTLE_SEC:
                log_debug(
                    "[telegram_utils] safe_send called with None bot (suppressed)"
                )
                _LAST_BOT_NONE_LOG_TIME = current_time
        return None
    # Accept either int or str chat identifiers (some interfaces use alphanumeric ids)
    if chat_id is None or not isinstance(chat_id, (int, str)):
        log_error("[telegram_utils] Cannot send message: chat_id is invalid")
        return None

    # Diagnostic: log safe_send entry and kwargs
    log_debug(
        f"[telegram_utils] safe_send called: chat_id={chat_id} type={type(chat_id)} kwargs_keys={list(kwargs.keys())} chunk_size={chunk_size} retries={retries} delay={delay}"
    )

    result = await cortex_response_send(
        bot, chat_id, text, chunk_size, retries, delay, **kwargs
    )

    # Diagnostic: log return value
    log_debug(
        f"[telegram_utils] safe_send result for chat_id={chat_id}: {repr(result)}"
    )
    return result


async def safe_edit(
    bot,
    chat_id: int,
    message_id: int,
    text: str,
    retries: int = 3,
    delay: int = 2,
    **kwargs,
):
    """Edit a Telegram message with retry support."""  # [FIX][telegram retry]

    # Filter kwargs to only include valid Telegram bot parameters
    # Remove custom parameters that are not supported by bot.edit_message_text()
    valid_kwargs = {k: v for k, v in kwargs.items() if k not in ["event_id"]}

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            log_debug(
                f"[telegram_utils] edit_message_text attempt {attempt}/{retries} chat_id={chat_id} message_id={message_id} kwargs={valid_kwargs}"
            )
            return await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, **valid_kwargs
            )
        except TimedOut as e:
            last_error = e
            if attempt < retries:
                print(
                    f"[telegram retry] edit_message_text timed out ({attempt}/{retries}), retrying..."
                )
                await asyncio.sleep(delay)
            else:
                print(
                    f"[telegram retry] edit_message_text failed after {retries} retries: {e}"
                )
        except Exception:
            raise
    trainer_id = _get_telegram_trainer_id()
    if trainer_id:
        try:
            await bot.send_message(
                chat_id=trainer_id,
                text=f"\u274c Telegram edit_message_text failed after {retries} retries (TimedOut)",
            )
        except Exception:
            pass
    if last_error:
        raise last_error


def _record_stale_reply_drop(interface_path: str | None) -> None:
    """Record a capability drop so Synth learns the reply target vanished.

    Follows the established capability-drops pattern (core/capability_drops.py):
    a structured record is remembered for this conversation and the prompt
    engine renders it as an informational block on the NEXT turn, so Synth can
    acknowledge the limitation in its own words. Never raises — notification
    must never break delivery.
    """
    try:
        if not interface_path:
            return
        from core.capability_drops import DROP_REPLY, make_drop, remember_drops

        remember_drops(
            interface_path,
            [
                make_drop(
                    DROP_REPLY,
                    "the replied-to message no longer exists on Telegram; "
                    "your reply was delivered without the quote reference",
                    "telegram",
                )
            ],
        )
        log_debug(
            f"[telegram_utils] stale-reply capability drop recorded for {interface_path}"
        )
    except Exception as exc:
        log_debug(f"[telegram_utils] stale-reply drop recording skipped: {exc}")


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
    interface_path: str | None = None,
    **kwargs,
) -> object | None:
    """Send a Telegram message with automatic thread fallback.

    ``thread_id`` is the correct Telegram Bot API parameter.  This
    helper mirrors the old ``_send_telegram_message`` logic so that any
    interface can reuse the same behaviour.  It first tries to send with the
    provided ``thread_id`` and falls back to sending without it if the
    thread does not exist.  If ``fallback_chat_id`` is provided it will then try
    sending to that chat using the accompanying fallback parameters.

    ``interface_path`` is optional routing metadata (never forwarded to the
    Telegram API): when the reply target turns out to be deleted, a structured
    capability drop is recorded under it so Synth learns about the degraded
    delivery on its next turn.
    """
    if interface_path is None:
        # These helpers are Telegram-bound; synthesise the canonical path so
        # every caller (e.g. scheduled events) still gets drop reporting.
        try:
            from core.interface_path_utils import build_interface_path

            interface_path = build_interface_path(
                "telegram_bot",
                str(chat_id),
                str(thread_id) if thread_id else None,
            )
        except Exception:
            interface_path = f"telegram_bot/{chat_id}"

    global _BOT_NONE_WARNED, _LAST_BOT_NONE_LOG_TIME
    if bot is None:
        # Log a single warning to avoid flooding logs; subsequent calls are debug.
        if not _BOT_NONE_WARNED:
            log_warning(
                "[telegram_utils] send_with_thread_fallback called with None bot — capturing stack for diagnostics"
            )
            stack = "".join(traceback.format_stack(limit=10))
            log_debug(
                f"[telegram_utils] Caller stack (first occurrence) for send_with_thread_fallback:\n{stack}"
            )
            _BOT_NONE_WARNED = True
        else:
            current_time = time.time()
            if current_time - _LAST_BOT_NONE_LOG_TIME > _BOT_NONE_LOG_THROTTLE_SEC:
                log_debug(
                    "[telegram_utils] send_with_thread_fallback called with None bot (suppressed)"
                )
                _LAST_BOT_NONE_LOG_TIME = current_time
        return

    # Respect per-chat cooldowns to avoid retry storms - queue message instead of skipping
    try:
        cd = _CHAT_COOLDOWNS.get(chat_id)
        if cd and time.time() < cd:
            wait_seconds = int(cd - time.time())
            log_warning(
                f"[telegram_utils] send_with_thread_fallback: chat {chat_id} in cooldown for {wait_seconds}s; queueing message"
            )
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

    # Bounded degradation ladder: each rung strips one stale identifier and
    # retries. A deleted/purged original message makes ``reply_to_message_id``
    # permanently undeliverable ("Message to be replied not found") — the text
    # must still go out, so the reply reference is dropped first (thread kept);
    # a stale thread id is dropped on the next rung. Unknown errors are logged
    # as ERROR and re-raised.
    last_error: Exception | None = None
    dropped_stale_reply = False
    for _attempt in range(4):
        try:
            log_debug(
                f"[telegram_utils] send_with_thread_fallback calling cortex_response_send chat_id={chat_id} send_kwargs={send_kwargs}"
            )
            message = await cortex_response_send(
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
                # ``None`` means "not sent": the message was either queued on a
                # chat cooldown or handled/blocked by the corrector. The old
                # wording blamed a cooldown unconditionally, which hid a lost
                # reply behind a queueing message (live 2026-09-21: no cooldown
                # line existed anywhere in the log for either loss).
                log_warning(
                    f"[telegram_utils] Message to {chat_id} not sent (queued on "
                    f"cooldown, handled by the corrector, or a failed send)"
                    f" (thread: {thread_id}, reply_message_id: {reply_to_message_id})"
                )
            log_debug(
                f"[telegram_utils] cortex_response_send returned: {repr(message)}"
            )
            if dropped_stale_reply:
                _record_stale_reply_drop(interface_path)
            return message
        except Exception as e:
            last_error = e
            # On network/flood errors, set a cooldown for this chat; stale
            # identifiers are data problems handled by the ladder below.
            try:
                if isinstance(e, RetryAfter):
                    seconds = getattr(e, "retry_after", None) or getattr(
                        e, "retry_after_seconds", None
                    )
                    _CHAT_COOLDOWNS[chat_id] = time.time() + (
                        int(seconds) if seconds else DEFAULT_COOLDOWN_SECONDS
                    )
                elif isinstance(e, NetworkError) and not _is_stale_identifier_error(
                    str(e)
                ):
                    _CHAT_COOLDOWNS[chat_id] = time.time() + DEFAULT_COOLDOWN_SECONDS
            except Exception:
                pass

            error_message = str(e)
            lowered = error_message.lower()
            if "chat not found" in lowered:
                log_error(
                    f"[telegram_utils] send_with_thread_fallback caught error: {repr(e)}"
                )
                log_error(
                    f"[telegram_utils] Failed to send to {chat_id} (thread {thread_id}): {repr(e)}"
                )
                raise

            dropped: str | None = None
            if (
                _is_stale_reply_target(error_message)
                and "reply_to_message_id" in send_kwargs
            ):
                send_kwargs.pop("reply_to_message_id")
                dropped = "reply_to_message_id"
                dropped_stale_reply = True
                log_warning(
                    "[telegram_utils] Reply target no longer exists; retrying without reply_to_message_id"
                )
            elif (
                thread_id
                and "thread not found" in lowered
                and "message_thread_id" in send_kwargs
            ):
                send_kwargs.pop("message_thread_id")
                dropped = "message_thread_id"
                log_warning(
                    f"[telegram_utils] Thread {thread_id} not found; retrying without thread"
                )

            if dropped is None:
                log_error(
                    f"[telegram_utils] send_with_thread_fallback caught error: {repr(e)}"
                )
                log_error(
                    f"[telegram_utils] Failed to send to {chat_id} (thread {thread_id}): {repr(e)}"
                )
                raise
    if last_error is not None:
        raise last_error
    return None

    if fallback_chat_id and fallback_chat_id != chat_id:
        fallback_kwargs = {**kwargs}
        if fallback_thread_id is not None:
            # Convert fallback_thread_id to int if it's a string (for Telegram API compatibility)
            if isinstance(fallback_thread_id, str) and fallback_thread_id.isdigit():
                fallback_thread_id = int(fallback_thread_id)
            fallback_kwargs["message_thread_id"] = fallback_thread_id
        if fallback_reply_to_message_id is not None:
            fallback_kwargs["reply_to_message_id"] = fallback_reply_to_message_id
        log_debug(f"[telegram_utils] Retrying in fallback chat {fallback_chat_id}")
        try:
            message = await cortex_response_send(
                bot, fallback_chat_id, text, **fallback_kwargs
            )
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
            log_error(f"[telegram_utils] Final fallback failed: {fallback_error}")
    return None


# Legacy display labels must never reach the corrector's prompt builders: they
# map the interface through ``message_chain._INTERFACE_TO_MESSAGE_ACTION``,
# whose keys are REGISTERED ids (``telegram_bot``). A display label misses that
# lookup and falls through to ``message_<label>``, so the required-format
# example taught the model the unregistered ``message_telegram`` plus a
# malformed ``telegram/<id>`` path; the model copied both verbatim, the
# corrected action was rejected and the reply was never delivered (live
# 2026-09-21, langfuse feca9072-0abe-46ef-90da-f1409723088e).
_INTERFACE_DISPLAY_ALIASES: dict[str, str] = {"telegram": "telegram_bot"}

# Field names from the action schema. A REAL action envelope keeps its keys even
# when its punctuation is mangled, so their presence is what separates broken
# JSON from ordinary prose that merely opens with ``{``.
_ACTION_ENVELOPE_KEYS: tuple[str, ...] = (
    '"actions"',
    "'actions'",
    "actions:",
    '"type"',
    "'type'",
    "type:",
    '"payload"',
    "'payload'",
    "payload:",
    '"action"',
    "'action'",
    "action:",
)


def _looks_like_action_envelope(text: str) -> bool:
    """Whether unparseable text is a broken ACTION payload rather than a message.

    A reply that opens with ``{`` is not automatically malformed JSON: this
    persona writes physical actions in braces, so ``{the crack lands hard and I
    buck forward...}`` and ``{I bounce, quick and filthy}`` are ordinary message
    text. Treating that as broken JSON diverted the whole reply into the
    corrector, which answered with an action instead of the text, so the reply
    never reached the chat while every log line looked healthy (live
    2026-09-21: langfuse feca9072-0abe-46ef-90da-f1409723088e and
    b822895b-b87d-43f8-b535-2bbe9c3d31c7; last delivery to that chat 11:24:34).

    Structural test only: the head must carry a JSON key or one of the action
    schema's own field names. No natural-language matching.
    """
    head = text.lstrip()[:400].lower()
    if not head.startswith(("{", "[")):
        return False
    return any(key in head for key in _ACTION_ENVELOPE_KEYS)


async def _record_delivery_failure(
    reason: str,
    *,
    chat_id: int | str | None,
    interface_path: str | None,
    kwargs: dict,
    text: str | None,
) -> None:
    """Best-effort delivery failure record. Never raises, never blocks a send.

    A reply that vanishes with no row anywhere is undiagnosable, which is exactly
    how the two 2026-09-21 losses had to be reconstructed from traces plus logs.
    """
    try:
        from core.llm_failure_log import build_failure_entry, record_failure_entry

        entry = build_failure_entry(
            reason=reason,
            stage="delivery",
            failure_code="delivery_failed",
            interface_path=interface_path,
            chat_id=chat_id,
            thread_id=kwargs.get("thread_id") if isinstance(kwargs, dict) else None,
            content_preview=(text or "")[:280],
            metadata={"sender": "cortex_response_send"},
        )
        await record_failure_entry(entry)
    except Exception as exc:
        log_debug(f"[telegram_utils] delivery failure record skipped: {exc}")


def _resolve_sender_interface_id(kwargs: dict) -> str:
    """Registered interface id for a corrector context built by this sender.

    Structural: the ``interface_path`` prefix when the caller supplied one
    (normalising a legacy display label to its registered id), otherwise this
    module's own interface — this module *is* the Telegram sender, so its id is
    a known constant rather than something to guess.
    """
    for key in ("interface_path", "path"):
        raw = kwargs.get(key)
        if isinstance(raw, str) and raw.strip():
            prefix = raw.strip().split("/")[0].strip()
            if prefix:
                return _INTERFACE_DISPLAY_ALIASES.get(prefix, prefix)
    return "telegram_bot"


async def cortex_response_send(
    bot,
    chat_id: int,
    text: str,
    chunk_size: int = 4000,
    retries: int = 3,
    delay: int = 2,
    **kwargs,
):
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
        if "token=" in bot_repr:
            # Extract token and show only last 4 characters
            import re

            token_match = re.search(r"token=([^]]+)", bot_repr)
            if token_match:
                token = token_match.group(1)
                if len(token) > 4:
                    masked_token = "*" * (len(token) - 4) + token[-4:]
                    bot_repr = bot_repr.replace(token, masked_token)
        log_debug(
            f"[cortex_response_send] Called with bot={bot_repr}, chat_id={chat_id}, kwargs={kwargs}"
        )
    except Exception:
        log_debug(
            f"[cortex_response_send] Called with chat_id={chat_id}, kwargs_keys={list(kwargs.keys())}"
        )

    # Log text content for debugging, and detect potential encoding issues (mojibake)
    if text:
        try:
            from core.text_utils import looks_like_mojibake, try_recover_mojibake

            log_debug(f"[cortex_response_send] Text repr: {text!r}")
            if looks_like_mojibake(text):
                log_warning(
                    "[cortex_response_send] Potential mojibake detected in LLM output (will forward as-is)."
                )
                recovered = try_recover_mojibake(text)
                log_debug(
                    f"[cortex_response_send] Mojibake recovery attempt: recovered={recovered!r}"
                )
        except Exception:
            log_debug("[cortex_response_send] mojibake detection unavailable")

        # For JSON content, always log fully without truncation for debugging
        if text.strip().startswith(("{", "[")):
            log_debug(
                f"[cortex_response_send] JSON content ({len(text)} chars, full dump below):\n{text}"
            )
        else:
            log_debug(f"[cortex_response_send] Text ({len(text)} chars): {text}")

    if "reply_to_message_id" in kwargs and not kwargs["reply_to_message_id"]:
        log_warning(
            "[cortex_response_send] reply_to_message_id not found. Sending without replying."
        )
        kwargs.pop("reply_to_message_id")

    # Validate chat_id
    if chat_id is None or not isinstance(chat_id, (int, str)):
        log_error(f"[cortex_response_send] Invalid chat_id provided: {chat_id}")
        return None

    # Convert string chat_id to int if possible
    if isinstance(chat_id, str) and chat_id.strip().lstrip("-").isdigit():
        try:
            chat_id = int(chat_id)
        except Exception:
            pass

    # Don't try to parse JSON from system/error messages
    is_system_message = (
        text.startswith(("[ERROR]", "[WARNING]", "[INFO]", "[DEBUG]"))
        or "system_message" in text
    )

    json_data = None
    if not is_system_message:
        json_data = extract_json_from_text(text)
        if json_data:
            log_debug(f"[cortex_response_send] JSON parsed successfully: {json_data}")
        elif _looks_like_action_envelope(text):
            # Text genuinely starts with a JSON-like structure AND carries
            # action-schema keys, so it is a broken action payload — route
            # through the corrector to attempt recovery.
            # NOTE: we intentionally do NOT match on braces *anywhere* in the
            # text (e.g. emotion tags like ``{happy 10}`` or markdown); the
            # envelope check keeps it to text that *starts* with ``{``/``[`` AND
            # looks like an action, because this persona writes physical actions
            # in braces and a leading ``{`` alone is ordinary prose.
            log_debug(
                f"[cortex_response_send] Text starts with JSON-like content but failed to parse: {text[:200]}..."
            )
            try:
                from types import SimpleNamespace
                from datetime import datetime

                message = SimpleNamespace()
                message.chat_id = chat_id
                message.text = ""
                message.original_text = text
                message.thread_id = kwargs.get("thread_id")
                message.date = datetime.utcnow()
                message.from_cortex = True

                current_interface = _resolve_sender_interface_id(kwargs)
                corrector_context = {
                    "interface": current_interface,
                    # The structural path, so the corrector resolves the
                    # interface from its prefix instead of trusting the label
                    # above — which is what broke the routing example.
                    "interface_path": kwargs.get("interface_path")
                    or f"{current_interface}/{chat_id}",
                    "original_chat_id": chat_id,
                    "original_thread_id": kwargs.get("thread_id"),
                    "original_text": text[:500] if text else "",
                    "from_cortex": True,
                }

                from core import action_parser

                orchestrator_result = await action_parser.corrector_orchestrator(
                    text, corrector_context, bot, message
                )

                if orchestrator_result is True:
                    log_debug(
                        "[cortex_response_send] corrector_orchestrator executed actions; not forwarding text"
                    )
                    # The reply TEXT is not forwarded: delivery was delegated to
                    # the corrected actions. Recorded so a missing "Message sent"
                    # line is explainable instead of invisible.
                    await _record_delivery_failure(
                        "reply text not forwarded; delivery delegated to the "
                        "corrector's corrected actions",
                        chat_id=chat_id,
                        interface_path=kwargs.get("interface_path")
                        or f"{current_interface}/{chat_id}",
                        kwargs=kwargs,
                        text=text,
                    )
                    return
                elif orchestrator_result is False:
                    log_warning(
                        "[cortex_response_send] corrector_orchestrator blocked message"
                    )
                    await _record_delivery_failure(
                        "corrector_orchestrator blocked the message",
                        chat_id=chat_id,
                        interface_path=kwargs.get("interface_path")
                        or f"{current_interface}/{chat_id}",
                        kwargs=kwargs,
                        text=text,
                    )
                    return None
                else:
                    # Orchestrator declined — block to prevent sending raw
                    # malformed JSON to the user.
                    log_warning(
                        "[cortex_response_send] corrector_orchestrator returned None on JSON-like text; blocking to prevent invalid send"
                    )
                    await _record_delivery_failure(
                        "corrector_orchestrator declined; blocked instead of "
                        "sending unparseable JSON",
                        chat_id=chat_id,
                        interface_path=kwargs.get("interface_path")
                        or f"{current_interface}/{chat_id}",
                        kwargs=kwargs,
                        text=text,
                    )
                    return None

            except Exception as e:
                log_debug(f"[cortex_response_send] corrector_orchestrator failed: {e}")
                await _record_delivery_failure(
                    f"corrector path raised {type(e).__name__}: {e}",
                    chat_id=chat_id,
                    interface_path=kwargs.get("interface_path")
                    or f"{current_interface}/{chat_id}",
                    kwargs=kwargs,
                    text=text,
                )
                return None
        else:
            log_debug(
                "[cortex_response_send] No JSON-like content detected, sending as normal text"
            )

    if json_data:
        try:
            from types import SimpleNamespace

            # If the JSON payload contains any unexpected top-level keys,
            # escalate to the corrector so the LLM can resend proper actions.
            if isinstance(json_data, dict):
                try:
                    from core.validation_registry import get_validation_registry

                    allowed_metadata = (
                        get_validation_registry().get_response_metadata_keys()
                    )
                except Exception:
                    allowed_metadata = []
                extra_keys = [
                    k
                    for k in json_data.keys()
                    if k != "actions" and k not in allowed_metadata
                ]
                if extra_keys:
                    log_warning(
                        f"[cortex_response_send] JSON contains unexpected top-level keys {extra_keys}; invoking corrector"
                    )
                    # build a dummy message for the corrector call
                    msg_obj = SimpleNamespace()
                    msg_obj.chat_id = chat_id
                    msg_obj.text = ""
                    msg_obj.original_text = text
                    msg_obj.thread_id = kwargs.get("thread_id")
                    msg_obj.from_cortex = True

                    # Same canonical-interface rule as the unparseable-JSON path
                    # above: a legacy display label here made the correction's
                    # routing example teach an unregistered action.
                    _extra_keys_iface = _resolve_sender_interface_id(kwargs)
                    _extra_keys_iface_path = kwargs.get("interface_path") or (
                        f"{_extra_keys_iface}/{chat_id}"
                    )
                    corr_ctx = {
                        "interface": _extra_keys_iface,
                        "interface_path": _extra_keys_iface_path,
                        "original_chat_id": chat_id,
                        "original_thread_id": kwargs.get("thread_id"),
                        "original_text": text[:500] if text else "",
                        "from_cortex": True,
                    }
                    from core import action_parser

                    try:
                        corr_res = await action_parser.corrector_orchestrator(
                            text, corr_ctx, bot, msg_obj
                        )
                        if corr_res is True:
                            log_debug(
                                "[cortex_response_send] corrector handled extra keys; not forwarding text"
                            )
                            await _record_delivery_failure(
                                "reply text not forwarded; delivery delegated to "
                                "the corrector's corrected actions",
                                chat_id=chat_id,
                                interface_path=_extra_keys_iface_path,
                                kwargs=kwargs,
                                text=text,
                            )
                            return
                        elif corr_res is False:
                            log_warning(
                                "[cortex_response_send] corrector blocked message due to extra keys"
                            )
                            await _record_delivery_failure(
                                "corrector_orchestrator blocked the message",
                                chat_id=chat_id,
                                interface_path=_extra_keys_iface_path,
                                kwargs=kwargs,
                                text=text,
                            )
                            return None
                        else:
                            log_warning(
                                "[cortex_response_send] corrector declined; blocking message"
                            )
                            await _record_delivery_failure(
                                "corrector_orchestrator declined; blocked instead "
                                "of sending unparseable JSON",
                                chat_id=chat_id,
                                interface_path=_extra_keys_iface_path,
                                kwargs=kwargs,
                                text=text,
                            )
                            return None
                    except Exception as e:
                        log_warning(
                            f"[cortex_response_send] corrector invocation failed: {e}"
                        )
                        await _record_delivery_failure(
                            f"corrector path raised {type(e).__name__}: {e}",
                            chat_id=chat_id,
                            interface_path=_extra_keys_iface_path,
                            kwargs=kwargs,
                            text=text,
                        )
                        return None
                actions = json_data["actions"]
                if not isinstance(actions, list):
                    log_warning("[cortex_response_send] actions field must be a list")
                    actions = []
            elif isinstance(json_data, list):
                actions = json_data
            elif isinstance(json_data, dict) and "type" in json_data:
                actions = [json_data]
            else:
                log_warning(
                    f"[cortex_response_send] Unrecognized JSON structure: {json_data}"
                )
                actions = []

            if actions:
                # Create message context for actions
                message = SimpleNamespace()
                message.chat_id = chat_id
                message.text = ""
                message.original_text = text
                message.thread_id = kwargs.get("thread_id")
                if "event_id" in kwargs:
                    message.event_id = kwargs["event_id"]

                # Use action parser if available
                try:
                    from core.action_parser import run_actions

                    context = {
                        "interface": "telegram",
                        "original_chat_id": chat_id,
                        "original_thread_id": kwargs.get("thread_id"),
                        "original_text": text[:500] if text else "",
                        "thread_defaults": {
                            "telegram": None,
                            "discord": None,
                            "default": None,
                        },
                    }
                    if "event_id" in kwargs:
                        context["event_id"] = kwargs["event_id"]

                    # Remove duplicate actions
                    unique_actions = []
                    seen_actions = set()
                    for action in actions:
                        action_id = str(action)
                        if action_id not in seen_actions:
                            unique_actions.append(action)
                            seen_actions.add(action_id)

                    await run_actions(unique_actions, context, bot, message)
                    log_info(
                        f"[telegram_safe_send] Processed {len(unique_actions)} unique JSON actions"
                    )
                    return

                except Exception as e:
                    log_warning(
                        f"[telegram_safe_send] Failed to process JSON actions: {e}"
                    )

        except Exception as e:
            log_warning(f"[telegram_safe_send] Failed to process JSON actions: {e}")

    # Dedupe: suppress duplicate sends within a short window
    try:
        from core.config_manager import config_registry

        dedupe_window = int(
            config_registry.get_value(
                "OUTGOING_DEDUPE_WINDOW",
                _DEFAULT_DEDUPE_WINDOW,
                label="Outgoing Message Dedupe Window (s)",
                description="Seconds to suppress duplicate outbound messages to the same chat.",
                value_type=int,
                group="core",
                component="message_send",
                advanced=True,
            )
        )
    except Exception:
        dedupe_window = _DEFAULT_DEDUPE_WINDOW

    import time
    import re

    dedupe_key: str | None = None
    try:
        # perform a more aggressive normalization so we catch invisible
        # characters, zero‑width spaces, extra linebreaks, etc.  The previous
        # logic simply collapsed whitespace which could still leave a stray
        # ``\u200B`` or similar in the string and defeat the cache.
        norm_text = str(text)
        # strip out common zero‑width / control characters
        norm_text = re.sub(r"[\u200B-\u200F\uFEFF]", "", norm_text)
        # collapse all whitespace to single spaces, then trim
        norm_text = " ".join(norm_text.split()).strip()
        # limit key length so the cache doesn't grow unbounded
        norm_text = norm_text[:500]

        # always stringify chat_id to avoid int/str mismatches
        dedupe_key = f"{chat_id}:{norm_text}"
        last = _OUTGOING_DEDUPE.get(dedupe_key)
        now = time.time()
        if last and (now - last) < dedupe_window:
            log_info(
                f"[cortex_response_send] Suppressing duplicate send to {chat_id} (within {dedupe_window}s)"
            )
            return None
        # Stamp deferred to after a successful send — see below.
        # A failed attempt (e.g. thread-not-found before fallback retry) must
        # not poison the cache and suppress the retry within the dedup window.
    except Exception:
        # Non-fatal if dedupe fails
        pass

    # Send as normal text with chunking
    log_debug("[telegram_safe_send] Sending as normal text with chunking")
    try:
        last_sent = None
        for i in range(0, len(text), chunk_size):
            chunk = text[i : i + chunk_size]
            log_debug(
                f"[cortex_response_send] Sending chunk {i // chunk_size + 1} (len={len(chunk)}) to chat_id={chat_id}"
            )
            sent = await _send_with_retry(bot, chat_id, chunk, retries, delay, **kwargs)
            # _send_with_retry may return a telegram Message object or None; keep last non-None
            if sent is not None:
                last_sent = sent
        # Record the send only after all chunks succeed so a failed attempt
        # never blocks a legitimate fallback retry within the dedup window.
        if dedupe_key is not None:
            try:
                _OUTGOING_DEDUPE[dedupe_key] = time.time()
            except Exception:
                pass
        # Return the last sent message (if any) to allow callers to track trainer-side message ids
        return last_sent
    except Exception as e:
        # Log as WARNING if it's a stale identifier (handled by the caller's
        # fallback ladder), ERROR otherwise
        error_msg = str(e).lower()
        if (
            "thread not found" in error_msg
            or "message thread not found" in error_msg
            or _is_stale_reply_target(error_msg)
        ):
            log_warning(
                f"[cortex_response_send] Stale identifier error (will retry without it): {repr(e)}"
            )
        else:
            log_error(f"[cortex_response_send] Failed to send text chunks: {repr(e)}")
        raise


# Backward compatibility alias (deprecated, use cortex_response_send instead)
telegram_safe_send = cortex_response_send
