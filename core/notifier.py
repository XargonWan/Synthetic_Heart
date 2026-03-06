# core/notifier.py

import asyncio
import time
from typing import List, Tuple, Callable
from core.logging_utils import log_debug, log_info, log_warning
from core.config import (
    get_log_chat_id_sync,
    get_log_chat_thread_id_sync,
    get_log_chat_interface_sync,
)
from collections import deque

_in_notify = False

_pending: List[Tuple[int, str]] = []
# Messages targeting interfaces that are not yet registered
_pending_interface_msgs: List[Tuple[str, int | str, str]] = []

# Flood-control / de-duplication state (initialized once at module load)
_last_notify_times = deque(maxlen=100)
_last_notify_messages = {}  # chat_id -> (last_time, message)
_NOTIFY_CAP_PER_SEC = 5
_NOTIFY_IDENTICAL_BLOCK_SEC = 300  # 5 minutes

# Store main event loop to avoid creating new ones
_main_loop = None


def _set_main_loop(loop):
    """Store reference to main event loop."""
    global _main_loop
    _main_loop = loop


def _safe_schedule_async(coro):
    """Schedule coroutine safely without creating new event loops."""
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            return loop.create_task(coro)
    except RuntimeError:
        # No running loop
        pass

    # Fallback: use stored main loop if available
    if _main_loop and not _main_loop.is_closed():
        try:
            return _main_loop.create_task(coro)
        except Exception as e:
            log_debug(f"[notifier] Failed to schedule on main loop: {e}")

    # Last resort: this shouldn't happen in normal operation
    log_warning("[notifier] Could not schedule async task - no event loop available")


def _default_notify(chat_id: int, message: str):
    """Fallback when no real notifier is configured: queue the message."""
    log_info(f"[NOTIFY:{chat_id}] {message}")
    # Queue pending so real notifier can flush later
    if (chat_id, message) not in _pending:
        _pending.append((chat_id, message))


_notify_impl: Callable[[int, str], None] = _default_notify


def _maybe_call_notify_fn(fn: Callable[[int, str], None], chat_id: int, chunk: str):
    """Call notifier function which may be sync or return a coroutine.

    Schedule coroutine results appropriately depending on whether an event
    loop is already running.
    """
    try:
        result = fn(chat_id, chunk)
        # If the notifier returned a coroutine, schedule or run it
        if asyncio.iscoroutine(result):
            _safe_schedule_async(result)
    except Exception as e:  # pragma: no cover - best effort
        log_warning(f"[notifier] Notifier function raised while sending: {repr(e)}")


def set_notifier(fn: Callable[[int, str], None]):
    """Register the real notifier and flush any queued messages.

    The notifier can be a synchronous function or an async function that
    returns a coroutine; both are supported.
    """
    global _notify_impl
    _notify_impl = fn
    # Flush pending messages safely
    for chat_id, msg in list(_pending):
        try:
            _maybe_call_notify_fn(fn, chat_id, msg)
        except Exception as e:
            log_warning(f"[notifier] Failed to send pending message: {repr(e)}")
    _pending.clear()


CHUNK_SIZE = 4000


def notify(chat_id: int, message: str):
    """Send ``message`` to ``chat_id`` in chunks to avoid interface limits.

    Rate-limits and deduplicates messages across calls.
    """
    global _in_notify

    if _in_notify:
        log_warning("[notifier] Recursive notify call detected; skipping")
        return

    # Flood control: non inviare più di X notify al secondo
    now = time.time()
    recent = [t for t in _last_notify_times if now - t < 1]
    if len(recent) >= _NOTIFY_CAP_PER_SEC:
        log_warning(
            f"[notifier] Flood control: troppe notify in 1 secondo, bloccata: {message}"
        )
        return

    # Blocca messaggi identici per N secondi
    last_time, last_msg = _last_notify_messages.get(chat_id, (0, None))
    if last_msg == message and now - last_time < _NOTIFY_IDENTICAL_BLOCK_SEC:
        log_info(
            f"[notifier] Messaggio identico già inviato <{_NOTIFY_IDENTICAL_BLOCK_SEC}s, bloccato: {message}"
        )
        return

    # Record this send
    _last_notify_times.append(now)
    _last_notify_messages[chat_id] = (now, message)

    _in_notify = True
    try:
        log_debug(f"[notifier] Sending message to {chat_id}: {message}")
        for i in range(0, len(message or ""), CHUNK_SIZE):
            chunk = message[i : i + CHUNK_SIZE]
            try:
                # Use helper that supports async notifier functions
                _maybe_call_notify_fn(_notify_impl, chat_id, chunk)
            except Exception as e:  # pragma: no cover - best effort
                log_warning(f"[notifier] Failed to send notification chunk: {repr(e)}")
    finally:
        _in_notify = False


def notifier(message: str) -> None:
    """Simplified notification: LogChat if available, otherwise fallback to trainer."""
    from core.config import (
        get_log_chat_thread_id_sync,
    )
    from core.core_initializer import INTERFACE_REGISTRY

    log_debug(f"[notifier] Sending: {message}")

    # Try LogChat first
    log_chat_id = get_log_chat_id_sync()
    log_chat_interface = get_log_chat_interface_sync()
    if log_chat_id and log_chat_interface and log_chat_interface in INTERFACE_REGISTRY:
        log_debug(f"[notifier] Using LogChat {log_chat_id} via {log_chat_interface}")
        iface = INTERFACE_REGISTRY.get(log_chat_interface)
        if iface:

            async def send_to_logchat():
                try:
                    message_data = {"text": message, "target": log_chat_id}
                    thread_id = get_log_chat_thread_id_sync()
                    if thread_id:
                        message_data["thread_id"] = thread_id
                        log_debug(
                            f"[notifier] Sending to LogChat with thread_id={thread_id}"
                        )

                    await iface.send_message(message_data)
                    log_debug("[notifier] Message sent to LogChat successfully")
                except Exception as e:
                    log_warning(f"[notifier] Failed to send to LogChat: {repr(e)}")
                    # Fallback to trainer
                    _fallback_to_trainer(message)

            try:
                loop = asyncio.get_running_loop()
                if loop and loop.is_running():
                    loop.create_task(send_to_logchat())
                else:
                    _safe_schedule_async(send_to_logchat())
            except RuntimeError:
                _safe_schedule_async(send_to_logchat())
            return

    # Fallback to trainer
    log_debug("[notifier] LogChat not available, using trainer fallback")
    _fallback_to_trainer(message)


def notify_intelligent(message: str) -> None:
    """Intelligent notification: LogChat if available, otherwise trainer via interfaces."""
    from core.config import (
        get_log_chat_thread_id_sync,
    )
    from core.core_initializer import INTERFACE_REGISTRY

    log_info(f"[notifier] notify_intelligent() called with message: {message}")

    # Try LogChat first
    log_chat_id = get_log_chat_id_sync()
    log_chat_interface = get_log_chat_interface_sync()
    if log_chat_id and log_chat_interface and log_chat_interface in INTERFACE_REGISTRY:
        log_info(f"[notifier] Using LogChat {log_chat_id} via {log_chat_interface}")
        iface = INTERFACE_REGISTRY.get(log_chat_interface)
        if iface:

            async def send_to_logchat():
                try:
                    message_data = {"text": message, "target": log_chat_id}
                    thread_id = get_log_chat_thread_id_sync()
                    if thread_id:
                        message_data["thread_id"] = thread_id
                        log_info(
                            f"[notifier] Sending to LogChat with thread_id={thread_id}"
                        )
                    else:
                        log_info("[notifier] Sending to LogChat without thread_id")

                    await iface.send_message(message_data)
                    log_info("[notifier] Message sent to LogChat successfully")
                except Exception as e:
                    log_warning(f"[notifier] Failed to send to LogChat: {repr(e)}")
                    # Fallback to trainer
                    _fallback_to_trainer(message)

            try:
                loop = asyncio.get_running_loop()
                if loop and loop.is_running():
                    loop.create_task(send_to_logchat())
                else:
                    _safe_schedule_async(send_to_logchat())
            except RuntimeError:
                _safe_schedule_async(send_to_logchat())
            return

    # Fallback to trainer
    log_info("[notifier] LogChat not available, using trainer fallback")
    _fallback_to_trainer(message)


def _fallback_to_trainer(message: str) -> None:
    """Fallback notification to trainer."""
    from core.config import get_trainer_id
    from core.core_initializer import INTERFACE_REGISTRY
    from core.interfaces_registry import get_interface_registry

    # Try all available interfaces with trainer IDs
    registry = get_interface_registry()
    for interface_name in registry.get_interface_names():
        trainer_id = get_trainer_id(interface_name)
        if not trainer_id or interface_name not in INTERFACE_REGISTRY:
            continue

        # trainer_id may be a list; iterate numeric entries only
        ids_to_try: list[int] = []
        if isinstance(trainer_id, (list, tuple)):
            for t in trainer_id:
                try:
                    ids_to_try.append(int(t))
                except Exception:
                    # ignore non-int values when doing fallback send
                    continue
        else:
            try:
                ids_to_try.append(int(trainer_id))
            except Exception:
                ids_to_try = []

        for tid in ids_to_try:
            log_info(f"[notifier] Fallback: sending to {interface_name} trainer {tid}")
            iface = INTERFACE_REGISTRY.get(interface_name)
            if not iface:
                break

            async def send_to_trainer(target_id: int):
                try:
                    message_data = {"text": message, "target": target_id}
                    await iface.send_message(message_data)
                    log_info(
                        f"[notifier] Message sent to {interface_name} trainer successfully"
                    )
                except Exception as e:
                    log_warning(
                        f"[notifier] Failed to send to {interface_name} trainer: {repr(e)}"
                    )

            try:
                loop = asyncio.get_running_loop()
                if loop and loop.is_running():
                    loop.create_task(send_to_trainer(tid))
                else:
                    _safe_schedule_async(send_to_trainer(tid))
            except RuntimeError:
                _safe_schedule_async(send_to_trainer(tid))
        # we attempted all numeric ids for this interface and either sent or skipped
        if ids_to_try:
            return

    # No fallback available
    log_warning(f"[notifier] No trainer available, message lost: {message[:50]}...")


def notify_trainer(message: str) -> None:
    """Notify the trainer via selected interfaces."""
    from core.config import (
        get_trainer_ids,
        get_log_chat_interface_sync,
    )
    from core.core_initializer import INTERFACE_REGISTRY

    log_info(f"[notifier] notify_trainer() called with message: {message}")
    log_info(f"[notifier] INTERFACE_REGISTRY keys: {list(INTERFACE_REGISTRY.keys())}")
    trainer_ids = get_trainer_ids()
    log_info(f"[notifier] TRAINER_IDS: {trainer_ids}")

    # If TRAINER_IDS is configured, use it
    if trainer_ids:
        interface_configs = trainer_ids.items()
        log_info(f"[notifier] Using TRAINER_IDS: {interface_configs}")
    else:
        # Fallback: If LogChat is configured, use it
        log_chat_id = get_log_chat_id_sync()
        log_chat_interface = get_log_chat_interface_sync()
        log_info(f"[notifier] LogChat ID from DB: {log_chat_id}")
        log_info(f"[notifier] LogChat interface: {log_chat_interface}")

        if (
            log_chat_id
            and log_chat_interface
            and log_chat_interface in INTERFACE_REGISTRY
        ):
            interface_configs = [(log_chat_interface, log_chat_id)]
            log_info(f"[notifier] Using LogChat config: {interface_configs}")
        else:
            log_info(
                "[notifier] No interfaces configured for error notifications and no fallback available"
            )
            return

    for interface_name, trainer_id in interface_configs:
        iface = INTERFACE_REGISTRY.get(interface_name)
        if not iface:
            available = ", ".join(sorted(INTERFACE_REGISTRY)) or "none"
            log_warning(
                f"[notifier] No interface '{interface_name}' available. "
                f"Available: {available}; queuing notification"
            )
            entry = (interface_name, trainer_id, message)
            if entry not in _pending_interface_msgs:
                _pending_interface_msgs.append(entry)
            continue

        targets: list[int | str] = []
        # Use the trainer_id that was determined in the interface_configs logic above
        targets.append(trainer_id)
        log_info(f"[notifier] Using target: {trainer_id} interface: {interface_name}")

        if not targets:
            continue

        async def send(target: int | str):
            try:
                # Build message data with thread_id for LogChat if applicable
                message_data = {"text": message, "target": target}
                log_chat_id = get_log_chat_id_sync()
                if target == log_chat_id:
                    thread_id = get_log_chat_thread_id_sync()
                    if thread_id:
                        message_data["thread_id"] = thread_id
                        log_info(
                            f"[notifier] Sending to LogChat {target} with thread_id={thread_id}, message_data={message_data}"
                        )
                    else:
                        log_info(
                            f"[notifier] Sending to LogChat {target} without thread_id (thread_id={thread_id})"
                        )
                else:
                    log_info(
                        f"[notifier] Sending to trainer {target} (log_chat_id={log_chat_id})"
                    )

                log_info(f"[notifier] Final message_data: {message_data}")
                await iface.send_message(message_data)
            except Exception as e:  # pragma: no cover - best effort
                # Check if interpreter is shutting down
                if "interpreter shutdown" in str(
                    e
                ) or "cannot schedule new futures" in str(e):
                    log_debug(
                        f"[notifier] Notification failed due to interpreter shutdown, skipping: {repr(e)}"
                    )
                else:
                    log_warning(
                        f"[notifier] Failed to notify via {interface_name}: {repr(e)}",
                    )

        for tgt in targets:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                loop.create_task(send(tgt))
            else:
                _safe_schedule_async(send(tgt))


def flush_pending_for_interface(interface_name: str) -> None:
    """Flush queued trainer notifications for a newly registered interface."""
    from core.core_initializer import INTERFACE_REGISTRY

    iface = INTERFACE_REGISTRY.get(interface_name)
    if not iface:
        return

    remaining: List[Tuple[str, int | str, str]] = []
    for name, trainer_id, msg in _pending_interface_msgs:
        if name != interface_name:
            remaining.append((name, trainer_id, msg))
            continue

        async def send():
            try:
                await iface.send_message({"text": msg, "target": trainer_id})
            except Exception as e:  # pragma: no cover - best effort
                log_warning(
                    f"[notifier] Failed to flush pending notify via {interface_name}: {repr(e)}",
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            loop.create_task(send())
        else:
            _safe_schedule_async(send())

    _pending_interface_msgs[:] = remaining
