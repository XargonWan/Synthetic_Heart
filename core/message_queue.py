import asyncio
import inspect
import time
import queue as _thread_queue
import heapq
from datetime import datetime
import traceback
from types import SimpleNamespace

from core import plugin_instance, rate_limit, recent_chats
from core.logging_utils import log_debug, log_error, log_warning, log_info
from core.mention_utils import is_message_for_bot
from core.reaction_handler import react_when_mentioned, get_reaction_emoji
from core.core_initializer import INTERFACE_REGISTRY
from core.interfaces_registry import get_interface_registry
from core.chat_context_manager import get_context_memory
from core.session_meta import (
    set_session_meta as set_session_meta_fn,
    get_session_meta as get_session_meta_fn,
)
from plugins.blocklist import is_user_blocked
from plugins.chat_link import ChatLinkStore
from core.user_utils import ensure_message_user_fields

# Use a priority queue so events can be processed before regular messages
HIGH_PRIORITY = 0
NORMAL_PRIORITY = 1
LOW_PRIORITY = (
    2  # For autonomous beats (G.R.I.L.L.O.) - processed only when queue is idle
)

_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
_lock = asyncio.Lock()
_consumer_task: asyncio.Task | None = None
_counter = 0  # Monotonic counter to prevent dict comparison when priorities are equal


class MessageQueue:
    """Minimal thread-safe queue for interfaces expecting blocking semantics."""

    def __init__(self):
        self._q = _thread_queue.Queue()

    def put(self, item):
        self._q.put(item)

    def get(self, timeout=None):
        return self._q.get(timeout=timeout)


async def _delayed_put(item: dict, delay: float) -> None:
    global _counter
    await asyncio.sleep(delay)
    priority = HIGH_PRIORITY if item.get("priority") else NORMAL_PRIORITY
    _counter += 1
    await _queue.put((priority, _counter, item))


async def enqueue(
    bot,
    message,
    context_memory=None,
    priority: bool = False,
    interface_id: str = None,
    skip_mention_check: bool = False,
    original_message=None,
) -> None:
    """Enqueue a message for serialized processing with rate limiting.

    Args:
        bot: The bot instance
        message: The message to process
        context_memory: (Deprecated) Message context dict. If not provided, uses centralized context manager.
        priority: If True, message is added to front of queue (for events)
        interface_id: The interface identifier (e.g., 'webui', 'interface_name')
        skip_mention_check: If True, skip is_message_for_bot check (for 1:1 interfaces like ollama, webui)
        original_message: The original message object from the interface (for reactions)
    """
    # Use centralized context manager if context_memory not provided
    if context_memory is None:
        context_memory = get_context_memory()
    # Normalise the incoming message's user fields for consistent downstream handling
    try:
        ensure_message_user_fields(message)
    except Exception:
        # Don't block enqueue if normalization fails - keep behavior safe
        log_debug("[QUEUE] Failed to normalise message.user for incoming message")

    message_text = getattr(message, "text", "")
    user_id = (
        getattr(message.from_user, "id", "unknown") if message.from_user else "unknown"
    )
    chat_id = getattr(message, "chat_id", "unknown")
    log_debug(
        f"[QUEUE] DEBUG: enqueue() called with interface_id='{interface_id}', skip_mention_check={skip_mention_check}, message='{message_text}', user_id={user_id}, chat_id={chat_id}"
    )
    log_debug(
        f"[QUEUE] Processing message: '{message_text}' from user {user_id} in chat {chat_id}"
    )

    # Check if message is directed to bot (skip for 1:1 interfaces like ollama, webui)
    if not skip_mention_check:
        log_debug(
            "[QUEUE] DEBUG: Checking if message is for bot - calling is_message_for_bot"
        )

        human_count = getattr(message, "human_count", None)
        if human_count is None and hasattr(message, "chat"):
            human_count = getattr(message.chat, "human_count", None)

        log_debug(
            f"[QUEUE] DEBUG: human_count={human_count}, message.chat.type={getattr(message.chat, 'type', 'unknown')}"
        )

        # Get bot username for mention detection
        bot_username = None
        try:
            if bot and hasattr(bot, "get_me"):
                bot_info = await bot.get_me()
                bot_username = bot_info.username if bot_info else None
                log_debug(f"[QUEUE] Bot username: {bot_username}")
        except Exception as e:
            log_debug(f"[QUEUE] Error getting bot username: {e}")

        directed, reason = await is_message_for_bot(
            message, bot, bot_username=bot_username, human_count=human_count
        )
        log_debug(
            f"[QUEUE] DEBUG: is_message_for_bot returned directed={directed}, reason='{reason}'"
        )

        if not directed:
            log_debug("[QUEUE] DEBUG: Message not directed to bot - ignoring")
            if reason == "missing_human_count":
                log_debug("[QUEUE] DEBUG: Reason: missing_human_count")
            elif reason == "multiple_humans":
                log_debug("[QUEUE] DEBUG: Reason: multiple_humans")
            else:
                log_debug(f"[QUEUE] DEBUG: Reason: {reason or 'not directed to bot'}")
            return

        log_debug("[QUEUE] DEBUG: Message is directed to bot - continuing processing")
    else:
        log_debug(
            "[QUEUE] DEBUG: skip_mention_check=True - bypassing is_message_for_bot check (1:1 interface)"
        )
        directed = True

    # Add reaction if configured (REACT_WHEN_MENTIONED)
    try:
        emoji = get_reaction_emoji()
        log_debug(f"[QUEUE] get_reaction_emoji returned: '{emoji}'")
        log_debug(f"[QUEUE] About to check emoji: '{emoji}' (bool: {bool(emoji)})")
        if emoji and directed:
            log_debug("[QUEUE] About to get interface registry")
            interface = INTERFACE_REGISTRY.get(interface_id)
            log_debug(f"[QUEUE] Interface for {interface_id}: {interface}")
            log_debug(f"[QUEUE] Interface type: {type(interface)}")
            log_debug(f"[QUEUE] original_message is None: {original_message is None}")
            if interface:
                log_debug(
                    f"[QUEUE] Adding reaction '{emoji}' via interface {interface_id}"
                )
                await react_when_mentioned(
                    interface, original_message or message, emoji
                )
            else:
                log_warning(f"[QUEUE] No interface found for {interface_id}")
        else:
            log_debug("[QUEUE] No reaction emoji configured or not directed")
    except Exception as e:
        log_error(f"[QUEUE] Error adding reaction: {e}")
        log_debug(f"[QUEUE] Reaction traceback: {traceback.format_exc()}")

    # Check if user is blocked (but allow trainers)
    user_id = message.from_user.id if message.from_user else 0
    registry = get_interface_registry()
    is_trainer = registry.is_trainer(interface_id, user_id) if interface_id else False

    log_debug(
        f"[QUEUE] DEBUG: Checking blocklist - user_id={user_id}, interface_id='{interface_id}', is_trainer={is_trainer}"
    )

    if not is_trainer and await is_user_blocked(user_id):
        log_debug(f"[QUEUE] DEBUG: User {user_id} is blocked - ignoring message")
        return

    log_debug(
        f"[QUEUE] DEBUG: User {user_id} is not blocked or is trainer, continuing processing"
    )

    plugin = plugin_instance.get_plugin()
    if not plugin:
        log_error("[QUEUE] No active plugin")
        return

    try:
        max_messages, window_seconds, trainer_fraction = plugin.get_rate_limit()
    except Exception as e:  # pragma: no cover - plugin may misbehave
        log_error(f"[QUEUE] Error obtaining rate limit: {repr(e)}", e)
        max_messages, window_seconds, trainer_fraction = float("inf"), 1, 1.0

    chat_id = message.chat_id
    llm_name = plugin.__class__.__module__.split(".")[-1]

    if (
        not is_trainer
        and user_id not in (0, -1)
        and not rate_limit.is_allowed(
            llm_name,
            user_id,
            interface_id or "unknown",
            max_messages,
            window_seconds,
            trainer_fraction,
            consume=False,
        )
    ):
        log_debug(
            f"[RATE LIMIT] Delaying user {user_id} by {delay} seconds (quota exceeded)"
        )
        item = {
            "bot": bot,
            "message": message,
            "chat_id": chat_id,
            "timestamp": time.time(),
            "context": context_memory,
            "priority": priority,
        }
        asyncio.create_task(_delayed_put(item, delay))
        return

    log_debug("[QUEUE] Rate limit check passed - continuing to enqueue message")

    async def _broadcast_global_animation_state(state: str) -> None:
        """Best-effort global animation state update (broadcast to all WebUI clients).

        Interfaces can disable this by setting `core_animation_broadcast=False` in the
        context dict passed to enqueue.
        """
        try:
            context_obj = context_memory
            if (
                isinstance(context_obj, dict)
                and context_obj.get("core_animation_broadcast") is False
            ):
                return
        except Exception:
            pass

        try:
            from core.persona_manager import get_persona_manager

            pm = get_persona_manager()
            if pm:
                await pm.set_animation_state(state, session_id=None)
        except Exception as anim_exc:
            log_debug(
                f"[QUEUE] Failed to broadcast animation state '{state}': {anim_exc}"
            )

    def _resolve_message_animation_state(event: str) -> str:
        """Resolve animation state for message lifecycle events.

        Currently used for `event='received'` (default: 'think').
        Override priority:
        1) context keys: `message_animation_state_received` / `animation_state_on_message_received`
        2) interface duck-typed method: `get_animation_state_for_message_event(...)`
        3) default
        """
        default_state = "think" if event == "received" else "think"

        context_obj = context_memory
        if isinstance(context_obj, dict):
            if event == "received":
                for k in (
                    "message_animation_state_received",
                    "animation_state_on_message_received",
                ):
                    v = context_obj.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()

        iface = None
        try:
            iface = INTERFACE_REGISTRY.get(interface_id) if interface_id else None
        except Exception:
            iface = None

        try:
            if iface is not None:
                fn = getattr(iface, "get_animation_state_for_message_event", None)
                if callable(fn):
                    v = fn(
                        event=event,
                        interface_id=interface_id,
                        context=context_obj,
                        message=message,
                        original_message=original_message,
                    )
                    if isinstance(v, str) and v.strip():
                        return v.strip()
        except Exception:
            pass

        return default_state

    # Per piano: appena il messaggio viene accettato per processing, entra in THINK.
    await _broadcast_global_animation_state(
        _resolve_message_animation_state("received")
    )

    meta = message.chat.title or message.chat.username or message.chat.first_name
    # Persist last-active chat, but don't let DB failures abort enqueueing
    try:
        await recent_chats.track_chat(chat_id, meta)
    except Exception as e:
        log_warning(f"[QUEUE] recent_chats.track_chat failed but continuing: {e}")

    # Extract thread_id - unified field name, check both Telegram and generic names
    # DEBUG: let's see what telegram message actually contains
    thread_attrs = [attr for attr in dir(message) if "thread" in attr.lower()]
    log_debug(f"[QUEUE] Available thread attributes in message: {thread_attrs}")

    # Check for thread_id, but also check message_thread_id (Telegram's native field)
    # Note: DO NOT set message.thread_id - Message objects are immutable
    thread_id_val = getattr(message, "thread_id", None)
    if thread_id_val is None:
        thread_id_val = getattr(message, "message_thread_id", None)

    log_debug(f"[QUEUE] thread_id extracted: {thread_id_val}")

    thread_id = thread_id_val
    interface = (
        interface_id
        if interface_id
        else (
            bot.get_interface_id()
            if bot and hasattr(bot, "get_interface_id")
            else bot.__class__.__name__
            if bot
            else None
        )
    )

    # Resolve chat and thread names automatically
    chat_name = None
    message_thread_name = None
    try:
        store = ChatLinkStore()
        resolver = store.get_name_resolver(interface)
        if resolver:
            log_debug(f"[QUEUE] Resolving names for chat {chat_id}, thread {thread_id}")
            names = await resolver(chat_id, thread_id, bot)
            if names:
                chat_name = names.get("chat_name")
                message_thread_name = names.get("message_thread_name")
                log_debug(
                    f"[QUEUE] Resolved names: chat='{chat_name}', thread='{message_thread_name}'"
                )
            else:
                log_debug("[QUEUE] Resolver returned no names")
        else:
            log_debug(f"[QUEUE] No name resolver for interface '{interface}'")
    except Exception as e:
        log_warning(f"[QUEUE] Failed to resolve chat/thread names: {e}")

    item = {
        "bot": bot,
        "message": message,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "interface": interface,
        "chat_name": chat_name,
        "message_thread_name": message_thread_name,
        "timestamp": time.time(),
        "context": context_memory,
        "priority": priority,
    }

    global _counter
    priority_val = HIGH_PRIORITY if priority else NORMAL_PRIORITY
    _counter += 1
    await _queue.put((priority_val, _counter, item))
    log_debug(f"[QUEUE] Message successfully put in queue with priority {priority_val}")

    if priority:
        log_debug(
            f"[QUEUE] High-priority message enqueued from {interface} chat {chat_id}"
            f" thread {thread_id} by user {user_id}"
        )
    else:
        log_debug(
            f"[QUEUE] Regular message enqueued from {interface} chat {chat_id}"
            f" thread {thread_id} by user {user_id}"
        )


async def enqueue_low_priority(
    bot, message, context_memory=None, interface_id: str = None, original_message=None
) -> None:
    """Enqueue a low-priority (background) message into the global queue.

    This is a convenience wrapper for plugins that want to submit background
    messages that must never block other user messages. It performs the same
    normalization and persistence steps as :func:`enqueue` but pushes the
    item with LOW_PRIORITY (value 2).
    """
    if context_memory is None:
        context_memory = get_context_memory()

    # Ensure message fields exist before queueing
    try:
        ensure_message_user_fields(message)
    except Exception:
        log_debug("[QUEUE] Failed to normalise message.user for enqueue_low_priority")

    user_id = (
        getattr(message.from_user, "id", "unknown") if message.from_user else "unknown"
    )
    chat_id = getattr(message, "chat_id", "unknown")
    thread_id = getattr(message, "thread_id", None) or getattr(
        message, "message_thread_id", None
    )

    # Resolve chat name if possible - best effort
    chat_name = None
    message_thread_name = None
    try:
        store = ChatLinkStore()
        resolver = store.get_name_resolver(interface_id)
        if resolver:
            names = await resolver(chat_id, thread_id, bot)
            if names:
                chat_name = names.get("chat_name")
                message_thread_name = names.get("message_thread_name")
    except Exception:
        pass

    item = {
        "bot": bot,
        "message": message,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "interface": interface_id
        or (
            bot.get_interface_id() if bot and hasattr(bot, "get_interface_id") else None
        ),
        "chat_name": chat_name,
        "message_thread_name": message_thread_name,
        "timestamp": time.time(),
        "context": context_memory,
        "priority": False,
    }

    global _counter
    _counter += 1
    # Use explicit LOW_PRIORITY value
    await _queue.put((LOW_PRIORITY, _counter, item))
    log_debug(
        f"[QUEUE] Low-priority message enqueued from {item['interface']} chat {chat_id} thread {thread_id}"
    )


async def compact_similar_messages(first: dict, limit: int = 5) -> list:
    """Collect already-queued messages from same chat/thread/interface."""
    batch = [first]
    chat_id = first["chat_id"]
    thread_id = first.get("thread_id")
    interface = first.get("interface")
    ts = first["timestamp"]

    seen_ids = set()
    first_msg = first.get("message")
    if first_msg:
        mid = getattr(first_msg, "message_id", None)
        if mid:
            seen_ids.add(mid)

    queue_items = list(_queue._queue)
    dirty = False
    for item_tuple in queue_items:
        if len(batch) >= limit:
            break
        if len(item_tuple) == 3:
            prio, counter, item = item_tuple
        else:
            prio, item = item_tuple
            counter = None
        if (
            item["chat_id"] == chat_id
            and item.get("thread_id") == thread_id
            and item.get("interface") == interface
            and item["timestamp"] - ts <= 600
        ):
            msg = item.get("message")
            if msg:
                mid = getattr(msg, "message_id", None)
                if mid and mid in seen_ids:
                    try:
                        _queue._queue.remove(item_tuple)
                        dirty = True
                    except ValueError:
                        pass
                    log_debug(f"[COMPACT] Removed duplicate message {mid} from queue")
                    continue
                if mid:
                    seen_ids.add(mid)
            try:
                _queue._queue.remove(item_tuple)
                dirty = True
                batch.append(item)
            except ValueError:
                pass

    if dirty:
        heapq.heapify(_queue._queue)

    batch.sort(key=lambda x: x["timestamp"])

    if len(batch) > 1:
        log_debug(f"[COMPACT] Compacted {len(batch)} messages from chat {chat_id}")

    return batch


async def _consumer_loop() -> None:
    """Continuously process queued messages one at a time."""
    log_info("[QUEUE] Consumer loop started")
    while True:
        try:
            priority, counter, item = await _queue.get()
            log_debug(
                f"[QUEUE] Dequeued message from chat {item.get('chat_id')} (priority={priority}, counter={counter})"
            )

            async with _lock:
                batch = await compact_similar_messages(item)
                final = batch[0]
                if len(batch) > 1 and final.get("message"):
                    lines = []
                    for b in batch:
                        msg = b.get("message")
                        if not (msg and getattr(msg, "text", None)):
                            continue
                        user = getattr(msg, "from_user", None)
                        if user:
                            # Prefer @username if present, otherwise prefer full_name
                            from core.user_utils import (
                                get_user_display_name,
                                get_user_usertag,
                            )

                            if getattr(user, "username", None):
                                name = get_user_usertag(user)
                            elif getattr(user, "full_name", None):
                                name = get_user_display_name(user)
                            else:
                                name = f"user_{get_user_display_name(user)}"
                            lines.append(f"{name}: {msg.text}")
                        else:
                            lines.append(msg.text)
                    base = final["message"]
                    merged = SimpleNamespace(
                        chat_id=getattr(base, "chat_id", None),
                        message_id=getattr(base, "message_id", None),
                        text="\n".join(lines),
                        from_user=SimpleNamespace(
                            id=0, username="group", full_name="group"
                        ),
                        date=getattr(base, "date", datetime.utcnow()),
                        thread_id=getattr(base, "thread_id", None),
                        chat=getattr(base, "chat", None),
                        reply_to_message=getattr(base, "reply_to_message", None),
                    )
                    final["message"] = merged
                    final["context"] = batch[-1].get("context", final.get("context"))
                log_debug(
                    f"[QUEUE] Processing message from chat {final.get('chat_id')}"
                )

            # Ensure chat exists with resolved names
            chat_name = final.get("chat_name")
            message_thread_name = final.get("message_thread_name")
            if chat_name or message_thread_name:
                try:
                    store = ChatLinkStore()
                    await store.ensure_chat_exists(
                        chat_id=final.get("chat_id"),
                        thread_id=final.get("thread_id"),
                        interface=final.get("interface"),
                        chat_name=chat_name,
                        message_thread_name=message_thread_name,
                    )
                    log_debug(
                        f"[QUEUE] Updated chat record with names: chat='{chat_name}', thread='{message_thread_name}'"
                    )
                except Exception as e:
                    log_warning(f"[QUEUE] Failed to update chat names: {e}")

            plugin = plugin_instance.get_plugin()
            if not plugin:
                log_error("[QUEUE] No active plugin when dispatching")
                continue

            try:
                max_messages, window_seconds, trainer_fraction = plugin.get_rate_limit()
            except Exception as e:  # pragma: no cover - plugin may misbehave
                log_error(f"[QUEUE] Error obtaining rate limit: {repr(e)}", e)
                max_messages, window_seconds, trainer_fraction = float("inf"), 1, 1.0

            user_msg = final.get("message")
            user_id = (
                user_msg.from_user.id
                if user_msg is not None and getattr(user_msg, "from_user", None)
                else 0
            )
            llm_name = plugin.__class__.__module__.split(".")[-1]

            # Check if user is trainer for this interface
            registry = get_interface_registry()
            interface_id = getattr(user_msg, "interface_id", "unknown")
            is_trainer = registry.is_trainer(interface_id, user_id)

            if not is_trainer and not rate_limit.is_allowed(
                llm_name,
                user_id,
                interface_id,
                max_messages,
                window_seconds,
                trainer_fraction,
                consume=True,
            ):
                delay = 300
                log_debug(
                    f"[RATE LIMIT] Delaying user {user_id} by {delay} seconds (quota exceeded)"
                )
                asyncio.create_task(_delayed_put(final, delay))
                continue

            try:
                # Get timeout configuration from message_chain module
                from core.message_chain import RESPONSE_TIMEOUT

                timeout_seconds = int(RESPONSE_TIMEOUT) if RESPONSE_TIMEOUT else 240

                # Check if this is an event prompt
                if "event_prompt" in final:
                    # Create a mock message object with event_id for events
                    mock_message = SimpleNamespace()
                    mock_message.event_id = final["context"].get("event_id")
                    mock_message.chat_id = "TARDIS/system/events"
                    mock_message.message_id = f"event_{mock_message.event_id}"

                    # Deliver the structured event prompt using the standard pipeline with timeout
                    try:
                        # Set session meta 'processing' True for this chat
                        try:
                            # Ensure interface_path is defined for events so session meta can be set
                            interface_path = final.get("interface") or (
                                bot.__class__.__name__ if bot else "unknown"
                            )
                            existing_meta = (
                                await get_session_meta_fn(interface_path) or {}
                            )
                            existing_meta["processing"] = True
                            await set_session_meta_fn(interface_path, existing_meta)
                        except Exception as sm_e:
                            log_debug(
                                f"[QUEUE] Failed to set processing session meta: {sm_e}"
                            )
                        await asyncio.wait_for(
                            plugin_instance.handle_incoming_message(
                                final["bot"],
                                mock_message,
                                final["event_prompt"],
                                final.get("interface"),
                            ),
                            timeout=timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        log_error(
                            f"[QUEUE] Event processing timed out after {timeout_seconds}s for event {mock_message.event_id}"
                        )
                        # Event timeout - message_chain will have already sent fallback if needed
                else:
                    # Build interface_path and add to context for prompt_engine to use
                    chat_id = final.get("chat_id")
                    thread_id = final.get("thread_id")
                    interface_id = final.get("interface", "unknown")

                    # Create interface_path and add to context
                    from core.interface_path_utils import build_interface_path

                    interface_path = build_interface_path(
                        interface_id,
                        str(chat_id),
                        str(thread_id) if thread_id else None,
                    )

                    # Add interface_path to context dict so prompt_engine can access it
                    context = final.get("context", {})
                    if isinstance(context, dict):
                        context["interface_path"] = interface_path
                        context["thread_id"] = thread_id
                        log_debug(
                            f"[QUEUE] Added interface_path to context: {interface_path}"
                        )
                        try:
                            # Extra debug: log processing context to aid diagnosing timeouts/routing
                            keys = list(context.keys())
                        except Exception:
                            keys = []
                        log_debug(
                            f"[QUEUE] PROCESSING CONTEXT: interface={interface_id}, interface_path={interface_path}, chat_id={chat_id}, thread_id={thread_id}, context_keys={keys}, message_preview={(getattr(final.get('message'), 'text', None) or '')[:200]}"
                        )
                    else:
                        log_warning(
                            "[QUEUE] Context is not a dict, cannot add interface_path"
                        )

                    async def _call_bot_generation_start() -> None:
                        try:
                            bot_obj = final.get("bot")
                            if bot_obj is None:
                                return
                            fn = getattr(bot_obj, "on_generation_start", None)
                            if fn is None:
                                return
                            if inspect.iscoroutinefunction(fn):
                                await fn(
                                    interface_path=interface_path,
                                    context=context,
                                    message=final.get("message"),
                                )
                            else:
                                fn(
                                    interface_path=interface_path,
                                    context=context,
                                    message=final.get("message"),
                                )
                        except Exception as hook_exc:
                            log_debug(
                                f"[QUEUE] on_generation_start hook failed for {interface_path}: {hook_exc}"
                            )

                    def _resolve_generation_animation_state(event: str) -> str:
                        """Resolve an animation state name for generation lifecycle.

                        Priority (best-effort):
                        1) context overrides (interface can set these when enqueueing)
                        2) optional interface methods (duck-typed)
                        3) defaults: start->write, end->idle

                        Supported context keys:
                        - generation_animation_state_start / generation_animation_state_end
                        - animation_state_on_generation_start / animation_state_on_generation_end
                        """
                        default_state = "write" if event == "start" else "idle"

                        context_obj = final.get("context")
                        if isinstance(context_obj, dict):
                            if event == "start":
                                for k in (
                                    "generation_animation_state_start",
                                    "animation_state_on_generation_start",
                                ):
                                    v = context_obj.get(k)
                                    if isinstance(v, str) and v.strip():
                                        return v.strip()
                            else:
                                for k in (
                                    "generation_animation_state_end",
                                    "animation_state_on_generation_end",
                                ):
                                    v = context_obj.get(k)
                                    if isinstance(v, str) and v.strip():
                                        return v.strip()

                        try:
                            iface_id = final.get("interface")
                            iface = (
                                INTERFACE_REGISTRY.get(iface_id) if iface_id else None
                            )
                        except Exception:
                            iface = None

                        # Optional interface methods (duck-typed)
                        try:
                            if iface is not None:
                                fn = getattr(
                                    iface,
                                    "get_animation_state_for_generation_event",
                                    None,
                                )
                                if callable(fn):
                                    v = fn(
                                        event=event,
                                        interface_path=interface_path,
                                        context=context_obj,
                                        message=final.get("message"),
                                    )
                                    if isinstance(v, str) and v.strip():
                                        return v.strip()
                        except Exception:
                            pass

                        try:
                            if iface is not None:
                                fn = getattr(
                                    iface, "get_generation_animation_states", None
                                )
                                if callable(fn):
                                    res = fn(
                                        interface_path=interface_path,
                                        context=context_obj,
                                        message=final.get("message"),
                                    )
                                    # Accept (start, end) or dict-like
                                    if isinstance(res, (tuple, list)) and len(res) >= 2:
                                        v = res[0] if event == "start" else res[1]
                                        if isinstance(v, str) and v.strip():
                                            return v.strip()
                                    if isinstance(res, dict):
                                        v = res.get(event)
                                        if isinstance(v, str) and v.strip():
                                            return v.strip()
                        except Exception:
                            pass

                        return default_state

                    async def _broadcast_global_animation_state(state: str) -> None:
                        """Best-effort global animation state update (broadcast to all WebUI clients).

                        Some interfaces (e.g. Telegram) pass a raw Bot object which does not
                        implement generation hooks; this keeps the THINK → WRITE → IDLE flow
                        consistent with `plan-animationHandlerVrm.prompt.md`.
                        """
                        # Allow interfaces to disable core broadcast if they manage it themselves.
                        context_obj = final.get("context")
                        if (
                            isinstance(context_obj, dict)
                            and context_obj.get("core_animation_broadcast") is False
                        ):
                            return

                        try:
                            from core.persona_manager import get_persona_manager

                            pm = get_persona_manager()
                            if pm:
                                await pm.set_animation_state(state, session_id=None)
                        except Exception as anim_exc:
                            log_debug(
                                f"[QUEUE] Failed to broadcast animation state '{state}': {anim_exc}"
                            )

                    async def _call_bot_generation_end(task: asyncio.Task) -> None:
                        try:
                            bot_obj = final.get("bot")
                            if bot_obj is None:
                                return
                            fn = getattr(bot_obj, "on_generation_end", None)
                            if fn is None:
                                return
                            success = True
                            try:
                                exc = task.exception()
                                success = exc is None
                            except Exception:
                                success = False
                            if inspect.iscoroutinefunction(fn):
                                await fn(
                                    interface_path=interface_path,
                                    success=success,
                                    context=context,
                                    message=final.get("message"),
                                )
                            else:
                                fn(
                                    interface_path=interface_path,
                                    success=success,
                                    context=context,
                                    message=final.get("message"),
                                )
                        except Exception as hook_exc:
                            log_debug(
                                f"[QUEUE] on_generation_end hook failed for {interface_path}: {hook_exc}"
                            )

                    try:
                        # Ensure the message object has normalized user fields and date
                        try:
                            ensure_message_user_fields(final.get("message"))
                        except Exception:
                            log_debug(
                                "[QUEUE] Failed to normalise message fields in consumer loop"
                            )

                        # Optional: notify the interface/bot that generation is starting.
                        # This is intentionally duck-typed so the core does not hard-code interface logic.
                        await _call_bot_generation_start()

                        # Global animation flow: when generation starts, switch to an interface-defined
                        # state (default: 'write'). This keeps THINK → (WRITE|TALK|...) consistent.
                        await _broadcast_global_animation_state(
                            _resolve_generation_animation_state("start")
                        )

                        # Run message processing in a Task so we can avoid hard-cancelling long-running
                        # flows (e.g. Selenium-based LLMs). On timeout we keep the task running and
                        # clear the session 'processing' flag when it eventually finishes.
                        processing_task = asyncio.create_task(
                            plugin_instance.handle_incoming_message(
                                final["bot"],
                                final["message"],
                                context,
                                final.get("interface"),
                            )
                        )
                        log_debug(
                            f"[QUEUE] Dispatched handle_incoming_message task for interface_path={interface_path}, interface={final.get('interface')}"
                        )
                        timed_out = False
                        try:
                            await asyncio.wait_for(
                                processing_task, timeout=timeout_seconds
                            )
                        except asyncio.TimeoutError:
                            timed_out = True
                            log_error(
                                f"[QUEUE] Message processing timed out after {timeout_seconds}s for chat {chat_id}"
                            )
                            # Additional debug to capture routing context when timeouts occur
                            try:
                                log_debug(
                                    f"[QUEUE] TIMEOUT CONTEXT: interface={final.get('interface')}, interface_path={interface_path}, chat_id={chat_id}, thread_id={thread_id}, message_preview={(getattr(final.get('message'), 'text', None) or '')[:200]}"
                                )
                            except Exception:
                                pass

                            # Attempt to send a fallback message so the client isn't left waiting.
                            try:
                                from core.message_chain import send_llm_fallback_message

                                await send_llm_fallback_message(
                                    final.get("bot"),
                                    final.get("message"),
                                    failure_reason=f"timeout after {timeout_seconds}s",
                                    context=context,
                                )
                                log_debug(
                                    f"[QUEUE] Sent fallback message due to timeout to {interface_path}"
                                )
                            except Exception as send_exc:
                                log_warning(
                                    f"[QUEUE] Failed to send fallback message on timeout for chat {chat_id}: {send_exc}"
                                )

                            async def _clear_processing_when_done() -> None:
                                try:
                                    # Log exceptions from the background task, if any
                                    try:
                                        exc = processing_task.exception()
                                        if exc is not None:
                                            log_warning(
                                                f"[QUEUE] Background processing task error for chat {chat_id}: {exc}"
                                            )
                                    except asyncio.CancelledError:
                                        return
                                    except Exception:
                                        pass

                                    # Notify optional hook that generation ended (even after a timeout).
                                    try:
                                        await _call_bot_generation_end(processing_task)
                                    except Exception:
                                        pass

                                    # Only clear 'processing' if no other messages are pending for this chat
                                    still_pending = False
                                    for prio, _, queued_item in list(_queue._queue):
                                        item_chat = (
                                            queued_item.get("chat_id")
                                            if isinstance(queued_item, dict)
                                            else getattr(queued_item, "chat_id", None)
                                        )
                                        if item_chat == chat_id:
                                            still_pending = True
                                            break
                                    if not still_pending:
                                        try:
                                            existing_meta = (
                                                await get_session_meta_fn(
                                                    interface_path
                                                )
                                                or {}
                                            )
                                            existing_meta["processing"] = False
                                            await set_session_meta_fn(
                                                interface_path, existing_meta
                                            )
                                        except Exception as set_e:
                                            log_debug(
                                                f"[QUEUE] Failed to clear processing session meta (background): {set_e}"
                                            )

                                        # Only return to IDLE when no other messages are pending for this chat.
                                        await _broadcast_global_animation_state(
                                            _resolve_generation_animation_state("end")
                                        )
                                except Exception as e:
                                    log_debug(
                                        f"[QUEUE] Error in background completion handler for chat {chat_id}: {e}"
                                    )

                            # Keep processing in background; clear meta when done.
                            try:
                                processing_task.add_done_callback(
                                    lambda _t: asyncio.create_task(
                                        _clear_processing_when_done()
                                    )
                                )
                            except Exception as cb_e:
                                log_debug(
                                    f"[QUEUE] Failed to attach background completion callback: {cb_e}"
                                )
                        finally:
                            # If we completed within the timeout (success or error), notify generation end now.
                            if not timed_out:
                                try:
                                    await _call_bot_generation_end(processing_task)
                                except Exception:
                                    pass
                    except asyncio.TimeoutError:
                        # (handled above)
                        pass
                    finally:
                        # Unmark processing for this session if no more messages are pending
                        try:
                            # If we timed out and left processing running in background,
                            # keep the session marked as processing until the callback clears it.
                            if "timed_out" in locals() and timed_out:
                                still_pending = True
                            else:
                                # Check for any remaining queued messages for the same chat
                                still_pending = False
                                for prio, _, queued_item in list(_queue._queue):
                                    item_chat = (
                                        queued_item.get("chat_id")
                                        if isinstance(queued_item, dict)
                                        else queued_item.chat_id
                                    )
                                    if item_chat == chat_id:
                                        still_pending = True
                                        break
                            if not still_pending:
                                try:
                                    existing_meta = (
                                        await get_session_meta_fn(interface_path) or {}
                                    )
                                    existing_meta["processing"] = False
                                    await set_session_meta_fn(
                                        interface_path, existing_meta
                                    )
                                except Exception as set_e:
                                    log_debug(
                                        f"[QUEUE] Failed to clear processing session meta: {set_e}"
                                    )

                                # Return to IDLE when this chat is done processing.
                                await _broadcast_global_animation_state(
                                    _resolve_generation_animation_state("end")
                                )
                        except Exception as pending_e:
                            log_debug(
                                f"[QUEUE] Error checking pending queue for chat {chat_id}: {pending_e}"
                            )
            except Exception as e:  # pragma: no cover - plugin may misbehave
                log_error(
                    f"[ERROR] Failed to process message from chat {final['chat_id']}: {e}\n{traceback.format_exc()}",
                )
                bot = final.get("bot")
                chat_id = final.get("chat_id")
                thread_id = final.get("thread_id")
                try:
                    if bot and chat_id:
                        kwargs = {"chat_id": chat_id, "text": "😵‍💫"}
                        if thread_id:
                            # Convert thread_id to int if it's a string (for Telegram API compatibility)
                            if isinstance(thread_id, str) and thread_id.isdigit():
                                thread_id = int(thread_id)
                            kwargs["message_thread_id"] = thread_id
                        reply_msg = final.get("message")
                        reply_id = getattr(reply_msg, "message_id", None)
                        if reply_id:
                            kwargs["reply_to_message_id"] = reply_id
                        await bot.send_message(**kwargs)
                except Exception as send_err:  # pragma: no cover - best effort
                    log_warning(f"[QUEUE] Failed to send fallback message: {send_err}")
            finally:
                for _ in batch:
                    _queue.task_done()
        except asyncio.CancelledError:
            log_info("[QUEUE] Consumer loop cancelled")
            break
        except Exception as e:
            log_error(
                f"[QUEUE] Unexpected error in consumer loop: {repr(e)}\n{traceback.format_exc()}"
            )


async def enqueue_event(bot, prompt_data, event_id: int = None) -> None:
    """Enqueue an event prompt with highest priority."""
    # Debug log to verify the payload content
    log_debug(f"[QUEUE] Verifying event payload: {prompt_data}")

    # Check required fields in the payload - adjust for the actual structure
    payload = prompt_data.get("input", {}).get("payload", {})
    if not payload.get("description"):
        log_error(
            "[QUEUE] Invalid event payload: missing 'description' in input.payload"
        )
        return

    item = {
        "bot": bot,
        "message": None,  # Events don't have actual messages
        "chat_id": "TARDIS/system/events",
        "thread_id": None,
        "interface": bot.__class__.__name__ if bot else None,
        "timestamp": time.time(),
        "context": {"event_id": event_id} if event_id else {},
        "priority": True,
        "event_prompt": prompt_data,  # Special event data
    }

    # Check to avoid duplicates in the queue
    for prio, cnt, queued_item in list(_queue._queue):
        if queued_item.get("event_prompt") == prompt_data:
            log_warning("[QUEUE] Duplicate event detected, not added to the queue")
            return

    global _counter
    _counter += 1
    await _queue.put((HIGH_PRIORITY, _counter, item))
    log_debug(f"[QUEUE] Event added to the queue with priority: {prompt_data}")
    log_debug(f"[QUEUE] Current queue state: {list(_queue._queue)}")


async def run() -> None:
    """Convenience wrapper to launch the consumer task if not running."""
    global _consumer_task

    if _consumer_task and not _consumer_task.done():
        log_debug("[QUEUE] Consumer already running")
        return

    _consumer_task = asyncio.create_task(_consumer_loop())
    log_info("[QUEUE] Consumer task started")
