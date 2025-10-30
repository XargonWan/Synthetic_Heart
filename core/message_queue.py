import asyncio
import time
import queue as _thread_queue
from datetime import datetime
import traceback
from types import SimpleNamespace

from core import plugin_instance, rate_limit, recent_chats
from core.logging_utils import log_debug, log_error, log_warning, log_info
from core.mention_utils import is_message_for_bot
from core.reaction_handler import react_when_mentioned, get_reaction_emoji
from core.core_initializer import INTERFACE_REGISTRY
from core.interfaces_registry import get_interface_registry
from plugins.blocklist import is_user_blocked
from plugins.chat_link import ChatLinkStore

# Use a priority queue so events can be processed before regular messages
HIGH_PRIORITY = 0
NORMAL_PRIORITY = 1

_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
_lock = asyncio.Lock()
_consumer_task: asyncio.Task | None = None


class MessageQueue:
    """Minimal thread-safe queue for interfaces expecting blocking semantics."""

    def __init__(self):
        self._q = _thread_queue.Queue()

    def put(self, item):
        self._q.put(item)

    def get(self, timeout=None):
        return self._q.get(timeout=timeout)


async def _delayed_put(item: dict, delay: float) -> None:
    await asyncio.sleep(delay)
    priority = HIGH_PRIORITY if item.get("priority") else NORMAL_PRIORITY
    await _queue.put((priority, item))


async def enqueue(bot, message, context_memory, priority: bool = False, interface_id: str = None, skip_mention_check: bool = False, original_message=None) -> None:
    """Enqueue a message for serialized processing with rate limiting.

    Args:
        bot: The bot instance
        message: The message to process
        context_memory: Message context
        priority: If True, message is added to front of queue (for events)
        interface_id: The interface identifier (e.g., 'webui', 'interface_name')
        skip_mention_check: If True, skip is_message_for_bot check (for 1:1 interfaces like ollama, webui)
        original_message: The original message object from the interface (for reactions)
    """
    message_text = getattr(message, 'text', '')
    user_id = getattr(message.from_user, 'id', 'unknown') if message.from_user else 'unknown'
    chat_id = getattr(message, 'chat_id', 'unknown')
    log_debug(f"[QUEUE] DEBUG: enqueue() called with interface_id='{interface_id}', skip_mention_check={skip_mention_check}, message='{message_text}', user_id={user_id}, chat_id={chat_id}")
    log_debug(f"[QUEUE] Processing message: '{message_text}' from user {user_id} in chat {chat_id}")
    
    # Check if message is directed to bot (skip for 1:1 interfaces like ollama, webui)
    if not skip_mention_check:
        log_debug(f"[QUEUE] DEBUG: Checking if message is for bot - calling is_message_for_bot")
        
        human_count = getattr(message, "human_count", None)
        if human_count is None and hasattr(message, "chat"):
            human_count = getattr(message.chat, "human_count", None)

        log_debug(f"[QUEUE] DEBUG: human_count={human_count}, message.chat.type={getattr(message.chat, 'type', 'unknown')}")
        
        # Get bot username for mention detection
        bot_username = None
        try:
            if bot and hasattr(bot, 'get_me'):
                bot_info = await bot.get_me()
                bot_username = bot_info.username if bot_info else None
                log_debug(f"[QUEUE] Bot username: {bot_username}")
        except Exception as e:
            log_debug(f"[QUEUE] Error getting bot username: {e}")
        
        directed, reason = await is_message_for_bot(
            message, bot, bot_username=bot_username, human_count=human_count
        )
        log_debug(f"[QUEUE] DEBUG: is_message_for_bot returned directed={directed}, reason='{reason}'")
        
        if not directed:
            log_debug(f"[QUEUE] DEBUG: Message not directed to bot - ignoring")
            if reason == "missing_human_count":
                log_debug("[QUEUE] DEBUG: Reason: missing_human_count")
            elif reason == "multiple_humans":
                log_debug("[QUEUE] DEBUG: Reason: multiple_humans")
            else:
                log_debug(f"[QUEUE] DEBUG: Reason: {reason or 'not directed to bot'}")
            return

        log_debug(f"[QUEUE] DEBUG: Message is directed to bot - continuing processing")
        
        # Add reaction if configured (REACT_WHEN_MENTIONED)
        try:
            emoji = get_reaction_emoji()
            log_debug(f"[QUEUE] get_reaction_emoji returned: '{emoji}'")
            log_debug(f"[QUEUE] About to check emoji: '{emoji}' (bool: {bool(emoji)})")
            if emoji:
                log_debug("[QUEUE] About to get interface registry")
                interface = INTERFACE_REGISTRY.get(interface_id)
                log_debug(f"[QUEUE] Interface for {interface_id}: {interface}")
                log_debug(f"[QUEUE] Interface type: {type(interface)}")
                log_debug(f"[QUEUE] original_message is None: {original_message is None}")
                if interface:
                    log_debug(f"[QUEUE] Adding reaction '{emoji}' via interface {interface_id}")
                    await react_when_mentioned(interface, original_message or message, emoji)
                else:
                    log_warning(f"[QUEUE] No interface found for {interface_id}")
            else:
                log_debug("[QUEUE] No reaction emoji configured")
        except Exception as e:
            log_error(f"[QUEUE] Error adding reaction: {e}")
            log_debug(f"[QUEUE] Reaction traceback: {traceback.format_exc()}")
    else:
        log_debug(f"[QUEUE] DEBUG: skip_mention_check=True - bypassing is_message_for_bot check (1:1 interface)")
    
    # Check if user is blocked (but allow trainers)
    user_id = message.from_user.id if message.from_user else 0
    registry = get_interface_registry()
    is_trainer = registry.is_trainer(interface_id, user_id) if interface_id else False
    
    log_debug(f"[QUEUE] DEBUG: Checking blocklist - user_id={user_id}, interface_id='{interface_id}', is_trainer={is_trainer}")
    
    if not is_trainer and await is_user_blocked(user_id):
        log_debug(f"[QUEUE] DEBUG: User {user_id} is blocked - ignoring message")
        return
    
    log_debug(f"[QUEUE] DEBUG: User {user_id} is not blocked or is trainer, continuing processing")
    
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
        and not rate_limit.is_allowed(
            llm_name, user_id, interface_id or "unknown", max_messages, window_seconds, trainer_fraction, consume=False
        )
    ):
        log_debug(f"[RATE LIMIT] Delaying user {user_id} by {delay} seconds (quota exceeded)")
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

    log_debug(f"[QUEUE] Rate limit check passed - continuing to enqueue message")

    meta = message.chat.title or message.chat.username or message.chat.first_name
    # Persist last-active chat, but don't let DB failures abort enqueueing
    try:
        await recent_chats.track_chat(chat_id, meta)
    except Exception as e:
        log_warning(f"[QUEUE] recent_chats.track_chat failed but continuing: {e}")

    # Extract thread_id - unified field name, check both Telegram and generic names
    # DEBUG: let's see what telegram message actually contains
    thread_attrs = [attr for attr in dir(message) if 'thread' in attr.lower()]
    log_debug(f"[QUEUE] Available thread attributes in message: {thread_attrs}")
    
    # Use only thread_id, message_thread_id is legacy and deprecated
    thread_id_val = getattr(message, "thread_id", None)
    log_debug(f"[QUEUE] message.thread_id = {thread_id_val}")
    
    thread_id = thread_id_val
    interface = interface_id if interface_id else (
        bot.get_interface_id()
        if bot and hasattr(bot, "get_interface_id")
        else bot.__class__.__name__ if bot else None
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
                log_debug(f"[QUEUE] Resolved names: chat='{chat_name}', thread='{message_thread_name}'")
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

    priority_val = HIGH_PRIORITY if priority else NORMAL_PRIORITY
    await _queue.put((priority_val, item))
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


async def compact_similar_messages(first: dict, limit: int = 5) -> list:
    """Collect already-queued messages from same chat/thread/interface."""
    batch = [first]
    chat_id = first["chat_id"]
    thread_id = first.get("thread_id")
    interface = first.get("interface")
    ts = first["timestamp"]

    queue_items = list(_queue._queue)
    for prio, item in queue_items:
        if len(batch) >= limit:
            break
        if (
            item["chat_id"] == chat_id
            and item.get("thread_id") == thread_id
            and item.get("interface") == interface
            and item["timestamp"] - ts <= 600
        ):
            _queue._queue.remove((prio, item))
            batch.append(item)

    batch.sort(key=lambda x: x["timestamp"])

    if len(batch) > 1:
        log_debug(f"[COMPACT] Compacted {len(batch)} messages from chat {chat_id}")

    return batch


async def _consumer_loop() -> None:
    """Continuously process queued messages one at a time."""
    log_info("[QUEUE] Consumer loop started")
    while True:
        try:
            priority, item = await _queue.get()
            log_debug(
                f"[QUEUE] Dequeued message from chat {item.get('chat_id')} (priority={priority})"
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
                            if getattr(user, "username", None):
                                name = f"@{user.username}"
                            elif getattr(user, "full_name", None):
                                name = user.full_name
                            else:
                                name = f"user_{getattr(user, 'id', 'unknown')}"
                            lines.append(f"{name}: {msg.text}")
                        else:
                            lines.append(msg.text)
                    base = final["message"]
                    merged = SimpleNamespace(
                        chat_id=getattr(base, "chat_id", None),
                        message_id=getattr(base, "message_id", None),
                        text="\n".join(lines),
                        from_user=SimpleNamespace(id=0, username="group", full_name="group"),
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
                        message_thread_name=message_thread_name
                    )
                    log_debug(f"[QUEUE] Updated chat record with names: chat='{chat_name}', thread='{message_thread_name}'")
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
            interface_id = getattr(user_msg, 'interface_id', 'unknown')
            is_trainer = registry.is_trainer(interface_id, user_id)

            if (
                not is_trainer
                and not rate_limit.is_allowed(
                    llm_name, user_id, interface_id, max_messages, window_seconds, trainer_fraction, consume=True
                )
            ):
                delay = 300
                log_debug(
                    f"[RATE LIMIT] Delaying user {user_id} by {delay} seconds (quota exceeded)"
                )
                asyncio.create_task(_delayed_put(final, delay))
                continue

            try:
                # Check if this is an event prompt
                if "event_prompt" in final:
                    # Create a mock message object with event_id for events
                    mock_message = SimpleNamespace()
                    mock_message.event_id = final["context"].get("event_id")
                    mock_message.chat_id = "TARDIS/system/events"  
                    mock_message.message_id = f"event_{mock_message.event_id}"
                    
                    # Deliver the structured event prompt using the standard pipeline
                    await plugin_instance.handle_incoming_message(
                        final["bot"], mock_message, final["event_prompt"], final.get("interface")
                    )
                else:
                    await plugin_instance.handle_incoming_message(
                        final["bot"], final["message"], final["context"], final.get("interface")
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
                    log_warning(
                        f"[QUEUE] Failed to send fallback message: {send_err}"
                    )
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
        log_error("[QUEUE] Invalid event payload: missing 'description' in input.payload")
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
    for prio, queued_item in list(_queue._queue):
        if queued_item.get("event_prompt") == prompt_data:
            log_warning("[QUEUE] Duplicate event detected, not added to the queue")
            return

    await _queue.put((HIGH_PRIORITY, item))
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

