import asyncio
from dataclasses import dataclass
import inspect
import time
import queue as _thread_queue
import heapq
import re
from datetime import datetime
from typing import Any, cast
import traceback
from types import SimpleNamespace

from core import plugin_instance, rate_limit
from core.beat_utils import is_outbound_beat
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
from core.interface_paths import get_name_resolver
from core.user_utils import ensure_message_user_fields

# Use a priority queue so events can be processed before regular messages.
#
# Priority scale: a plain 0–11 numeric axis where HIGHER = MORE URGENT.
# The names are deliberately generic (broad-scope) — the *meaning* lives in the
# comments, not in narrow feature-bound names. Producers pick the closest band.
#
# NOTE ON HEAP ORDERING: the underlying ``asyncio.PriorityQueue`` is a min-heap
# (lowest key popped first). To make "higher = more urgent" work we push the
# NEGATED priority as the heap key via ``_heap_key`` — never push a raw priority
# value. The monotonic ``_counter`` stays the FIFO tie-break.
#
# NOTE ON VALUES: producers reference these constants **by name**, never by the
# raw integer, so the exact numbers are free to be re-spaced. REFLECTION was
# inserted between HIGH and URGENT (bumping EMERGENCY→11, URGENT→10) so a Vessel
# "stop and think about my goal" turn can jump ahead of ordinary in-world player
# chat yet still yield to a real-world emergency/safety notification.
# BACKGROUND(2) was split below LOW(3) so G.R.I.L.L.O. background beats sit at
# the absolute bottom and never contend with anything else.
PRIORITY_EMERGENCY = 11  # Reserved top band — a real emergency, above everything
PRIORITY_URGENT = 10  # Calendar reminders, auto_response(priority=True), safety
PRIORITY_REFLECTION = 9  # Vessel "pause & reflect on my goal" turn (above player chat)
PRIORITY_HIGH = 8  # Direct prioritised human input (e.g. in-world player chat)
PRIORITY_TRAINER = 7  # The trainer — must always reach Synth promptly
PRIORITY_RADIO = 6  # Radio-host DJ banter (on-the-fly LLM generation during playback)
PRIORITY_GENERAL = 5  # Ordinary user chat (telegram/discord/matrix/webui/ollama…)
PRIORITY_AMBIENT = 4  # Autonomous vessel perceptions / will-beats (below humans)
PRIORITY_LOW = 3  # Background-adjacent (web-search 2nd-pass, misc fallback)
PRIORITY_BACKGROUND = 2  # G.R.I.L.L.O. beats — absolute bottom, never starves anything

# Anything at or below this band is treated as background/cancellable by the
# consumer loop (run without blocking user-facing traffic, cancellable when a
# user message arrives for the same interface_path).
PRIORITY_BACKGROUND_THRESHOLD = PRIORITY_LOW


def _heap_key(priority_val: int) -> int:
    """Convert a semantic priority (higher = more urgent) to a min-heap key.

    The queue is a min-heap, so we negate: the most urgent (largest) semantic
    value becomes the smallest heap key and is popped first.
    """
    return -int(priority_val)


# Human-readable label for each priority band, keyed by the semantic value.
# Used only for display (e.g. the WebUI Activity → Queue tab); producers still
# reference the numeric constants by name. A value not in the map falls back to
# a plain ``str(priority)`` at the call site.
_PRIORITY_LABELS: dict[int, str] = {
    PRIORITY_EMERGENCY: "Emergency",
    PRIORITY_URGENT: "Urgent",
    PRIORITY_REFLECTION: "Reflection",
    PRIORITY_HIGH: "High",
    PRIORITY_TRAINER: "Trainer",
    PRIORITY_RADIO: "Radio",
    PRIORITY_GENERAL: "General",
    PRIORITY_AMBIENT: "Ambient",
    PRIORITY_LOW: "Low",
    PRIORITY_BACKGROUND: "Background",
}


def priority_label(priority_val: int) -> str:
    """Return a human-readable name for a semantic priority band."""
    try:
        return _PRIORITY_LABELS.get(int(priority_val), str(priority_val))
    except (TypeError, ValueError):
        return str(priority_val)


_queue: asyncio.PriorityQueue | None = None
_queue_loop: asyncio.AbstractEventLoop | None = None
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None
_consumer_task: asyncio.Task | None = None
_supervisor_task: asyncio.Task | None = None
_shutdown_requested: bool = False  # True only when a deliberate stop() was called
_counter = 0  # Monotonic counter to prevent dict comparison when priorities are equal

# Watchdog: how often the supervisor checks the consumer is still alive.
_SUPERVISOR_INTERVAL_SECONDS = 5.0


@dataclass(slots=True)
class _BackgroundTaskEntry:
    task: asyncio.Task
    cancel_on_user_message: bool = True


# Track running LOW_PRIORITY background tasks by interface_path so they can be
# cancelled when a higher-priority (user) message arrives for the same chat.
# This prevents duplicate responses when a grillo outreach beat and a user
# message target the same interface_path concurrently.
_bg_tasks: dict[str, _BackgroundTaskEntry] = {}

_GRILLO_ACTIVITY_MESSAGE_ID_RE = re.compile(r"^grillo_[a-z_]+_(\d+)$")


def _should_cancel_low_priority_on_user_message(context: object) -> bool:
    if not isinstance(context, dict):
        return True
    context_dict = cast(dict[str, object], context)
    return not (
        bool(context_dict.get("grillo_beat"))
        and is_outbound_beat(context_dict.get("beat_type"))
    )


def _extract_grillo_activity_log_id(message: object) -> int | None:
    """Recover a Grillo activity id from a synthetic message id when needed."""
    message_id = getattr(message, "message_id", None)
    if not isinstance(message_id, str):
        return None
    match = _GRILLO_ACTIVITY_MESSAGE_ID_RE.match(message_id.strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _get_queue() -> asyncio.PriorityQueue:
    global _queue, _queue_loop
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        if _queue is None:
            raise RuntimeError(
                "[QUEUE] Cannot create message queue without a running event loop"
            )
        return _queue

    if _queue is None:
        _queue = asyncio.PriorityQueue()
        _queue_loop = current_loop
        return _queue

    if _queue_loop is not current_loop:
        old_items = list(_queue._queue)
        log_warning(
            "[QUEUE] Existing queue is bound to a different event loop; "
            "recreating queue on the current loop"
        )
        _queue = asyncio.PriorityQueue()
        _queue_loop = current_loop
        if old_items:
            _queue._queue.extend(old_items)
            heapq.heapify(_queue._queue)

    return _queue


def _drop_stale_vessel_perceptions(world_chat_id: str) -> None:
    """Remove queued autonomous vessel perceptions for one world scope.

    Synth's own in-world perceptions (will beats, sightings) are produced on a
    fast timer but consumed slowly on a heavy engine, so they accumulate. When a
    real player speaks to Synth in-world, those pending autonomous beats are
    stale — a will beat is only meaningful *now* — and would otherwise force the
    player chat to wait one full slow turn per queued beat. We drop them so the
    player is answered promptly. Never touches the player chat itself (kept by
    the ``vessel_player_chat`` flag) nor any non-vessel traffic. Best-effort and
    fully guarded; a failure leaves the queue untouched.
    """
    if _queue is None:
        return
    heap = _queue._queue
    if not heap:
        return

    kept: list = []
    dropped = 0
    for entry in heap:
        try:
            _prio, _counter_val, entry_item = entry
        except (TypeError, ValueError):
            kept.append(entry)
            continue
        is_vessel = (
            isinstance(entry_item, dict) and entry_item.get("interface") == "vessel"
        )
        same_world = (
            isinstance(entry_item, dict) and entry_item.get("chat_id") == world_chat_id
        )
        is_player = isinstance(entry_item, dict) and entry_item.get(
            "vessel_player_chat"
        )
        # Drop only Synth's own autonomous perceptions for THIS world.
        if is_vessel and same_world and not is_player:
            dropped += 1
            continue
        kept.append(entry)

    if dropped:
        heap[:] = kept
        heapq.heapify(heap)
        # Keep the Queue's unfinished-task accounting consistent: each pruned
        # item was ``put`` (incrementing the counter) but will never be
        # ``get``/``task_done``. Guarded — internal attr may vary across
        # Python versions.
        try:
            for _ in range(dropped):
                if getattr(_queue, "_unfinished_tasks", 0) > 0:
                    _queue._unfinished_tasks -= 1
            if getattr(_queue, "_unfinished_tasks", 1) == 0:
                _queue._finished.set()
        except Exception:  # pragma: no cover - defensive
            pass
        log_debug(
            f"[QUEUE] Pruned {dropped} stale autonomous vessel perception(s) "
            f"for '{world_chat_id}' ahead of an in-world player chat"
        )


def _supersede_pending_vessel_beats(world_chat_id: str) -> None:
    """Drop older autonomous vessel perceptions superseded by a fresh one.

    Synth's own in-world will beats/perceptions are produced on a fast timer
    (``VESSEL_WILL_INTERVAL_SEC``) but each turn can take far longer to consume
    on a heavy engine (e.g. Selenium), so successive beats pile up behind the
    turn in flight. A will beat only means anything *now*: once a newer one is
    ready, the queued older ones are stale snapshots of a world that has since
    moved on. Left in the queue they would be coalesced together by
    :func:`compact_similar_messages` (same ``chat_id``) into a single turn
    carrying N identical "quiet moment to reflect" prompts, which makes the
    engine emit the *same* line N times in a row.

    So, right before a new autonomous perception for a world is enqueued, we
    drop the ones already queued for that same world. At most one autonomous
    beat is ever pending, so nothing gets coalesced and nothing is repeated.
    Purely structural (interface + world scope + the ``vessel_player_chat``
    flag) — never message text. Player chats and non-vessel traffic are never
    touched. Best-effort and fully guarded.
    """
    if _queue is None:
        return
    heap = _queue._queue
    if not heap:
        return

    kept: list = []
    dropped = 0
    for entry in heap:
        try:
            _prio, _counter_val, entry_item = entry
        except (TypeError, ValueError):
            kept.append(entry)
            continue
        is_vessel = (
            isinstance(entry_item, dict) and entry_item.get("interface") == "vessel"
        )
        same_world = (
            isinstance(entry_item, dict) and entry_item.get("chat_id") == world_chat_id
        )
        is_player = isinstance(entry_item, dict) and entry_item.get(
            "vessel_player_chat"
        )
        # Supersede only Synth's own autonomous perceptions for THIS world; a
        # ``no_compact`` beat is intentionally standalone and is left alone.
        is_no_compact = isinstance(entry_item, dict) and entry_item.get("no_compact")
        if is_vessel and same_world and not is_player and not is_no_compact:
            dropped += 1
            continue
        kept.append(entry)

    if dropped:
        heap[:] = kept
        heapq.heapify(heap)
        # Keep the Queue's unfinished-task accounting consistent (see
        # _drop_stale_vessel_perceptions). Guarded — internal attr may vary.
        try:
            for _ in range(dropped):
                if getattr(_queue, "_unfinished_tasks", 0) > 0:
                    _queue._unfinished_tasks -= 1
            if getattr(_queue, "_unfinished_tasks", 1) == 0:
                _queue._finished.set()
        except Exception:  # pragma: no cover - defensive
            pass
        log_debug(
            f"[QUEUE] Superseded {dropped} older autonomous vessel beat(s) for "
            f"'{world_chat_id}' with a fresher one"
        )


def drop_vessel_queue_for_world(world_chat_id: str) -> int:
    """Remove **every** queued item for a Vessel world when its session ends.

    Called at session teardown (logout / cooldown / connector disconnect). Once
    the session is closed, anything still queued for that ``vessel/<world>``
    scope — autonomous will beats, action beats, sightings, *and* any pending
    player chat — is stale: there is no live embodiment left to act on it, and
    leaving it would either be dispatched into a dead world or, worse, be
    coalesced into the next session's turns. So we drop the whole world scope.

    Unlike :func:`_drop_stale_vessel_perceptions` and
    :func:`_supersede_pending_vessel_beats`, this deliberately also removes the
    player chat (``vessel_player_chat``): the session is over, so those messages
    can no longer be answered in-world. Purely structural (interface + world
    scope) — never message text. Non-vessel traffic is never touched. Returns
    the number of items dropped. Best-effort and fully guarded; a failure leaves
    the queue untouched.
    """
    if _queue is None:
        return 0
    heap = _queue._queue
    if not heap:
        return 0

    kept: list = []
    dropped = 0
    for entry in heap:
        try:
            _prio, _counter_val, entry_item = entry
        except (TypeError, ValueError):
            kept.append(entry)
            continue
        is_vessel = (
            isinstance(entry_item, dict) and entry_item.get("interface") == "vessel"
        )
        # Match the exact world scope OR any deeper per-server scope beneath it
        # (``vessel/<game>`` also purges ``vessel/<game>/<world>``), so a purge
        # keyed on the game token covers the concrete per-server session scope.
        entry_chat = entry_item.get("chat_id") if isinstance(entry_item, dict) else None
        same_world = isinstance(entry_chat, str) and (
            entry_chat == world_chat_id or entry_chat.startswith(f"{world_chat_id}/")
        )
        if is_vessel and same_world:
            dropped += 1
            continue
        kept.append(entry)

    if dropped:
        heap[:] = kept
        heapq.heapify(heap)
        # Keep the Queue's unfinished-task accounting consistent (see
        # _drop_stale_vessel_perceptions). Guarded — internal attr may vary.
        try:
            for _ in range(dropped):
                if getattr(_queue, "_unfinished_tasks", 0) > 0:
                    _queue._unfinished_tasks -= 1
            if getattr(_queue, "_unfinished_tasks", 1) == 0:
                _queue._finished.set()
        except Exception:  # pragma: no cover - defensive
            pass
        log_debug(
            f"[QUEUE] Dropped {dropped} queued vessel item(s) for "
            f"'{world_chat_id}' at session teardown"
        )
    return dropped


def snapshot_queue(limit: int = 500) -> list[dict[str, object]]:
    """Return a read-only snapshot of the currently PENDING queue items.

    Powers the WebUI Activity → Queue tab. Each entry is a plain, JSON-safe dict
    describing one queued message — never the live ``item`` (which holds the bot,
    the raw message object and futures). Items are returned in the order they
    would be popped by the consumer: most urgent first (highest semantic
    priority), FIFO within a band (via the monotonic counter).

    The item currently being processed by the consumer has already been
    ``get``-ed off the heap and is therefore NOT included — this is only the
    pending backlog.

    Best-effort and fully guarded (mirrors the prune helpers): reads a copy of
    the internal heap without a lock, so a rare concurrent mutation can only
    yield a slightly stale snapshot, never an exception. Returns ``[]`` when the
    queue has not been created yet or on any error.

    Args:
        limit: Maximum number of items to return (defensive cap for huge
            backlogs). Clamped to ``[1, 5000]``.

    Returns:
        A list of dicts, each with: ``position`` (1-based pop order),
        ``priority`` (int), ``priority_label`` (str), ``interface``,
        ``interface_path``, ``chat_id``, ``chat_name``, ``thread_name``,
        ``enqueued_at`` (epoch seconds, float), ``age_seconds`` (float),
        ``text_preview`` (str, truncated), ``is_priority`` (bool) and the
        structural vessel flags ``vessel_player_chat`` / ``vessel_reflection`` /
        ``vessel_appraisal``.
    """
    try:
        cap = max(1, min(5000, int(limit)))
    except (TypeError, ValueError):
        cap = 500

    if _queue is None:
        return []

    try:
        # Copy the heap before iterating so a concurrent enqueue/prune cannot
        # mutate it mid-read. Each entry is ``(heap_key, counter, item)``.
        entries = list(_queue._queue)
    except Exception:  # pragma: no cover - defensive
        return []

    # Sort into the exact pop order: min-heap key first (most urgent), then the
    # monotonic counter (FIFO tie-break).
    def _sort_key(entry: object) -> tuple[int, int]:
        try:
            heap_key, counter_val, _item = entry  # type: ignore[misc]
            return (int(heap_key), int(counter_val))
        except (TypeError, ValueError):
            return (0, 0)

    try:
        entries.sort(key=_sort_key)
    except Exception:  # pragma: no cover - defensive
        pass

    now = time.time()
    out: list[dict[str, object]] = []
    for position, entry in enumerate(entries[:cap], start=1):
        try:
            heap_key, _counter_val, item = entry
        except (TypeError, ValueError):
            continue
        if not isinstance(item, dict):
            continue

        priority_val = -int(heap_key)

        # Text preview from the wrapped message object, truncated. Guarded — the
        # message may be any interface's object; we only want its ``.text``.
        preview = ""
        try:
            raw_text = getattr(item.get("message"), "text", "") or ""
            preview = str(raw_text).strip().replace("\n", " ")
            if len(preview) > 240:
                preview = preview[:240] + "…"
        except Exception:  # pragma: no cover - defensive
            preview = ""

        enqueued_at = item.get("timestamp")
        try:
            enqueued_at_f = float(enqueued_at) if enqueued_at is not None else None
        except (TypeError, ValueError):
            enqueued_at_f = None
        age_seconds = (now - enqueued_at_f) if enqueued_at_f is not None else None

        out.append(
            {
                "position": position,
                "priority": priority_val,
                "priority_label": priority_label(priority_val),
                "interface": item.get("interface"),
                "interface_path": item.get("interface_path"),
                "chat_id": item.get("chat_id"),
                "chat_name": item.get("chat_name"),
                "thread_name": item.get("message_thread_name"),
                "enqueued_at": enqueued_at_f,
                "age_seconds": age_seconds,
                "text_preview": preview,
                "is_priority": bool(item.get("priority")),
                "vessel_player_chat": bool(item.get("vessel_player_chat")),
                "vessel_reflection": bool(item.get("vessel_reflection")),
                "vessel_appraisal": bool(item.get("vessel_appraisal")),
            }
        )

    return out


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        if _lock is None:
            raise RuntimeError(
                "[QUEUE] Cannot create lock without a running event loop"
            )
        return _lock

    if _lock is None:
        _lock = asyncio.Lock()
        _lock_loop = current_loop
        return _lock

    if _lock_loop is not current_loop:
        log_warning(
            "[QUEUE] Existing lock is bound to a different event loop; "
            "recreating lock on the current loop"
        )
        _lock = asyncio.Lock()
        _lock_loop = current_loop

    return _lock


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
    priority = PRIORITY_URGENT if item.get("priority") else PRIORITY_GENERAL
    _counter += 1
    await _get_queue().put((_heap_key(priority), _counter, item))


async def enqueue(
    bot,
    message,
    context_memory=None,
    history_scope: str | None = None,
    priority: bool = False,
    interface_id: str = None,
    skip_mention_check: bool = False,
    original_message=None,
    response_future: asyncio.Future | None = None,
    media_future: asyncio.Future | None = None,
) -> None:
    """Enqueue a message for serialized processing with rate limiting.

    Args:
        bot: The bot instance
        message: The message to process
        context_memory: (Deprecated) Message context dict. If not provided, uses centralized context manager.
        history_scope: Optional per-message history scope ("local"|"recent"|"unified"). If None, falls back to global `UNIFIED_HISTORY` behavior.
        priority: If True, message is added to front of queue (for events)
        interface_id: The interface identifier (e.g., 'webui', 'interface_name')
        skip_mention_check: If True, skip is_message_for_bot check (for 1:1 interfaces like ollama, webui)
        original_message: The original message object from the interface (for reactions)
        response_future: Optional Future that will be completed with the processing result or exception.
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

    # Default to 'local' history scope for interface-originated messages unless
    # an explicit history_scope was provided by the caller. This keeps the
    # behaviour implicit for interfaces and avoids touching all call sites.
    try:
        if history_scope is None and (
            interface_id or (bot and hasattr(bot, "get_interface_id"))
        ):
            history_scope = "local"
            log_debug(
                f"[QUEUE] Defaulted history_scope to 'local' for interface {interface_id or getattr(bot, 'get_interface_id', lambda: None)()}"
            )
    except Exception:
        pass

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

        # === CENTRALIZED ATTENTION (wake/sleep) HANDLING ===
        try:
            # Lazy import to avoid cycles
            from core.chat_attention import get_attention

            chat_scope = getattr(message, "chat_id", None)
            is_awake = get_attention(chat_scope, True)
            explicit_trigger = getattr(message, "is_explicit_trigger", False)

            if is_awake:
                if not directed:
                    # When a chat is awake, do NOT automatically treat every message as
                    # directed to the bot. Only mark as directed if the message has an
                    # explicit trigger (e.g., @mention, reply to bot, DM or the interface
                    # has explicitly flagged this message as an explicit trigger).
                    if getattr(message, "is_explicit_trigger", False):
                        directed = True
                        reason = "explicit_trigger_awake"
                # If already directed, leave intact.
            else:
                if directed and not explicit_trigger:
                    directed = False
                    reason = "asleep_state_no_trigger"
                    log_debug(
                        f"[QUEUE] Suppressed message due to Asleep state: {getattr(message, 'text', '')}"
                    )
                elif not directed and explicit_trigger:
                    directed = True
                    reason = "explicit_trigger_asleep"
        except Exception:
            # If anything goes wrong, fall back to original directed decision
            pass

        # Refresh the alias-triggered attention window (no-op unless
        # CHAT_ATTENTION_WINDOW_SECONDS > 0). Deliberately excludes private
        # chats (irrelevant there) and bot senders (peer SyntH messages must
        # never seed or extend this chat's attention window).
        if directed:
            try:
                from core.chat_attention import mark_engaged

                sender = getattr(message, "from_user", None)
                sender_is_bot = bool(getattr(sender, "is_bot", False))
                chat_type = (
                    getattr(message.chat, "type", None)
                    if hasattr(message, "chat")
                    else None
                )
                if not sender_is_bot and chat_type != "private":
                    mark_engaged(getattr(message, "chat_id", None))
            except Exception:
                pass

        if not directed:
            log_debug("[QUEUE] DEBUG: Message not directed to bot - ignoring")
            if reason == "missing_human_count":
                log_debug("[QUEUE] DEBUG: Reason: missing_human_count")
            elif reason == "multiple_humans":
                log_debug("[QUEUE] DEBUG: Reason: multiple_humans")
            else:
                log_debug(f"[QUEUE] DEBUG: Reason: {reason or 'not directed to bot'}")
            if response_future is not None and not response_future.done():
                response_future.set_result(None)
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
        if response_future is not None and not response_future.done():
            response_future.set_result(None)
        return

    log_debug(
        f"[QUEUE] DEBUG: User {user_id} is not blocked or is trainer, continuing processing"
    )

    plugin = plugin_instance.get_plugin()
    if not plugin:
        log_error("[QUEUE] No active plugin")
        if response_future is not None and not response_future.done():
            response_future.set_result(None)
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
        delay = 300
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

    # Last-active tracking is handled centrally by
    # ``core.interface_paths.touch_interface_path`` from the chat-context and
    # outbound message paths, so no explicit per-enqueue tracking is needed.

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
        resolver = get_name_resolver(interface)
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
        "interface_path": getattr(message, "interface_path", None),
        "chat_name": chat_name,
        "message_thread_name": message_thread_name,
        "timestamp": time.time(),
        "context": context_memory,
        "priority": priority,
        "history_scope": history_scope,
        "response_future": response_future,
        "media_future": media_future,
        # Opt-out of queue coalescing. A message flagged ``_no_compact`` (e.g. a
        # salient in-world player chat directly addressing Synth) must run as its
        # own turn — never merged with autonomous perceptions/will-beat prompts
        # that share the same ``chat_id`` — so its text stays the primary
        # ``original_user_message`` and gets a direct reply. Structural flag set
        # by the originating interface; defaults to False for every other path.
        "no_compact": bool(getattr(message, "_no_compact", False)),
        # Structural marker: a real in-world player chat directly addressing
        # Synth (set by the vessel interface). Used below to rank it above
        # Synth's own autonomous vessel perceptions and to prune stale ones.
        "vessel_player_chat": bool(getattr(message, "_vessel_player_chat", False)),
        # Structural marker: a Vessel "pause & reflect on my goal" turn (set by
        # the vessel interface when Synth stops to author/expand its goal). Used
        # below to rank it at PRIORITY_REFLECTION — ahead of ordinary in-world
        # player chat but below any real emergency/urgent notification — and to
        # prune stale autonomous beats so the reflection turn runs unobstructed.
        "vessel_reflection": bool(getattr(message, "_vessel_reflection", False)),
        # Structural marker: a Vessel post-damage appraisal turn (set by the
        # vessel interface right after Synth took damage). Ranked at
        # PRIORITY_URGENT so the "I was just hurt — what do I do?" cognition turn
        # jumps ahead of ordinary autonomous play and in-world chat; the fast
        # survival reflex already reacted mechanically, this is the deliberate
        # combat/social appraisal on top. Prunes stale autonomous beats too.
        "vessel_appraisal": bool(getattr(message, "_vessel_appraisal", False)),
    }

    global _counter

    # === Priority assignment (numeric 0–10 scale, higher = more urgent) ===
    # A pure, unconditional ranking based only on the message's structural
    # origin — never on message text (project rule: no keyword/trigger-word
    # logic) and never conditional on whether a Vessel session is active. There
    # is NO de-prioritisation: ordinary chat is never demoted because Synth
    # happens to be embodied in a world. The bands, from most to least urgent:
    #   * priority=True (scheduled events / urgent notifications) → URGENT.
    #   * A real in-world player chat (structural ``vessel_player_chat`` flag,
    #     set by the vessel interface from event kind + actor) → HIGH: it is a
    #     human speaking directly, so it ranks above ordinary chat and above
    #     Synth's own autonomous perceptions.
    #   * The trainer (TRAINER_CHAT_ID) → TRAINER: always reaches Synth promptly.
    #   * Synth's own autonomous vessel perceptions/will-beats → AMBIENT: below
    #     every human, so a person is always answered before background play.
    #   * Everything else (ordinary user chat) → GENERAL.
    # Fully guarded + lazily imported so removing the Vessel plugin, or any
    # failure, leaves enqueue behaviour unchanged.
    if priority:
        priority_val = PRIORITY_URGENT
    elif interface == "vessel" and item.get("vessel_appraisal"):
        # Synth just took damage. The fast survival reflex already reacted; this
        # is the deliberate appraisal turn ("I was hurt — fight smart, disengage,
        # or respond socially if a person struck me?"). Rank it URGENT so it is
        # consumed ahead of ordinary autonomous play and in-world chat, and prune
        # the older autonomous beats for this world so it runs unobstructed
        # (structural + world scope, guarded; player chats and ``no_compact``
        # items are preserved).
        priority_val = PRIORITY_URGENT
        try:
            _supersede_pending_vessel_beats(chat_id)
        except Exception as _apr_exc:  # pragma: no cover - defensive
            log_debug(f"[QUEUE] Vessel appraisal prune skipped: {_apr_exc}")
    elif interface == "vessel" and item.get("vessel_reflection"):
        # Synth deliberately stopped to think about its goal. This ranks ABOVE
        # ordinary in-world player chat (HIGH) yet below any urgent/emergency
        # notification, so the reflection turn is the next thing consumed. Prune
        # the older autonomous beats already queued for this world so the
        # reflection turn is not coalesced with — or delayed behind — stale
        # will/action beats (structural + world scope, guarded; player chats and
        # ``no_compact`` items are preserved).
        priority_val = PRIORITY_REFLECTION
        try:
            _supersede_pending_vessel_beats(chat_id)
        except Exception as _ref_exc:  # pragma: no cover - defensive
            log_debug(f"[QUEUE] Vessel reflection prune skipped: {_ref_exc}")
    elif interface == "vessel" and item.get("vessel_player_chat"):
        # A human speaking in-world. Prune stale autonomous perceptions for the
        # same world so the player is answered promptly (structural, guarded).
        priority_val = PRIORITY_HIGH
        try:
            _drop_stale_vessel_perceptions(chat_id)
        except Exception as _prune_exc:  # pragma: no cover - defensive
            log_debug(f"[QUEUE] Vessel perception prune skipped: {_prune_exc}")
    elif interface == "vessel":
        # Synth's own autonomous perception/will-beat: ranked BELOW any human
        # chat. A fresh beat supersedes older queued ones for the same world so
        # they are not coalesced into one turn with N identical prompts.
        priority_val = PRIORITY_AMBIENT
        try:
            _supersede_pending_vessel_beats(chat_id)
        except Exception as _sup_exc:  # pragma: no cover - defensive
            log_debug(f"[QUEUE] Vessel beat supersede skipped: {_sup_exc}")
    else:
        priority_val = PRIORITY_GENERAL
        try:
            from core.config import config_registry

            trainer_path = str(
                config_registry.get_value("TRAINER_CHAT_ID", "") or ""
            ).strip()
            msg_path = getattr(message, "interface_path", None) or ""
            if trainer_path and str(msg_path) == trainer_path:
                priority_val = PRIORITY_TRAINER
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[QUEUE] Trainer priority lookup skipped: {exc}")

    _counter += 1
    await _get_queue().put((_heap_key(priority_val), _counter, item))
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


async def enqueue_and_wait(
    bot,
    message,
    context_memory=None,
    history_scope: str | None = None,
    priority: bool = False,
    interface_id: str = None,
    skip_mention_check: bool = False,
    original_message=None,
    timeout: float | None = None,
):
    """Enqueue a message and wait for its processing result.

    This is useful for HTTP-like interfaces that need an immediate response
    from the core pipeline instead of purely asynchronous queue delivery.
    """
    loop = asyncio.get_running_loop()
    response_future = loop.create_future()
    await enqueue(
        bot=bot,
        message=message,
        context_memory=context_memory,
        history_scope=history_scope,
        priority=priority,
        interface_id=interface_id,
        skip_mention_check=skip_mention_check,
        original_message=original_message,
        response_future=response_future,
    )
    if timeout is not None:
        return await asyncio.wait_for(response_future, timeout=timeout)
    return await response_future


async def enqueue_low_priority(
    bot,
    message,
    context_memory=None,
    history_scope: str | None = None,
    interface_id: str = None,
    original_message=None,
    priority: int = PRIORITY_LOW,
) -> None:
    """Enqueue a low/background-priority message into the global queue.

    This is a convenience wrapper for plugins that want to submit background
    messages at a specific priority band. By default items are pushed at
    PRIORITY_LOW; callers that need a different band (e.g. radio-host banter at
    PRIORITY_RADIO, G.R.I.L.L.O. beats at PRIORITY_BACKGROUND, or calendar
    reminders at PRIORITY_URGENT) pass an explicit ``priority``.

    Items at or below PRIORITY_BACKGROUND_THRESHOLD are treated as non-blocking
    background work by the consumer loop; items above it are awaited normally
    and will block the consumer until they complete.

    Args:
        history_scope: Optional per-message history scope propagated to the consumer/prompt builder.
        priority: Semantic priority band (default PRIORITY_LOW).  Use the named
                  ``PRIORITY_*`` constants.
    """
    if context_memory is None:
        context_memory = get_context_memory()

    # Ensure message fields exist before queueing
    try:
        ensure_message_user_fields(message)
    except Exception:
        log_debug("[QUEUE] Failed to normalise message.user for enqueue_low_priority")

    # Default history_scope for interface-originated low-priority messages as well
    try:
        if history_scope is None and interface_id:
            history_scope = "local"
            log_debug(
                f"[QUEUE] Defaulted history_scope to 'local' for low-priority interface {interface_id}"
            )
    except Exception:
        pass

    chat_id = getattr(message, "chat_id", "unknown")
    thread_id = getattr(message, "thread_id", None) or getattr(
        message, "message_thread_id", None
    )

    # Resolve chat name if possible - best effort
    chat_name = None
    message_thread_name = None
    try:
        resolver = get_name_resolver(interface_id)
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
        "history_scope": history_scope,
    }

    global _counter
    _counter += 1
    await _get_queue().put((_heap_key(priority), _counter, item))
    log_debug(
        f"[QUEUE] enqueue_low_priority: {priority_label(priority)} "
        f"from {item['interface']} chat {chat_id} thread {thread_id}"
    )


async def compact_similar_messages(first: dict, limit: int = 5) -> list:
    """Collect already-queued messages from same chat/thread/interface."""
    batch = [first]
    chat_id = first["chat_id"]
    thread_id = first.get("thread_id")
    interface = first.get("interface")
    ts = first["timestamp"]

    # A ``no_compact`` base must never absorb other queued messages: it is a
    # direct address (e.g. an in-world player chat mentioning Synth) that has to
    # run as its own turn so its text remains the primary user message and earns
    # a reply — instead of being blended into a pile of autonomous perceptions
    # (sightings / will-beat prompts) that share the same ``chat_id``.
    if first.get("no_compact"):
        return batch

    seen_ids = set()
    first_msg = first.get("message")
    if first_msg:
        mid = getattr(first_msg, "message_id", None)
        if mid:
            seen_ids.add(mid)

    queue_items = list(_get_queue()._queue)
    dirty = False
    for item_tuple in queue_items:
        if len(batch) >= limit:
            break
        if len(item_tuple) == 3:
            prio, counter, item = item_tuple
        else:
            prio, item = item_tuple
        # Never absorb a ``no_compact`` item into another base — it must run as
        # its own standalone turn (a direct address that needs its own reply).
        if item.get("no_compact"):
            continue
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
                        _get_queue()._queue.remove(item_tuple)
                        dirty = True
                    except ValueError:
                        pass
                    log_debug(f"[COMPACT] Removed duplicate message {mid} from queue")
                    continue
                if mid:
                    seen_ids.add(mid)
            try:
                _get_queue()._queue.remove(item_tuple)
                dirty = True
                batch.append(item)
            except ValueError:
                pass

    if dirty:
        heapq.heapify(_get_queue()._queue)

    batch.sort(key=lambda x: x["timestamp"])

    if len(batch) > 1:
        log_debug(f"[COMPACT] Compacted {len(batch)} messages from chat {chat_id}")

    return batch


async def _consumer_loop() -> None:
    """Continuously process queued messages one at a time."""
    log_info("[QUEUE] Consumer loop started")
    while True:
        try:
            heap_key, counter, item = await _get_queue().get()
            # Recover the semantic priority (higher = more urgent) from the
            # negated min-heap key pushed by every enqueue path via _heap_key.
            priority = -int(heap_key)
            log_debug(
                f"[QUEUE] Dequeued message from chat {item.get('chat_id')} (priority={priority}, counter={counter})"
            )

            # If this item carries a media_future, media processing (Auris/Iris/download)
            # is still in progress in handle_media_live. Block here until it resolves so
            # the consumer slot stays occupied by this user-facing item and no
            # background (Grillo) item can be extracted and started concurrently.
            _media_future: asyncio.Future | None = item.get("media_future")
            if _media_future is not None:
                try:
                    _resolved_msg = await asyncio.wait_for(_media_future, timeout=120.0)
                    item["message"] = _resolved_msg
                except asyncio.TimeoutError:
                    log_warning(
                        f"[QUEUE] media_future timed out (120 s) for chat {item.get('chat_id')}; discarding item"
                    )
                    continue
                except asyncio.CancelledError:
                    log_warning(
                        f"[QUEUE] media_future was cancelled for chat {item.get('chat_id')}; discarding item"
                    )
                    continue
                except Exception as _mf_exc:
                    log_warning(
                        f"[QUEUE] media_future raised {_mf_exc!r} for chat {item.get('chat_id')}; discarding item"
                    )
                    continue

            async with _get_lock():
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

            plugin = plugin_instance.get_plugin()
            if not plugin:
                # For grillo internal beats, attempt a one-time auto-load from config
                # before giving up — beats fire on a timer and must not be silently dropped.
                _is_grillo = final.get("interface") == "grillo" or (
                    isinstance(final.get("context"), dict)
                    and final["context"].get("grillo_beat")
                )
                if _is_grillo:
                    try:
                        from core.config import get_active_cortex_engine as _gace

                        _engine_name = await _gace(scope="base")
                        await plugin_instance.load_plugin(
                            _engine_name, ensure_started=True
                        )
                        plugin = plugin_instance.get_plugin()
                        if plugin:
                            log_info(
                                f"[QUEUE] Auto-loaded engine '{_engine_name}' for grillo beat"
                            )
                    except Exception as _e:
                        log_warning(f"[QUEUE] Auto-load for grillo beat failed: {_e}")
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

            # Check if user is trainer for this interface.
            # NOTE: interface_id lives on the queue item (final["interface"]),
            # NOT on the message object itself.
            registry = get_interface_registry()
            interface_id = final.get("interface") or getattr(
                user_msg, "interface_id", "unknown"
            )
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

                timeout_seconds = (
                    int(RESPONSE_TIMEOUT) if RESPONSE_TIMEOUT else 300
                )  # default bump from 240 to 300s

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
                            # derive interface_path from explicit field or bot class name
                            interface_path = final.get("interface") or (
                                final.get("bot").__class__.__name__
                                if final.get("bot")
                                else "unknown"
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

                    interface_path = final.get("interface_path") or getattr(
                        final.get("message"), "interface_path", None
                    )
                    if not interface_path:
                        interface_path = build_interface_path(
                            interface_id,
                            str(chat_id),
                            str(thread_id) if thread_id else None,
                        )

                    # Add interface_path to context dict so prompt_engine can access it
                    context = final.get("context", {})
                    if isinstance(context, dict):
                        if not context.get("activity_log_id"):
                            recovered_activity_log_id = _extract_grillo_activity_log_id(
                                final.get("message")
                            )
                            if recovered_activity_log_id is not None:
                                context["activity_log_id"] = recovered_activity_log_id
                                context["grillo_activity_log_id"] = (
                                    recovered_activity_log_id
                                )
                        context["interface_path"] = interface_path
                        context["thread_id"] = thread_id
                        # Propagate trainer flag so plugin_instance can route
                        # to TRAINER_CORTEX when a scope override is configured.
                        context["is_trainer"] = is_trainer
                        # Propagate voice input flag from interface (used by message_chain TTS auto-inject)
                        # We explicitly write *both* True and False so that stale values
                        # from a previous message do not linger.  Previously we only
                        # set the flag when it was True which meant a voice message
                        # could taint the context for all subsequent text replies,
                        # leading the bot to auto-send audio even when the new
                        # incoming message was plain text.  See issue described by
                        # user: "se il messaggio entrante non è un audio il synth
                        # non dovrebbe rispondere con un audio".  Clearing the key
                        # prevents unexpected audio responses.
                        _queued_msg = final.get("message")
                        if getattr(_queued_msg, "is_voice_input", False):
                            context["is_voice_input"] = True
                            # For voice-originated input there is no textual
                            # "writing" phase — the reply is spoken. Keep the
                            # avatar in THINK during generation instead of
                            # switching to WRITE (transcription is not a write).
                            context.setdefault(
                                "animation_state_on_generation_start", "think"
                            )
                        else:
                            # remove stale flag if present
                            context.pop("is_voice_input", None)
                            context.pop("animation_state_on_generation_start", None)

                        # Propagate explicit request_tts flag (e.g. from handle_media_live wrap)
                        if getattr(_queued_msg, "request_tts", False):
                            context["request_tts"] = True
                        else:
                            context.pop("request_tts", None)

                        # Propagate voice channel presence so the prompt engine can
                        # inform the LLM that the sender is in a voice channel — this
                        # lets the model decide to issue join_voice_discord.
                        _vc_id = getattr(_queued_msg, "voice_channel_id", None)
                        if _vc_id:
                            context["voice_channel_id"] = str(_vc_id)
                        else:
                            context.pop("voice_channel_id", None)

                        # Propagate the structural in-world player-chat marker so
                        # message_chain can treat a reactive vessel player turn as
                        # user-facing (and demand a reply) while leaving Synth's own
                        # autonomous vessel perceptions/will-beats unaffected. Set by
                        # the vessel interface from event kind + actor presence, never
                        # from message text. Written both True and False so a stale
                        # value from a previous turn never lingers on the context.
                        if getattr(_queued_msg, "_vessel_player_chat", False):
                            context["vessel_player_chat"] = True
                        else:
                            context.pop("vessel_player_chat", None)

                        # Propagate trainer flag so plugin_instance can route
                        # to TRAINER_CORTEX when a scope override is configured.
                        if is_trainer:
                            context["is_trainer"] = True

                        # Propagate per-message history_scope when present so prompt_engine/history_engine can honour it
                        hs = final.get("history_scope")
                        if hs is not None:
                            context["history_scope"] = hs
                            log_debug(
                                f"[QUEUE] Propagated history_scope into context: {hs}"
                            )

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

                    # NOTE: closures below that can run *deferred* (via
                    # add_done_callback after the loop has moved on) bind the
                    # current item's values as keyword defaults — loop variables
                    # share one cell across iterations, so reading them late
                    # would target the wrong queue item (same trick as
                    # _bg_done_cb further down).
                    def _resolve_generation_animation_state(
                        event: str,
                        *,
                        _final: dict = final,
                        _interface_path: str = interface_path,
                    ) -> str:
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

                        context_obj = _final.get("context")
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
                            iface_id = _final.get("interface")
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
                                        interface_path=_interface_path,
                                        context=context_obj,
                                        message=_final.get("message"),
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
                                        interface_path=_interface_path,
                                        context=context_obj,
                                        message=_final.get("message"),
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

                    async def _broadcast_global_animation_state(
                        state: str, *, _final: dict = final
                    ) -> None:
                        """Best-effort global animation state update (broadcast to all WebUI clients).

                        Some interfaces (e.g. Telegram) pass a raw Bot object which does not
                        implement generation hooks; this keeps the THINK → WRITE → IDLE flow
                        consistent with `plan-animationHandlerVrm.prompt.md`.
                        """
                        # Allow interfaces to disable core broadcast if they manage it themselves.
                        context_obj = _final.get("context")
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

                    async def _call_bot_generation_end(
                        task: asyncio.Task,
                        *,
                        _final: dict = final,
                        _interface_path: str = interface_path,
                        _context: Any = context,
                    ) -> None:
                        try:
                            bot_obj = _final.get("bot")
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
                                    interface_path=_interface_path,
                                    success=success,
                                    context=_context,
                                    message=_final.get("message"),
                                )
                            else:
                                fn(
                                    interface_path=_interface_path,
                                    success=success,
                                    context=_context,
                                    message=_final.get("message"),
                                )
                        except Exception as hook_exc:
                            log_debug(
                                f"[QUEUE] on_generation_end hook failed for {_interface_path}: {hook_exc}"
                            )

                    try:
                        # Cancel any running LOW_PRIORITY background task for the
                        # same interface_path IMMEDIATELY — before any event-loop
                        # yields — so the Grillo task cannot make further progress
                        # (e.g. write to chat history) between now and when we
                        # actually start processing the user message.
                        _existing_bg = _bg_tasks.get(interface_path)
                        if _existing_bg is not None and _existing_bg.task.done():
                            _bg_tasks.pop(interface_path, None)
                            _existing_bg = None
                        if (
                            _existing_bg is not None
                            and _existing_bg.cancel_on_user_message
                            and not _existing_bg.task.done()
                        ):
                            _bg_tasks.pop(interface_path, None)
                            _existing_bg.task.cancel()
                            log_info(
                                f"[QUEUE] Cancelled LOW_PRIORITY background task for {interface_path} "
                                f"(superseded by incoming user message)"
                            )

                        # Also cancel any Grillo-internal background beats (keyed as
                        # "grillo/…"). These run under a different interface_path but
                        # share the event loop and can interleave between Iris/Auris
                        # analysis and LLM prompt construction inside
                        # handle_incoming_message. Direct user requests always
                        # take priority over background Grillo beats.
                        for _gk in [
                            k for k in list(_bg_tasks) if k.startswith("grillo/")
                        ]:
                            _gt = _bg_tasks.get(_gk)
                            if _gt is not None and _gt.task.done():
                                _bg_tasks.pop(_gk, None)
                                _gt = None
                            if _gt is not None and not _gt.task.done():
                                _bg_tasks.pop(_gk, None)
                                _gt.task.cancel()
                                log_info(
                                    f"[QUEUE] Cancelled Grillo internal beat {_gk} "
                                    f"(user message arrived for {interface_path})"
                                )

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

                        # Selenium-based LLMs manage browser state and cannot be safely
                        # cancelled mid-flight. All other engines (HTTP-based Gemini, OpenAI, …)
                        # support asyncio cancellation and should be stopped on timeout so they
                        # don't deliver a "ghost" reply after the fallback has already been sent.
                        task_is_cancellable = getattr(
                            plugin_instance, "task_cancellable", True
                        )

                        # Run message processing in a Task so we can apply a per-element timeout.
                        processing_task = asyncio.create_task(
                            plugin_instance.handle_incoming_message(
                                final["bot"],
                                final["message"],
                                context,
                                final.get("interface"),
                            )
                        )
                        response_future = final.get("response_future")
                        log_debug(
                            f"[QUEUE] Dispatched handle_incoming_message task for interface_path={interface_path}, interface={final.get('interface')} cancellable={task_is_cancellable}"
                        )

                        # If this is a background item, do not await it - run in background
                        # so long-running background beats (e.g., G.R.I.L.L.O.) do not block
                        # processing of regular user messages. Threshold check on the
                        # semantic priority (any band at/below PRIORITY_LOW is background).
                        if priority <= PRIORITY_BACKGROUND_THRESHOLD:
                            log_debug(
                                f"[QUEUE] Low-priority task scheduled as background for interface_path={interface_path}; not awaiting"
                            )

                            if (
                                response_future is not None
                                and not response_future.done()
                            ):
                                response_future.set_exception(
                                    RuntimeError(
                                        "enqueue_and_wait cannot wait for low-priority background tasks"
                                    )
                                )

                            # Track this background task so it can be cancelled if a
                            # user message arrives for the same interface_path.
                            _bg_entry = _BackgroundTaskEntry(
                                task=processing_task,
                                cancel_on_user_message=_should_cancel_low_priority_on_user_message(
                                    context
                                ),
                            )
                            _bg_tasks[interface_path] = _bg_entry

                            # Ensure generation_end hook is called when background task completes.
                            # Bind the current iteration's closure: the callback fires after the
                            # loop has moved on and the name would resolve to a newer item's hook.
                            processing_task.add_done_callback(
                                lambda t, _cb=_call_bot_generation_end: (
                                    asyncio.create_task(_cb(t))
                                )
                            )

                            # Clean up tracking and log exceptions when done
                            _captured_ipath = interface_path  # capture for closure

                            def _bg_done_cb(
                                t: asyncio.Task,
                                _ipath: str = _captured_ipath,
                                _entry: _BackgroundTaskEntry = _bg_entry,
                            ) -> None:
                                # Remove from tracking dict
                                current_entry = _bg_tasks.get(_ipath)
                                if current_entry is _entry:
                                    _bg_tasks.pop(_ipath, None)
                                try:
                                    exc = t.exception()
                                    if exc is not None:
                                        log_warning(
                                            f"[QUEUE] Background task for {_ipath} raised: {exc}"
                                        )
                                except asyncio.CancelledError:
                                    log_info(
                                        f"[QUEUE] Background task for {_ipath} was cancelled (user message arrived)"
                                    )
                                except Exception:
                                    pass

                            processing_task.add_done_callback(_bg_done_cb)

                            # Continue to next queued item without waiting
                            continue

                        timed_out = False
                        try:
                            await asyncio.wait_for(
                                processing_task, timeout=timeout_seconds
                            )
                        except asyncio.TimeoutError:
                            timed_out = True
                            log_error(
                                f"[QUEUE] Message processing timed out after {timeout_seconds}s for chat {chat_id}"  # log uses whatever timeout is active
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

                            if task_is_cancellable:
                                # Cancel the task immediately so no delayed "ghost" response
                                # arrives after the fallback has already been sent to the user.
                                processing_task.cancel()
                                try:
                                    await asyncio.wait_for(
                                        asyncio.shield(processing_task), timeout=2
                                    )
                                except (
                                    asyncio.TimeoutError,
                                    asyncio.CancelledError,
                                    Exception,
                                ):
                                    pass
                                try:
                                    await _call_bot_generation_end(processing_task)
                                except Exception:
                                    pass
                                log_debug(
                                    f"[QUEUE] Processing task cancelled after timeout for chat {chat_id}"
                                )
                            else:
                                # Non-cancellable engine (e.g. Selenium) — let task finish in
                                # background and clean up when it eventually completes.
                                log_debug(
                                    f"[QUEUE] Processing task kept alive (non-cancellable engine) for chat {chat_id}"
                                )

                                async def _clear_processing_when_done(
                                    *,
                                    _ptask: asyncio.Task = processing_task,
                                    _chat_id: Any = chat_id,
                                    _ipath: str = interface_path,
                                    _end_cb: Any = _call_bot_generation_end,
                                    _broadcast: Any = _broadcast_global_animation_state,
                                    _resolve: Any = _resolve_generation_animation_state,
                                ) -> None:
                                    # Runs via add_done_callback after the loop has moved
                                    # on — all loop variables and per-iteration closures
                                    # must be bound as defaults, not read from the cells.
                                    try:
                                        try:
                                            exc = _ptask.exception()
                                            if exc is not None:
                                                log_warning(
                                                    f"[QUEUE] Background processing task error for chat {_chat_id}: {exc}"
                                                )
                                        except asyncio.CancelledError:
                                            return
                                        except Exception:
                                            pass

                                        try:
                                            await _end_cb(_ptask)
                                        except Exception:
                                            pass

                                        still_pending = False
                                        for prio, _, queued_item in list(
                                            _get_queue()._queue
                                        ):
                                            item_chat = (
                                                queued_item.get("chat_id")
                                                if isinstance(queued_item, dict)
                                                else getattr(
                                                    queued_item, "chat_id", None
                                                )
                                            )
                                            if item_chat == _chat_id:
                                                still_pending = True
                                                break
                                        if not still_pending:
                                            try:
                                                existing_meta = (
                                                    await get_session_meta_fn(_ipath)
                                                    or {}
                                                )
                                                existing_meta["processing"] = False
                                                await set_session_meta_fn(
                                                    _ipath, existing_meta
                                                )
                                            except Exception as set_e:
                                                log_debug(
                                                    f"[QUEUE] Failed to clear processing session meta (background): {set_e}"
                                                )
                                            await _broadcast(_resolve("end"))
                                    except Exception as e:
                                        log_debug(
                                            f"[QUEUE] Error in background completion handler for chat {_chat_id}: {e}"
                                        )

                                try:
                                    processing_task.add_done_callback(
                                        lambda _t, _cb=_clear_processing_when_done: (
                                            asyncio.create_task(_cb())
                                        )
                                    )
                                except Exception as cb_e:
                                    log_debug(
                                        f"[QUEUE] Failed to attach background completion callback: {cb_e}"
                                    )
                        finally:
                            if (
                                response_future is not None
                                and not response_future.done()
                            ):
                                if processing_task.done():
                                    try:
                                        response_future.set_result(
                                            processing_task.result()
                                        )
                                    except Exception as exc:
                                        response_future.set_exception(exc)
                                elif timed_out:
                                    response_future.set_exception(
                                        asyncio.TimeoutError(
                                            f"Message processing timed out after {timeout_seconds}s"
                                        )
                                    )

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
                                for prio, _, queued_item in list(_get_queue()._queue):
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
                    _get_queue().task_done()
        except asyncio.CancelledError:
            if _shutdown_requested:
                log_info("[QUEUE] Consumer loop cancelled (deliberate shutdown)")
                break
            # An *accidental* cancellation (e.g. a per-message timeout cancel that
            # propagated up, or a structured-concurrency parent cancel) must NOT
            # silently kill the consumer forever. Re-raise so the supervisor
            # watchdog detects the dead task and restarts it.
            log_warning(
                "[QUEUE] Consumer loop received an unexpected cancellation; "
                "re-raising so the supervisor can restart it"
            )
            raise
        except Exception as e:
            log_error(
                f"[QUEUE] Unexpected error in consumer loop: {repr(e)}\n{traceback.format_exc()}"
            )
            # A stale-loop error means every subsequent iteration will fail the
            # same way.  Break immediately rather than spinning and spamming logs.
            if isinstance(e, RuntimeError) and "bound to a different event loop" in str(
                e
            ):
                log_error(
                    "[QUEUE] Consumer stopping: queue bound to wrong event loop. Call run() to reinitialize."
                )
                break


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
    for prio, cnt, queued_item in list(_get_queue()._queue):
        if queued_item.get("event_prompt") == prompt_data:
            log_warning("[QUEUE] Duplicate event detected, not added to the queue")
            return

    global _counter
    _counter += 1
    await _get_queue().put((_heap_key(PRIORITY_URGENT), _counter, item))
    log_debug(f"[QUEUE] Event added to the queue with priority: {prompt_data}")
    log_debug(f"[QUEUE] Current queue state: {list(_get_queue()._queue)}")


def _start_consumer_task() -> None:
    """(Re)create the consumer task if it is missing or finished."""
    global _consumer_task

    if _consumer_task and not _consumer_task.done():
        return

    _consumer_task = asyncio.create_task(_consumer_loop())
    log_info("[QUEUE] Consumer task started")


async def _supervisor_loop() -> None:
    """Watchdog that keeps the consumer alive.

    A single hung LLM generation (e.g. a Selenium engine that stalls) can trigger
    a per-message timeout whose cancellation propagates up and kills the consumer
    task. Without supervision the consumer never restarts, so every subsequent
    message queues silently and is never processed. This loop detects a dead
    consumer and restarts it, unless a deliberate shutdown was requested.
    """
    log_info("[QUEUE] Consumer supervisor started")
    while not _shutdown_requested:
        try:
            await asyncio.sleep(_SUPERVISOR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            break

        if _shutdown_requested:
            break

        task = _consumer_task
        if task is None or task.done():
            # Surface why the previous consumer died, for diagnostics.
            if task is not None:
                try:
                    exc = task.exception()
                    if exc is not None:
                        log_warning(
                            f"[QUEUE] Supervisor: consumer task died with {exc!r}; restarting"
                        )
                    else:
                        log_warning(
                            "[QUEUE] Supervisor: consumer task exited unexpectedly; restarting"
                        )
                except asyncio.CancelledError:
                    log_warning(
                        "[QUEUE] Supervisor: consumer task was cancelled; restarting"
                    )
                except asyncio.InvalidStateError:
                    # Should not happen (task.done() is True), guard anyway.
                    pass
            _start_consumer_task()

    log_info("[QUEUE] Consumer supervisor stopped")


async def run() -> None:
    """Convenience wrapper to launch the consumer task if not running."""
    global _supervisor_task, _shutdown_requested

    # A fresh run() clears any previous shutdown request.
    _shutdown_requested = False

    if _consumer_task and not _consumer_task.done():
        log_debug("[QUEUE] Consumer already running")
    else:
        # Ensure the queue primitives are initialized on the active event loop.
        _get_queue()
        _get_lock()
        _start_consumer_task()

    # Launch the supervisor watchdog once.
    if _supervisor_task is None or _supervisor_task.done():
        _supervisor_task = asyncio.create_task(_supervisor_loop())


async def stop() -> None:
    """Deliberately stop the consumer and supervisor (e.g. on shutdown)."""
    global _shutdown_requested, _consumer_task, _supervisor_task

    _shutdown_requested = True

    for task in (_supervisor_task, _consumer_task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass

    _consumer_task = None
    _supervisor_task = None
    log_info("[QUEUE] Consumer and supervisor stopped")
